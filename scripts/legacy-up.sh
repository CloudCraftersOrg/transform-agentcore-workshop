#!/usr/bin/env bash
# Deploy the legacy estate (one EC2 instance running the Flask monolith) and wait until it answers.
#
# Usage: scripts/legacy-up.sh
# Env:   AWS_PROFILE, AWS_REGION (default us-west-2)
#        ALLOWED_CIDR=203.0.113.10/32   restrict port 80 (default 0.0.0.0/0)
#        AUTO_APPROVE=1                 skip the confirmation prompt
#        VERBOSE=1                      print the full terraform plan
set -euo pipefail

TF_DIR="$(cd "$(dirname "$0")/../legacy-infra/terraform" && pwd)"
export AWS_REGION="${AWS_REGION:-us-west-2}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"
if [[ -n "${ALLOWED_CIDR:-}" ]]; then
  export TF_VAR_allowed_cidr="$ALLOWED_CIDR"
fi

tf() { terraform -chdir="$TF_DIR" "$@"; }

PLAN_DIR="$(mktemp -d)"
trap 'rm -rf "$PLAN_DIR"' EXIT

echo "==> AWS identity (profile: ${AWS_PROFILE:-default}, region: $AWS_REGION)"
aws sts get-caller-identity --query Arn --output text

echo "==> terraform init"
tf init -input=false >/dev/null

echo "==> terraform plan"
tf plan -input=false -no-color -out="$PLAN_DIR/tfplan" > "$PLAN_DIR/plan.txt"
if [[ "${VERBOSE:-0}" == 1 ]]; then
  cat "$PLAN_DIR/plan.txt"
else
  grep -E '^  # |^Plan:|^No changes' "$PLAN_DIR/plan.txt" || true
fi
echo "    port 80 open to: ${TF_VAR_allowed_cidr:-0.0.0.0/0}"

if grep -q '^No changes' "$PLAN_DIR/plan.txt"; then
  echo "==> Already deployed"
else
  if [[ "${AUTO_APPROVE:-0}" != 1 ]]; then
    read -r -p "Create these AWS resources (about USD 2.25 per day)? [y/N] " answer
    [[ "$answer" =~ ^[Yy] ]] || { echo "Aborted, nothing was created."; exit 1; }
  fi
  echo "==> terraform apply"
  tf apply -input=false "$PLAN_DIR/tfplan"
fi

url="$(tf output -raw app_url)"
echo "==> Waiting for $url (first boot installs packages: about 2-4 minutes)"
deadline=$((SECONDS + TIMEOUT_SECONDS))
until curl -sf --max-time 5 "$url/api/items" >/dev/null; do
  if (( SECONDS > deadline )); then
    echo
    echo "Timed out after ${TIMEOUT_SECONDS}s. Check the bootstrap log on the instance:"
    echo "  $(tf output -raw ssm_session_command)"
    echo "  sudo tail -n 50 /var/log/legacy-bootstrap.log"
    exit 1
  fi
  printf '.'
  sleep 10
done

echo
echo "==> The legacy app is up"
echo "    URL:         $url"
echo "    Instance:    $(tf output -raw instance_id) ($(tf output -raw instance_type), $(tf output -raw availability_zone))"
echo "    Shell:       $(tf output -raw ssm_session_command)"
echo "    Smoke test:  $(tf output -raw smoke_test)"
echo "    Load:        scripts/legacy-load.sh $url 5"
