#!/usr/bin/env bash
# Create (or reuse) the local kind cluster used by the k8s_validation agent's live deploy.
# Idempotent and safe to re-run. Env: KIND_CLUSTER, METRICS_SERVER=0 to skip, PREPULL_IMAGES, PRELOAD_IMAGES.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLUSTER="${KIND_CLUSTER:-transform-workshop}"
CONTEXT="kind-$CLUSTER"
METRICS_SERVER_VERSION="v0.9.0"
# Base images pulled on the host so `docker build` of the microservice is fast on stage.
PREPULL_IMAGES="${PREPULL_IMAGES:-python:3.12-slim}"
# Images loaded into the node so pods start without pulling (e.g. an in-cluster dev database).
PRELOAD_IMAGES="${PRELOAD_IMAGES:-}"

KIND="$ROOT/.tools/bin/kind"
[[ -x "$KIND" ]] || KIND="$(command -v kind || true)"
[[ -n "$KIND" ]] || { echo "kind not found: run scripts/install-tools.sh first" >&2; exit 1; }
command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker is not running" >&2; exit 1; }

if "$KIND" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "kind cluster '$CLUSTER' already exists"
  "$KIND" export kubeconfig --name "$CLUSTER" >/dev/null
else
  echo "creating kind cluster '$CLUSTER'"
  "$KIND" create cluster --name "$CLUSTER" --config "$ROOT/scripts/kind-config.yaml" --wait 120s
fi
kubectl --context "$CONTEXT" wait --for=condition=Ready nodes --all --timeout=120s >/dev/null

for image in $PREPULL_IMAGES; do
  docker image inspect "$image" >/dev/null 2>&1 || docker pull -q "$image"
done
for image in $PRELOAD_IMAGES; do
  docker image inspect "$image" >/dev/null 2>&1 || docker pull -q "$image"
  "$KIND" load docker-image "$image" --name "$CLUSTER"
done

# metrics-server so the HPA reports CPU; kind kubelets use self-signed certs, hence --kubelet-insecure-tls.
if [[ "${METRICS_SERVER:-1}" == "1" ]]; then
  if ! kubectl --context "$CONTEXT" -n kube-system get deployment metrics-server >/dev/null 2>&1; then
    echo "installing metrics-server $METRICS_SERVER_VERSION"
    if kubectl --context "$CONTEXT" apply -f \
      "https://github.com/kubernetes-sigs/metrics-server/releases/download/$METRICS_SERVER_VERSION/components.yaml" >/dev/null; then
      kubectl --context "$CONTEXT" -n kube-system patch deployment metrics-server --type=json \
        -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' >/dev/null
    else
      echo "warning: metrics-server install failed (HPA will show <unknown> targets)" >&2
    fi
  fi
fi

echo "cluster ready: kubectl --context $CONTEXT get nodes"
kubectl --context "$CONTEXT" get nodes
