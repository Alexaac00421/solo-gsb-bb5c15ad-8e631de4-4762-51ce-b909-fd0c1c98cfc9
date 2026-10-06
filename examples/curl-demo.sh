#!/usr/bin/env bash
# End-to-end call examples for the bus transfer fare clearing service.
#
# Prerequisites: service is up (docker compose up -d --build).
# Override BASE_URL / DAY if needed.
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
ACCOUNT="${ACCOUNT:-acct-demo}"
DAY="${DAY:-2026-10-10}"

j() { python3 -m json.tool 2>/dev/null || cat; }

echo ">>> 1) health"
curl -fsS "$BASE_URL/health"; echo

echo ">>> 2) submit leg A (08:00, 200 cents)"
curl -fsS -X POST "$BASE_URL/trips" -H 'Content-Type: application/json' \
  -d "{\"account_id\":\"$ACCOUNT\",\"trip_no\":\"A001\",\"board_time\":\"${DAY}T08:00:00+08:00\",\"base_fare\":200}" | j

echo ">>> 3) submit leg B (09:00, 500 cents) -> same transfer group, group fare 500"
curl -fsS -X POST "$BASE_URL/trips" -H 'Content-Type: application/json' \
  -d "{\"account_id\":\"$ACCOUNT\",\"trip_no\":\"A002\",\"board_time\":\"${DAY}T09:00:00+08:00\",\"base_fare\":500}" | j

echo ">>> 4) identical retransmission (idempotent, HTTP 200, no new journal)"
curl -fsS -o /dev/null -w "http=%{http_code}\n" -X POST "$BASE_URL/trips" \
  -H 'Content-Type: application/json' \
  -d "{\"account_id\":\"$ACCOUNT\",\"trip_no\":\"A001\",\"board_time\":\"${DAY}T08:00:00+08:00\",\"base_fare\":200}"

echo ">>> 5) conflicting content (same trip_no, different fare) -> 409"
curl -s -o /dev/null -w "http=%{http_code}\n" -X POST "$BASE_URL/trips" \
  -H 'Content-Type: application/json' \
  -d "{\"account_id\":\"$ACCOUNT\",\"trip_no\":\"A001\",\"board_time\":\"${DAY}T08:00:00+08:00\",\"base_fare\":999}"

echo ">>> 6) late leg C (12:00, 1500 cents) and D (14:00, 1500 cents)"
curl -fsS -X POST "$BASE_URL/trips" -H 'Content-Type: application/json' \
  -d "{\"account_id\":\"$ACCOUNT\",\"trip_no\":\"A003\",\"board_time\":\"${DAY}T12:00:00+08:00\",\"base_fare\":1500}" >/dev/null
curl -fsS -X POST "$BASE_URL/trips" -H 'Content-Type: application/json' \
  -d "{\"account_id\":\"$ACCOUNT\",\"trip_no\":\"A004\",\"board_time\":\"${DAY}T14:00:00+08:00\",\"base_fare\":1500}" >/dev/null

echo ">>> 7) query operating day: raw 3500, capped charged 2000"
curl -fsS "$BASE_URL/accounts/$ACCOUNT/operating-days/$DAY" | j

echo ">>> 8) correction across the 04:00 boundary (03:30 day D -> 05:00 day D)"
curl -fsS -X POST "$BASE_URL/trips" -H 'Content-Type: application/json' \
  -d "{\"account_id\":\"$ACCOUNT\",\"trip_no\":\"X1\",\"board_time\":\"${DAY}T03:30:00+08:00\",\"base_fare\":800}" >/dev/null
curl -fsS -X POST "$BASE_URL/trips/X1/correct" -H 'Content-Type: application/json' \
  -d "{\"expected_version\":1,\"board_time\":\"${DAY}T05:00:00+08:00\",\"base_fare\":800}" | j

echo ">>> 9) stale version -> 409"
curl -s -o /dev/null -w "http=%{http_code}\n" -X POST "$BASE_URL/trips/X1/correct" \
  -H 'Content-Type: application/json' \
  -d "{\"expected_version\":1,\"board_time\":\"${DAY}T06:00:00+08:00\",\"base_fare\":1}"

echo ">>> 10) revoke X1 with current version 2"
curl -fsS -X POST "$BASE_URL/trips/X1/revoke" -H 'Content-Type: application/json' \
  -d '{"expected_version":2}' | j

echo ">>> 11) walk the ledger with cursors (no gaps, no duplicates)"
cursor=0
while :; do
  resp=$(curl -fsS "$BASE_URL/journals?account_id=$ACCOUNT&after=$cursor&limit=2")
  echo "$resp" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('page seqs:', [i['seq'] for i in d['items']], 'has_more:', d['has_more'])
"
  has_more=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['has_more'])")
  cursor=$(echo "$resp" | python3 -c "import json,sys; print(json.load(sys.stdin)['next_cursor'] or 0)")
  [ "$has_more" = "True" ] || break
done
