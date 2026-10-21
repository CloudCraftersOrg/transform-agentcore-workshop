#!/usr/bin/env bash
# Delete the workshop kind cluster (no-op when it does not exist).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="${KIND_CLUSTER:-transform-workshop}"
KIND="$ROOT/.tools/bin/kind"
[[ -x "$KIND" ]] || KIND="$(command -v kind || true)"
[[ -n "$KIND" ]] || { echo "kind not found: nothing to delete"; exit 0; }

if "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  "$KIND" delete cluster --name "$CLUSTER"
else
  echo "kind cluster '$CLUSTER' does not exist"
fi
