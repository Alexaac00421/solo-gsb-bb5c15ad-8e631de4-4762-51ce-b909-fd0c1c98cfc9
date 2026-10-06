"""Call examples for the fare-clearing API (stdlib only, no dependencies).

Usage:
    python examples/api_examples.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ACCOUNT = "acct-demo"
DAY = "2026-10-10"


def call(method: str, path: str, payload: dict | None = None, *, expect: int | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode())
        status = exc.code
    marker = "OK " if expect is None or status == expect else "!! "
    print(f"{marker}{method} {path} -> {status}")
    print(json.dumps(body, ensure_ascii=False, indent=2, default=str))
    return status, body


def main() -> None:
    call("GET", "/health")

    # Submit two legs 60 minutes apart -> one transfer group, fare max(200,500).
    call("POST", "/trips", {
        "account_id": ACCOUNT, "trip_no": "A001",
        "board_time": f"{DAY}T08:00:00+08:00", "base_fare": 200,
    }, expect=201)
    call("POST", "/trips", {
        "account_id": ACCOUNT, "trip_no": "A002",
        "board_time": f"{DAY}T09:00:00+08:00", "base_fare": 500,
    }, expect=201)

    # Identical replay -> idempotent 200.
    call("POST", "/trips", {
        "account_id": ACCOUNT, "trip_no": "A001",
        "board_time": f"{DAY}T08:00:00+08:00", "base_fare": 200,
    }, expect=200)

    # Different content with same trip_no -> 409.
    call("POST", "/trips", {
        "account_id": ACCOUNT, "trip_no": "A001",
        "board_time": f"{DAY}T08:00:00+08:00", "base_fare": 999,
    }, expect=409)

    # Two more separated legs -> raw 3500, daily cap 2000.
    for trip_no, when, fare in [
        ("A003", "12:00", 1500),
        ("A004", "14:00", 1500),
    ]:
        call("POST", "/trips", {
            "account_id": ACCOUNT, "trip_no": trip_no,
            "board_time": f"{DAY}T{when}:00+08:00", "base_fare": fare,
        }, expect=201)

    call("GET", f"/accounts/{ACCOUNT}/operating-days/{DAY}")

    # Cross-boundary correction: 03:30 belongs to the previous operating day.
    call("POST", "/trips", {
        "account_id": ACCOUNT, "trip_no": "X1",
        "board_time": f"{DAY}T03:30:00+08:00", "base_fare": 800,
    }, expect=201)
    call("POST", "/trips/X1/correct", {
        "expected_version": 1,
        "board_time": f"{DAY}T05:00:00+08:00",
        "base_fare": 800,
    }, expect=200)

    # Stale version -> 409.
    call("POST", "/trips/X1/correct", {
        "expected_version": 1,
        "board_time": f"{DAY}T06:00:00+08:00",
        "base_fare": 1,
    }, expect=409)

    # Revoke with the current version.
    call("POST", "/trips/X1/revoke", {"expected_version": 2}, expect=200)

    # Walk the ledger.
    cursor = 0
    while True:
        _, page = call("GET", f"/journals?account_id={ACCOUNT}&after={cursor}&limit=2")
        cursor = page["next_cursor"] or 0
        if not page["has_more"]:
            break


if __name__ == "__main__":
    main()
