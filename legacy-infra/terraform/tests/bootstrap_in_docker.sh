#!/usr/bin/env bash
# Prove the EC2 bootstrap works without AWS: render user_data with Terraform, run it twice
# (idempotency) in an Amazon Linux 2023 container, then smoke-test the app through nginx.
# Usage: legacy-infra/terraform/tests/bootstrap_in_docker.sh   (PLATFORM=linux/arm64 for speed)
set -euo pipefail

TF_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE=public.ecr.aws/amazonlinux/amazonlinux:2023
PLATFORM="${PLATFORM:-linux/amd64}" # t3.large is x86_64
PORT="${PORT:-8088}"
NAME=legacy-bootstrap-test
WORK="$(mktemp -d)"
URL="http://127.0.0.1:$PORT"

cleanup() { [[ "${KEEP:-0}" == 1 ]] || docker rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

fail() { echo "FAIL: $*"; docker exec "$NAME" tail -n 30 /var/log/legacy-bootstrap.log || true; exit 1; }

echo "==> Rendering user_data with Terraform"
terraform -chdir="$TF_DIR" init -input=false -backend=false >/dev/null
echo 'base64encode(local.user_data)' | terraform -chdir="$TF_DIR" console | tr -d '"\n' | base64 -d > "$WORK/user_data.sh"
echo "    $(wc -c < "$WORK/user_data.sh" | tr -d ' ') bytes (EC2 limit: 16384)"

echo "==> Starting $IMAGE ($PLATFORM) on port $PORT"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --init --name "$NAME" --platform "$PLATFORM" -p "$PORT:80" \
  -v "$WORK/user_data.sh:/user_data.sh:ro" "$IMAGE" sleep infinity >/dev/null
# Packages the real AMI already ships (cloud-init also brings the rpm Jinja2/MarkupSafe that pip shadows).
docker exec "$NAME" dnf install -y -q cloud-init gzip procps-ng >/dev/null

echo "==> Running the bootstrap (1st run)"
docker exec "$NAME" bash /user_data.sh >/dev/null || fail "bootstrap run 1"
echo "==> Running the bootstrap again (idempotency)"
docker exec "$NAME" bash /user_data.sh >/dev/null || fail "bootstrap run 2"

echo "==> Smoke tests through nginx at $URL"
check() {  # check <description> <expected> <actual>
  if [[ "$3" == *"$2"* ]]; then echo "    ok   $1"; else fail "$1: expected '$2', got '$3'"; fi
}
check "GET /status" "OK" "$(curl -sf "$URL/status")"
check "GET / renders the dashboard" "Legacy Inventory" "$(curl -sf "$URL/")"
check "GET /api/items returns the seed" "SKU-1001" "$(curl -sf "$URL/api/items")"
check "POST /api/orders -> 201" "201" "$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -d '{"item_id": 1, "quantity": 2}' "$URL/api/orders")"
check "stock decremented" '"quantity":1198' "$(curl -sf "$URL/api/items/1" | tr -d ' ')"
check "oversell rejected -> 409" "409" "$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  -H 'Content-Type: application/json' -d '{"item_id": 5, "quantity": 999}' "$URL/api/orders")"
check "GET /reports/daily writes a CSV" "report written to /opt/legacy-inventory/reports" "$(curl -sf "$URL/reports/daily")"
check "cron job installed" "/reports/daily" "$(docker exec "$NAME" cat /etc/cron.d/legacy-inventory)"
check "app runs as root" "root" "$(docker exec "$NAME" ps -o user= -p "$(docker exec "$NAME" pgrep -f 'python3 /opt/legacy-inventory/app/app.py' | head -1)")"
check "file logging" "order" "$(docker exec "$NAME" cat /var/log/legacy-inventory/app.log)"
check "nginx config valid" "successful" "$(docker exec "$NAME" nginx -t 2>&1)"
check "bootstrap log" "bootstrap complete" "$(docker exec "$NAME" cat /var/log/legacy-bootstrap.log)"
echo "PASS: the bootstrap works on Amazon Linux 2023 (set KEEP=1 to keep the container)"
