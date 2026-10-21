#!/usr/bin/env bash
# Mixed GET/POST traffic against the legacy app, so CloudWatch has utilization data
# and the orders table fills up. Ctrl-C stops early and still prints the summary.
#
# Usage: scripts/legacy-load.sh [URL] [MINUTES]   (URL defaults to the terraform output app_url)
# Env:   WORKERS=4   parallel clients
set -euo pipefail

TF_DIR="$(cd "$(dirname "$0")/../legacy-infra/terraform" && pwd)"
URL="${1:-$(terraform -chdir="$TF_DIR" output -raw app_url)}"
URL="${URL%/}"
MINUTES="${2:-5}"
WORKERS="${WORKERS:-4}"
END=$(( $(date +%s) + MINUTES * 60 ))
RESULTS="$(mktemp)"

# Seed stock and prices of items 1-6, used to restock (the warehouse receiving goods).
SEED_QTY=(0 1200 950 7 42 5 64)
SEED_PRICE=(0 0.35 0.12 1299.00 89.90 14.50 22.00)

hit() {  # hit <label> <curl args...>: one request, recorded as "<label> <status>"
  local label="$1" code
  shift
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" || true)"
  echo "$label $code" >> "$RESULTS"
}

worker() {
  trap 'exit 0' TERM
  local item qty
  while (( $(date +%s) < END )); do
    item=$(( RANDOM % 6 + 1 ))
    qty=$(( RANDOM % 3 + 1 ))
    case $(( RANDOM % 50 )) in
      0)       hit "GET  /reports/daily" "$URL/reports/daily" ;;
      1)       hit "PUT  /api/items/{id} (restock)" -X PUT -H 'Content-Type: application/json' \
                 -d "{\"quantity\": ${SEED_QTY[$item]}, \"price\": ${SEED_PRICE[$item]}}" "$URL/api/items/$item" ;;
      [2-9])   hit "GET  /" "$URL/" ;;
      1[0-9])  hit "GET  /api/items/{id}" "$URL/api/items/$item" ;;
      2[0-4])  hit "GET  /api/items?search=" "$URL/api/items?search=bolt" ;;
      2[5-9]|3[0-4]) hit "POST /api/orders" -X POST -H 'Content-Type: application/json' \
                 -d "{\"item_id\": $item, \"quantity\": $qty}" "$URL/api/orders" ;;
      *)       hit "GET  /api/items" "$URL/api/items" ;;
    esac
  done
}

summary() {
  pkill -P $$ 2>/dev/null || true
  echo
  echo "==> $(wc -l < "$RESULTS" | tr -d ' ') requests (count, endpoint, HTTP status)"
  sort "$RESULTS" | uniq -c | sort -rn
  rm -f "$RESULTS"
}

if ! curl -sf --max-time 5 "$URL/status" >/dev/null; then
  echo "The app is not answering at $URL/status"
  rm -f "$RESULTS"
  exit 1
fi
echo "==> $WORKERS workers against $URL for $MINUTES minute(s)"
trap summary EXIT
for _ in $(seq 1 "$WORKERS"); do
  worker &
done
while (( $(date +%s) < END )); do
  left=$(( END - $(date +%s) ))
  sleep $(( left < 15 ? left : 15 ))
  echo "    $(wc -l < "$RESULTS" | tr -d ' ') requests so far"
done
wait
