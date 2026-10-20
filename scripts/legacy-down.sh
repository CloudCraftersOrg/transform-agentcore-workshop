#!/usr/bin/env bash
# Destroy the legacy estate created by scripts/legacy-up.sh.
#
# Usage: scripts/legacy-down.sh        (Terraform asks you to type "yes")
#        AUTO_APPROVE=1 scripts/legacy-down.sh
set -euo pipefail

TF_DIR="$(cd "$(dirname "$0")/../legacy-infra/terraform" && pwd)"
export AWS_REGION="${AWS_REGION:-us-west-2}"

tf() { terraform -chdir="$TF_DIR" "$@"; }

if [[ ! -s "$TF_DIR/terraform.tfstate" ]]; then
  echo "No state in $TF_DIR: nothing to destroy."
  exit 0
fi

echo "==> Destroying the legacy estate (profile: ${AWS_PROFILE:-default}, region: $AWS_REGION)"
tf init -input=false >/dev/null
if [[ "${AUTO_APPROVE:-0}" == 1 ]]; then
  tf destroy -input=false -auto-approve
else
  tf destroy
fi
echo "==> Done. Nothing from the legacy estate is left running."
