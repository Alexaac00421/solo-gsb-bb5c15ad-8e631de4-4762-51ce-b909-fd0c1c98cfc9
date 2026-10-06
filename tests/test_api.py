"""End-to-end HTTP/API tests against a real PostgreSQL database."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

D = "2026-10-10"  # operating day


async def _submit(client, account="acc1", trip="T1", board=f"{D}T08:00:00+08:00", fare=200):
    return await client.post(
        "/trips",
        json={"account_id": account, "trip_no": trip, "board_time": board, "base_fare": fare},
    )


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["db"] == "up"


async def test_submit_and_get_trip(client):
    r = await _submit(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["trip"]["version"] == 1
    assert body["trip"]["status"] == "active"
    assert body["trip"]["op_date"] == "2026-10-10"
    assert body["idempotent"] is False

    r = await client.get("/trips/T1")
    assert r.status_code == 200
    assert r.json()["trip_no"] == "T1"
    # Output is rendered in Beijing time.
    assert r.json()["board_time"].startswith("2026-10-10T08:00:00+08:00")


async def test_identical_retransmission_is_idempotent(client):
    assert (await _submit(client)).json()["idempotent"] is False
    again = await _submit(client)
    assert again.status_code == 200
    body = again.json()
    assert body["idempotent"] is True
    assert body["recalculated_dates"] == []
    assert body["journals"] == []

    # Only one charge journal exists for the day despite two submissions.
    r = await client.get("/journals?account_id=acc1")
    charges = [j for j in r.json()["items"] if j["entry_type"] == "charge"]
    assert len(charges) == 1


async def test_same_trip_no_different_content_conflicts(client):
    await _submit(client, fare=200)
    r = await _submit(client, fare=250)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRIP_CONTENT_CONFLICT"


async def test_unknown_trip_errors_are_explicit(client):
    r = await client.post(
        "/trips/NOPE/correct",
        json={"expected_version": 1, "board_time": f"{D}T08:00:00+08:00", "base_fare": 1},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRIP_NOT_FOUND"

    r = await client.post("/trips/NOPE/revoke", json={"expected_version": 1})
    assert r.status_code == 404

    r = await client.get("/trips/NOPE")
    assert r.status_code == 404


async def test_illegal_fare_rejected(client):
    r = await client.post(
        "/trips",
        json={"account_id": "a", "trip_no": "T", "board_time": f"{D}T08:00:00+08:00", "base_fare": -1},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_illegal_time_rejected(client):
    r = await client.post(
        "/trips",
        json={"account_id": "a", "trip_no": "T", "board_time": "not-a-time", "base_fare": 1},
    )
    assert r.status_code == 422

    r = await client.post(
        "/trips",
        json={"account_id": "a", "trip_no": "T2", "board_time": "1990-01-01T08:00:00+08:00", "base_fare": 1},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_BOARD_TIME"


async def test_operating_day_boundary_0400(client):
    # 03:30 belongs to previous day; 04:00 belongs to current day.
    assert (await _submit(client, trip="EARLY", board="2026-10-11T03:30:00+08:00")).json()["trip"]["op_date"] == "2026-10-10"
    assert (await _submit(client, trip="LATE", board="2026-10-11T04:00:00+08:00")).json()["trip"]["op_date"] == "2026-10-11"


async def test_transfer_groups_and_charged_amount(client):
    await _submit(client, trip="A", board=f"{D}T08:00:00+08:00", fare=200)
    await _submit(client, trip="B", board=f"{D}T09:00:00+08:00", fare=500)  # +60m, same group
    await _submit(client, trip="C", board=f"{D}T11:00:00+08:00", fare=300)  # gap 120m -> new group

    r = await client.get(f"/accounts/acc1/operating-days/{D}")
    day = r.json()
    assert day["trip_count"] == 3
    assert day["raw_fare"] == 800  # max(200,500) + 300
    assert day["charged_fare"] == 800
    assert len(day["groups"]) == 2
    assert [leg["trip_no"] for leg in day["groups"][0]["legs"]] == ["A", "B"]
    assert day["groups"][0]["group_fare"] == 500
    assert [leg["trip_no"] for leg in day["groups"][1]["legs"]] == ["C"]
    # First settlement marked 'settled'; later arrivals mark it 'recalculated'.
    assert day["status"] == "recalculated"


async def test_first_settlement_then_recalculated_status(client):
    await _submit(client, trip="A", board=f"{D}T08:00:00+08:00", fare=200)
    first = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert first["status"] == "settled"

    await _submit(client, trip="B", board=f"{D}T12:00:00+08:00", fare=300)
    later = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert later["status"] == "recalculated"


async def test_gap_exactly_90_minutes_is_same_group(client):
    await _submit(client, trip="A", board=f"{D}T08:00:00+08:00", fare=100)
    await _submit(client, trip="B", board=f"{D}T09:30:00+08:00", fare=200)
    day = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert len(day["groups"]) == 1
    assert day["raw_fare"] == 200


async def test_daily_cap_2000(client):
    await _submit(client, trip="A", board=f"{D}T08:00:00+08:00", fare=1500)
    await _submit(client, trip="B", board=f"{D}T12:00:00+08:00", fare=1500)
    day = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert day["raw_fare"] == 3000
    assert day["charged_fare"] == 2000


async def test_correction_bumps_version_and_recalculates(client):
    await _submit(client, trip="A", board=f"{D}T08:00:00+08:00", fare=200)
    r = await client.post(
        "/trips/A/correct",
        json={"expected_version": 1, "board_time": f"{D}T08:00:00+08:00", "base_fare": 700},
    )
    assert r.status_code == 200, r.text
    assert r.json()["trip"]["version"] == 2
    assert r.json()["trip"]["base_fare"] == 700

    day = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert day["charged_fare"] == 700
    assert day["status"] == "recalculated"

    # Stale version rejected.
    r = await client.post(
        "/trips/A/correct",
        json={"expected_version": 1, "board_time": f"{D}T08:00:00+08:00", "base_fare": 800},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "STALE_VERSION"


async def test_cross_day_correction_recalculates_both_days(client):
    # Start on day 2026-10-10 at 03:30 (belongs to op day 10-09), then move
    # it across the 04:00 boundary into op day 10-10.
    r = await _submit(client, trip="A", account="acc1", board="2026-10-10T03:30:00+08:00", fare=900)
    assert r.json()["trip"]["op_date"] == "2026-10-09"

    # Another leg anchors a charge on 10-10 so both days are observable.
    await _submit(client, trip="B", account="acc1", board="2026-10-10T08:00:00+08:00", fare=400)

    old_day = (await client.get("/accounts/acc1/operating-days/2026-10-09")).json()
    assert old_day["charged_fare"] == 900

    r = await client.post(
        "/trips/A/correct",
        json={"expected_version": 1, "board_time": "2026-10-10T04:30:00+08:00", "base_fare": 900},
    )
    assert r.status_code == 200, r.text
    assert r.json()["recalculated_dates"] == ["2026-10-09", "2026-10-10"]

    old_day = (await client.get("/accounts/acc1/operating-days/2026-10-09")).json()
    assert old_day["charged_fare"] == 0
    assert old_day["trip_count"] == 0

    # A joins B's day; 04:30 -> 08:00 gap is 210m so they form two groups: 900+400.
    new_day = (await client.get("/accounts/acc1/operating-days/2026-10-10")).json()
    assert new_day["trip_count"] == 2
    assert new_day["charged_fare"] == 1300
    assert len(new_day["groups"]) == 2

    journals = (await client.get("/journals?account_id=acc1")).json()["items"]
    types = {(j["op_date"], j["entry_type"]) for j in journals}
    assert ("2026-10-09", "charge") in types
    assert ("2026-10-09", "reversal") in types
    assert ("2026-10-10", "charge") in types


async def test_reverse_then_new_charge_amounts_on_late_arrival(client):
    # A late leg joins an existing group and raises its fare.
    await _submit(client, trip="A", board=f"{D}T08:00:00+08:00", fare=200)
    await _submit(client, trip="LATE", board=f"{D}T08:30:00+08:00", fare=600)
    day = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert day["charged_fare"] == 600
    journals = (await client.get("/journals?account_id=acc1")).json()["items"]
    assert [j["amount"] for j in journals] == [200, -200, 600]


async def test_revoke_removes_leg_and_reverses(client):
    await _submit(client, trip="A", board=f"{D}T08:00:00+08:00", fare=200)
    r = await client.post("/trips/A/revoke", json={"expected_version": 1})
    assert r.status_code == 200
    assert r.json()["trip"]["status"] == "revoked"
    assert r.json()["trip"]["version"] == 2

    day = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert day["trip_count"] == 0
    assert day["charged_fare"] == 0
    assert day["groups"] == []

    journals = (await client.get("/journals?account_id=acc1")).json()["items"]
    assert journals[-1]["entry_type"] == "reversal"
    assert journals[-1]["amount"] == -200

    # Replay with the old version is stale; revoked trips cannot be corrected.
    again = await client.post("/trips/A/revoke", json={"expected_version": 1})
    assert again.status_code == 409
    correct = await client.post(
        "/trips/A/correct",
        json={"expected_version": 2, "board_time": f"{D}T09:00:00+08:00", "base_fare": 1},
    )
    assert correct.status_code == 409
    assert correct.json()["error"]["code"] == "TRIP_REVOKED"


async def test_journal_is_append_only(client):
    await _submit(client, trip="A", fare=200)
    # Direct database mutation must be rejected by triggers.
    from app.db import get_pool
    async with get_pool().connection() as conn:
        with pytest.raises(Exception) as exc:
            await conn.execute("UPDATE journals SET amount = 999")
        await conn.rollback()
        with pytest.raises(Exception):
            await conn.execute("DELETE FROM journals")
        await conn.rollback()
    assert "append-only" in str(exc.value)


async def test_journal_pagination_no_gap_no_duplicate(client):
    for i in range(5):
        await _submit(client, trip=f"T{i}", board=f"{D}T{(8 + i * 3) % 23:02d}:00:00+08:00", fare=100)

    # Five separate groups; every new fare differs from the prior one so each
    # settlement posts a reversal (except the first) plus a charge:
    # 5 charges + 4 reversals = 9 ledger rows.
    all_rows = (await client.get("/journals?account_id=acc1&limit=500")).json()["items"]
    assert len(all_rows) == 9

    seen: set[int] = set()
    cursor = 0
    pages = 0
    while True:
        r = await client.get(f"/journals?account_id=acc1&after={cursor}&limit=2")
        page = r.json()
        seqs = [item["seq"] for item in page["items"]]
        assert len(seqs) <= 2
        assert all(s not in seen for s in seqs)  # no duplicates
        assert seqs == sorted(seqs)  # submission order
        seen.update(seqs)
        pages += 1
        if not page["has_more"]:
            break
        assert page["next_cursor"] == seqs[-1]
        cursor = page["next_cursor"]
    assert pages == 5  # ceil(9 / 2)
    assert len(seen) == 9


async def test_naive_board_time_interpreted_as_beijing(client):
    r = await client.post(
        "/trips",
        json={"account_id": "acc1", "trip_no": "T", "board_time": "2026-10-10T03:30:00", "base_fare": 100},
    )
    assert r.status_code == 201, r.text
    assert r.json()["trip"]["op_date"] == "2026-10-09"


async def test_correction_same_content_same_version_is_noop(client):
    await _submit(client, trip="A", fare=200)
    r = await client.post(
        "/trips/A/correct",
        json={"expected_version": 1, "board_time": f"{D}T08:00:00+08:00", "base_fare": 200},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["idempotent"] is True
    assert body["trip"]["version"] == 1


async def test_accounts_are_settled_independently(client):
    await _submit(client, account="a", trip="A1", fare=1500)
    await _submit(client, account="b", trip="B1", fare=1500)
    await _submit(client, account="b", trip="B2", board=f"{D}T12:00:00+08:00", fare=1500)
    day_a = (await client.get(f"/accounts/a/operating-days/{D}")).json()
    day_b = (await client.get(f"/accounts/b/operating-days/{D}")).json()
    assert day_a["charged_fare"] == 1500
    assert day_b["charged_fare"] == 2000


async def test_empty_day_query(client):
    day = (await client.get(f"/accounts/ghost/operating-days/{D}")).json()
    assert day["status"] == "empty"
    assert day["charged_fare"] == 0
    assert day["groups"] == []
