"""Core business logic.

Every write operation runs in exactly one transaction so that trip status,
daily settlements and journals commit (or roll back) atomically.
Per-account advisory locks serialize writes to the same account; locks are
acquired in a deterministic key order so corrections spanning two operating
days cannot deadlock.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

import psycopg

from .config import Settings
from .errors import ServiceError
from .fare import Leg, settle_day
from .timeutil import SHANGHAI_TZ, op_date_of, to_shanghai

TRIP_COLUMNS = (
    "trip_no", "account_id", "board_time", "base_fare",
    "version", "status", "op_date",
)
JOURNAL_COLUMNS = (
    "seq", "account_id", "op_date", "amount",
    "entry_type", "reason", "trip_no", "created_at",
)

# Sanity guard against illegal boarding times.
MIN_BOARD_YEAR = 2000
MAX_BOARD_YEAR = 2100


def _lock_key(account_id: str) -> int:
    digest = hashlib.blake2b(account_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _normalise_board_time(raw: datetime) -> datetime:
    """Validate a boarding time and return a timezone-aware instant.

    Naive datetimes are interpreted as Beijing local time, per the API contract.
    """
    aware = raw if raw.tzinfo is not None else raw.replace(tzinfo=SHANGHAI_TZ)
    sh = to_shanghai(aware)
    if not (MIN_BOARD_YEAR <= sh.year < MAX_BOARD_YEAR):
        raise ServiceError(
            "INVALID_BOARD_TIME",
            f"board_time year must be within [{MIN_BOARD_YEAR}, {MAX_BOARD_YEAR})",
            http_status=422,
        )
    return aware


def _payload(columns: tuple[str, ...], row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(columns, row))


async def _lock_accounts(conn: psycopg.AsyncConnection, account_ids: list[str]) -> None:
    """Acquire per-account transaction-scoped advisory locks in key order."""
    for key in sorted({_lock_key(a) for a in account_ids}):
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def _same_content(existing: dict[str, Any], account_id: str, board_time: datetime, base_fare: int) -> bool:
    """Idempotency check: account + instant + fare must match exactly.

    TIMESTAMPTZ equality is instant-based, so the same moment expressed in
    another timezone still counts as identical content.
    """
    return (
        existing["account_id"] == account_id
        and existing["board_time"] == board_time
        and existing["base_fare"] == base_fare
    )


async def _get_trip(
    conn: psycopg.AsyncConnection, trip_no: str, for_update: bool = False
) -> dict[str, Any] | None:
    sql = f"SELECT {', '.join(TRIP_COLUMNS)} FROM trips WHERE trip_no = %s"
    if for_update:
        sql += " FOR UPDATE"
    cursor = await conn.execute(sql, (trip_no,))
    record = await cursor.fetchone()
    return _payload(TRIP_COLUMNS, record) if record else None


async def _insert_journal(
    conn: psycopg.AsyncConnection,
    *,
    account_id: str,
    op_day: date,
    amount: int,
    entry_type: str,
    reason: str,
    trip_no: str,
) -> dict[str, Any]:
    cursor = await conn.execute(
        """
        INSERT INTO journals (account_id, op_date, amount, entry_type, reason, trip_no)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING seq, account_id, op_date, amount, entry_type, reason, trip_no, created_at
        """,
        (account_id, op_day, amount, entry_type, reason, trip_no),
    )
    return _payload(JOURNAL_COLUMNS, await cursor.fetchone())


async def _recalculate_day(
    conn: psycopg.AsyncConnection,
    *,
    account_id: str,
    op_day: date,
    settings: Settings,
    reason: str,
    trigger_trip_no: str,
) -> list[dict[str, Any]]:
    """Rebuild groups + settlement for one account/day; post ledger deltas.

    The caller holds the advisory lock for ``account_id``, so at most one
    recalculation of the day runs at a time. When the actually-charged amount
    changes, the old amount is reversed and the new amount is posted; if the
    capped amount is unchanged (e.g. the cap still binds) no journal is added.
    """
    # Lock (or create) the settlement row first.
    cursor = await conn.execute(
        "SELECT charged_fare, raw_fare, trip_count FROM daily_settlements "
        "WHERE account_id=%s AND op_date=%s FOR UPDATE",
        (account_id, op_day),
    )
    row = await cursor.fetchone()
    if row is None:
        old_charged: int | None = None
        await conn.execute(
            "INSERT INTO daily_settlements (account_id, op_date, cap_cents) VALUES (%s, %s, %s)",
            (account_id, op_day, settings.daily_cap_cents),
        )
        first_settlement = True
    else:
        old_charged = row[0]
        # A row may already exist (e.g. a prior all-zero day); "settled"
        # applies only while it has never carried a non-empty result.
        first_settlement = row[1] == 0 and row[2] == 0 and old_charged == 0

    legs_cursor = await conn.execute(
        """
        SELECT trip_no, board_time, base_fare
        FROM trips
        WHERE account_id = %s AND op_date = %s AND status = 'active'
        ORDER BY board_time, trip_no
        """,
        (account_id, op_day),
    )
    legs = [
        Leg(trip_no=r[0], board_time=r[1], base_fare=r[2])
        for r in await legs_cursor.fetchall()
    ]

    groups, raw_fare, charged_fare = settle_day(
        legs,
        cap_cents=settings.daily_cap_cents,
    )

    # Rebuild transfer groups for the day (members cascade away with groups).
    await conn.execute(
        "DELETE FROM transfer_groups WHERE account_id=%s AND op_date=%s",
        (account_id, op_day),
    )
    for index, group in enumerate(groups):
        group_fare = max(leg.base_fare for leg in group)
        group_cursor = await conn.execute(
            """
            INSERT INTO transfer_groups (account_id, op_date, group_index, group_fare)
            VALUES (%s, %s, %s, %s)
            RETURNING group_id
            """,
            (account_id, op_day, index, group_fare),
        )
        group_id = (await group_cursor.fetchone())[0]
        for order, leg in enumerate(group):
            await conn.execute(
                """
                INSERT INTO group_members (group_id, trip_no, leg_order, base_fare)
                VALUES (%s, %s, %s, %s)
                """,
                (group_id, leg.trip_no, order, leg.base_fare),
            )

    journals: list[dict[str, Any]] = []
    effective_old = old_charged if old_charged is not None else 0
    if effective_old != charged_fare:
        if effective_old > 0:
            journals.append(await _insert_journal(
                conn,
                account_id=account_id,
                op_day=op_day,
                amount=-effective_old,
                entry_type="reversal",
                reason=f"{reason}: reverse previous settlement",
                trip_no=trigger_trip_no,
            ))
        if charged_fare > 0:
            journals.append(await _insert_journal(
                conn,
                account_id=account_id,
                op_day=op_day,
                amount=charged_fare,
                entry_type="charge",
                reason=f"{reason}: new settlement",
                trip_no=trigger_trip_no,
            ))

    await conn.execute(
        """
        UPDATE daily_settlements
        SET raw_fare=%s, charged_fare=%s, cap_cents=%s, trip_count=%s,
            status=%s, updated_at=now()
        WHERE account_id=%s AND op_date=%s
        """,
        (
            raw_fare, charged_fare, settings.daily_cap_cents, len(legs),
            "settled" if first_settlement else "recalculated",
            account_id, op_day,
        ),
    )
    return journals


# ---------------------------------------------------------------------------
# Public write operations
# ---------------------------------------------------------------------------

async def submit_trip(
    conn: psycopg.AsyncConnection,
    *,
    account_id: str,
    trip_no: str,
    board_time: datetime,
    base_fare: int,
    settings: Settings,
) -> dict[str, Any]:
    board_time = _normalise_board_time(board_time)
    op_day = op_date_of(board_time)

    await conn.execute("BEGIN")
    try:
        await _lock_accounts(conn, [account_id])

        existing = await _get_trip(conn, trip_no, for_update=True)
        if existing is not None:
            if not _same_content(existing, account_id, board_time, base_fare):
                raise ServiceError(
                    "TRIP_CONTENT_CONFLICT",
                    f"trip_no {trip_no!r} already exists with different content; "
                    "use POST /trips/{trip_no}/correct to amend it",
                    http_status=409,
                )
            # Identical retransmission: idempotent, nothing is recomputed.
            return {"trip": existing, "idempotent": True, "recalculated_dates": [], "journals": []}

        await conn.execute(
            """
            INSERT INTO trips (trip_no, account_id, board_time, base_fare, version, status, op_date)
            VALUES (%s, %s, %s, %s, 1, 'active', %s)
            """,
            (trip_no, account_id, board_time, base_fare, op_day),
        )
        journals = await _recalculate_day(
            conn,
            account_id=account_id,
            op_day=op_day,
            settings=settings,
            reason="trip_submit",
            trigger_trip_no=trip_no,
        )
        trip = await _get_trip(conn, trip_no)
        await conn.commit()
        return {
            "trip": trip,
            "idempotent": False,
            "recalculated_dates": [op_day],
            "journals": journals,
        }
    except Exception:
        await conn.rollback()
        raise


async def correct_trip(
    conn: psycopg.AsyncConnection,
    *,
    trip_no: str,
    expected_version: int,
    board_time: datetime,
    base_fare: int,
    settings: Settings,
) -> dict[str, Any]:
    board_time = _normalise_board_time(board_time)
    new_op_day = op_date_of(board_time)

    await conn.execute("BEGIN")
    try:
        peek = await _get_trip(conn, trip_no)
        if peek is None:
            raise ServiceError("TRIP_NOT_FOUND", f"unknown trip_no {trip_no!r}", http_status=404)
        await _lock_accounts(conn, [peek["account_id"]])

        existing = await _get_trip(conn, trip_no, for_update=True)
        if existing["status"] == "revoked":
            raise ServiceError("TRIP_REVOKED", f"trip {trip_no!r} has been revoked", http_status=409)
        if existing["version"] != expected_version:
            raise ServiceError(
                "STALE_VERSION",
                f"expected_version {expected_version} is stale; current version is {existing['version']}",
                http_status=409,
            )

        unchanged = (
            existing["board_time"] == board_time and existing["base_fare"] == base_fare
        )
        if unchanged:
            # Correction carrying the current version and identical content:
            # a no-op confirmation, reported as idempotent.
            return {"trip": existing, "idempotent": True, "recalculated_dates": [], "journals": []}

        account_id = existing["account_id"]
        old_op_day = existing["op_date"]
        await conn.execute(
            """
            UPDATE trips
            SET board_time=%s, base_fare=%s, op_date=%s,
                version=version+1, updated_at=now()
            WHERE trip_no=%s
            """,
            (board_time, base_fare, new_op_day, trip_no),
        )

        # Both the old and the new operating day are recomputed (they coincide
        # unless the correction crosses the 04:00 boundary).
        affected_days = sorted({old_op_day, new_op_day})
        journals: list[dict[str, Any]] = []
        for day in affected_days:
            journals.extend(await _recalculate_day(
                conn,
                account_id=account_id,
                op_day=day,
                settings=settings,
                reason="trip_correct",
                trigger_trip_no=trip_no,
            ))

        trip = await _get_trip(conn, trip_no)
        await conn.commit()
        return {
            "trip": trip,
            "idempotent": False,
            "recalculated_dates": affected_days,
            "journals": journals,
        }
    except Exception:
        await conn.rollback()
        raise


async def revoke_trip(
    conn: psycopg.AsyncConnection,
    *,
    trip_no: str,
    expected_version: int,
    settings: Settings,
) -> dict[str, Any]:
    await conn.execute("BEGIN")
    try:
        peek = await _get_trip(conn, trip_no)
        if peek is None:
            raise ServiceError("TRIP_NOT_FOUND", f"unknown trip_no {trip_no!r}", http_status=404)
        await _lock_accounts(conn, [peek["account_id"]])

        existing = await _get_trip(conn, trip_no, for_update=True)
        if existing["status"] == "revoked" or existing["version"] != expected_version:
            # Covers revoked trips as well as replays after a successful revoke
            # (the version was incremented when the revocation was applied).
            raise ServiceError(
                "STALE_VERSION",
                f"cannot revoke with version {expected_version}; trip {trip_no!r} is at "
                f"version {existing['version']} ({existing['status']}); fetch the trip first",
                http_status=409,
            )

        account_id = existing["account_id"]
        op_day = existing["op_date"]
        await conn.execute(
            "UPDATE trips SET status='revoked', version=version+1, updated_at=now() WHERE trip_no=%s",
            (trip_no,),
        )
        journals = await _recalculate_day(
            conn,
            account_id=account_id,
            op_day=op_day,
            settings=settings,
            reason="trip_revoke",
            trigger_trip_no=trip_no,
        )
        trip = await _get_trip(conn, trip_no)
        await conn.commit()
        return {
            "trip": trip,
            "idempotent": False,
            "recalculated_dates": [op_day],
            "journals": journals,
        }
    except Exception:
        await conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Public read operations
# ---------------------------------------------------------------------------

async def get_trip(conn: psycopg.AsyncConnection, *, trip_no: str) -> dict[str, Any]:
    trip = await _get_trip(conn, trip_no)
    if trip is None:
        raise ServiceError("TRIP_NOT_FOUND", f"unknown trip_no {trip_no!r}", http_status=404)
    return trip


async def get_day_settlement(
    conn: psycopg.AsyncConnection,
    *,
    account_id: str,
    op_day: date,
    cap_cents: int,
) -> dict[str, Any]:
    rows = await conn.execute(
        """
        SELECT g.group_index, g.group_fare,
               m.trip_no, t.board_time, m.base_fare
        FROM transfer_groups g
        JOIN group_members m ON m.group_id = g.group_id
        JOIN trips t ON t.trip_no = m.trip_no
        WHERE g.account_id = %s AND g.op_date = %s
        ORDER BY g.group_index, m.leg_order
        """,
        (account_id, op_day),
    )
    groups: dict[int, dict[str, Any]] = {}
    for group_index, group_fare, member_trip_no, board_time, base_fare in await rows.fetchall():
        bucket = groups.setdefault(
            group_index, {"group_index": group_index, "group_fare": group_fare, "legs": []}
        )
        bucket["legs"].append({
            "trip_no": member_trip_no,
            "board_time": board_time,
            "base_fare": base_fare,
        })

    settle = await conn.execute(
        """
        SELECT raw_fare, charged_fare, cap_cents, trip_count, status
        FROM daily_settlements WHERE account_id=%s AND op_date=%s
        """,
        (account_id, op_day),
    )
    record = await settle.fetchone()
    if record is None:
        return {
            "account_id": account_id,
            "op_date": op_day,
            "status": "empty",
            "raw_fare": 0,
            "charged_fare": 0,
            "cap_cents": cap_cents,
            "trip_count": 0,
            "groups": [],
        }
    raw_fare, charged_fare, stored_cap, trip_count, status = record
    return {
        "account_id": account_id,
        "op_date": op_day,
        "status": status,
        "raw_fare": raw_fare,
        "charged_fare": charged_fare,
        "cap_cents": stored_cap,
        "trip_count": trip_count,
        "groups": [groups[i] for i in sorted(groups)],
    }


async def list_journals(
    conn: psycopg.AsyncConnection,
    *,
    after_seq: int,
    limit: int,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Cursor page over the append-only ledger.

    ``after_seq`` is the ``next_cursor`` of the previous page (0 initially).
    ``limit + 1`` rows are fetched so the next cursor is known without
    rereading anything; previously returned rows are never modified.
    """
    sql = (
        "SELECT seq, account_id, op_date, amount, entry_type, reason, trip_no, created_at "
        "FROM journals WHERE seq > %s"
    )
    params: list[Any] = [after_seq]
    if account_id:
        sql += " AND account_id = %s"
        params.append(account_id)
    sql += " ORDER BY seq ASC LIMIT %s"
    params.append(limit + 1)

    cursor = await conn.execute(sql, params)
    records = await cursor.fetchall()
    has_more = len(records) > limit
    page = records[:limit]
    items = [_payload(JOURNAL_COLUMNS, r) for r in page]
    # The cursor always points at the last returned row so the next page
    # starts strictly after it; has_more tells the client whether to continue.
    next_cursor = items[-1]["seq"] if items else None
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
