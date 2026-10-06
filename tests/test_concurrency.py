"""Concurrency and edge-case tests."""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio

D = "2026-10-10"


async def test_concurrent_submissions_to_same_account_settle_consistently(client):
    async def submit(i: int, hour: int):
        return await client.post(
            "/trips",
            json={
                "account_id": "acc1",
                "trip_no": f"C{i}",
                "board_time": f"{D}T{hour:02d}:00:00+08:00",
                "base_fare": 100,
            },
        )

    # 8 legs spread across the day (>90 min apart) => 8 independent groups.
    results = await asyncio.gather(*(submit(i, 7 + 2 * i) for i in range(8)))
    assert all(r.status_code in (200, 201) for r in results), [r.text for r in results]

    day = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert day["trip_count"] == 8
    assert len(day["groups"]) == 8
    assert day["raw_fare"] == 800

    # Final ledger net movement must equal the last charged amount exactly.
    journals = (await client.get("/journals?account_id=acc1&limit=500")).json()["items"]
    assert sum(j["amount"] for j in journals) == day["charged_fare"]


async def test_concurrent_identical_replays_idempotent(client):
    payload = {
        "account_id": "acc1",
        "trip_no": "ONLY",
        "board_time": f"{D}T08:00:00+08:00",
        "base_fare": 300,
    }

    async def post():
        return await client.post("/trips", json=payload)

    results = await asyncio.gather(*(post() for _ in range(6)))
    created = [r for r in results if r.status_code == 201]
    replays = [r for r in results if r.status_code == 200]
    assert len(created) == 1
    assert len(replays) == 5
    assert all(r.json()["idempotent"] for r in replays)

    journals = (await client.get("/journals?account_id=acc1")).json()["items"]
    assert len(journals) == 1


async def test_no_journal_when_cap_keeps_binding(client):
    await client.post("/trips", json={
        "account_id": "acc1", "trip_no": "A",
        "board_time": f"{D}T08:00:00+08:00", "base_fare": 2500,
    })
    await client.post("/trips", json={
        "account_id": "acc1", "trip_no": "B",
        "board_time": f"{D}T12:00:00+08:00", "base_fare": 3000,
    })
    day = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert day["raw_fare"] == 5500
    assert day["charged_fare"] == 2000
    journals = (await client.get("/journals?account_id=acc1")).json()["items"]
    # First day charges 2000 (cap); second recalculation still charges 2000,
    # so the capped amount is unchanged and no reversal/charge is added.
    assert [(j["entry_type"], j["amount"]) for j in journals] == [("charge", 2000)]


async def test_zero_fares_emit_no_money_journals(client):
    for i in range(3):
        r = await client.post("/trips", json={
            "account_id": "acc1", "trip_no": f"Z{i}",
            "board_time": f"{D}T{8 + i:02d}:00:00+08:00", "base_fare": 0,
        })
        assert r.status_code == 201
    journals = (await client.get("/journals?account_id=acc1")).json()["items"]
    assert journals == []
    day = (await client.get(f"/accounts/acc1/operating-days/{D}")).json()
    assert day["charged_fare"] == 0
    assert len(day["groups"]) == 1  # 60-min gaps, all free legs in one group
