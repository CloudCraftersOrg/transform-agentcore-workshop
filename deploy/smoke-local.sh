#!/usr/bin/env bash
# Prove a deployment package is self-contained without deploying anything:
# build the exact CodeZip that `agentcore deploy` would upload, unzip it into a read-only linux/arm64
# python:3.12 container, start it the way AgentCore does (opentelemetry-instrument main.py), call GET /ping,
# check that the vendored tools (kubeconform, kubectl, uv, ...) resolve on PATH and run, then POST /invocations.
#
#   deploy/smoke-local.sh hello
#   deploy/smoke-local.sh hello '{"instruction": "List the files", "files": {"a.txt": "hi"}}'
#   NO_INVOKE=1 deploy/smoke-local.sh terraform        # /ping only (no Bedrock call)
#
# The invocation calls Bedrock with your current AWS credentials (exported into the container for this run).
# shellcheck source=deploy/_common.sh disable=SC1091
source "$(dirname "$0")/_common.sh"

[[ $# -ge 1 ]] || die "usage: deploy/smoke-local.sh <stage> [payload]"
stage="$1"; payload="${2:-List the files and report.}"
port="${SMOKE_PORT:-18080}"
image="${SMOKE_IMAGE:-python:3.12-slim}"
name="agentcore-smoke-$stage"
require docker
ensure_cli

# Runs inside the container: `import main` exposes vendor/bin exactly as at startup (copy to /tmp, chmod, PATH).
TOOLS_CHECK='
import os, shutil, subprocess, sys
import main
VERSION_ARGS = {"uv": ["--version"], "kubeconform": ["-v"], "kubectl": ["version", "--client"], "terraform": ["version"]}
missing = 0
for tool in sorted(os.listdir("vendor/bin")):
    path = shutil.which(tool)
    out = "NOT FOUND"
    if path:
        proc = subprocess.run([path, *VERSION_ARGS.get(tool, ["--version"])], capture_output=True, text=True)
        out = path + "  " + ((proc.stdout or proc.stderr).strip().splitlines() or ["?"])[0]
    missing += not path
    print("  %-12s %s" % (tool, out))
sys.exit(1 if missing else 0)
'

work="$(mktemp -d "${TMPDIR:-/tmp}/agentcore-smoke.XXXXXX")"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

log "building a scratch AgentCore project for $stage and packaging it with the AgentCore CLI (no upload)"
# shellcheck disable=SC2046  # flags are plain words
tool scratch "$stage" "$work/project" --region "$AWS_REGION" $(binary_flags)
(cd "$work/project" && "$AGENTCORE_BIN" package -d . -r "$stage")
zip="$work/project/agentcore/$stage.zip"
[[ -f "$zip" ]] || die "package not found: $zip"
python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$zip" "$work/pkg"
log "package: $(du -h "$zip" | cut -f1) zipped, $(du -sh "$work/pkg" | cut -f1) unzipped"

env_args=(-e AWS_REGION="$AWS_REGION" -e PYTHONPATH=/var/task -e OTEL_SERVICE_NAME="$stage"
  -e OTEL_PYTHON_DISTRO=aws_distro -e OTEL_PYTHON_CONFIGURATOR=aws_configurator
  -e OTEL_TRACES_EXPORTER=console -e OTEL_METRICS_EXPORTER=none -e OTEL_LOGS_EXPORTER=none)
if [[ "${NO_INVOKE:-0}" != "1" ]]; then
  (umask 077; aws configure export-credentials --format env-no-export > "$work/creds.env") \
    || die "could not export AWS credentials (aws sso login?)"
  env_args+=(--env-file "$work/creds.env")
fi

docker rm -f "$name" >/dev/null 2>&1 || true
docker run -d --name "$name" --platform linux/arm64 --read-only --tmpfs /tmp:exec \
  -v "$work/pkg:/var/task:ro" -w /var/task -p "$port:8080" "${env_args[@]}" \
  "$image" python bin/opentelemetry-instrument python main.py >/dev/null

for _ in $(seq 1 30); do
  curl -fsS -m 2 "http://localhost:$port/ping" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS -m 5 "http://localhost:$port/ping" || { docker logs "$name" | tail -40; die "/ping failed"; }
echo
log "/ping OK"
if [[ -d "$work/pkg/vendor/bin" ]]; then
  log "vendored tools, found on PATH the way main.py sets it up, and run once:"
  docker exec "$name" python -c "$TOOLS_CHECK" || die "a vendored tool is missing or does not run"
fi
if [[ "${NO_INVOKE:-0}" != "1" ]]; then
  tool invoke "$stage" "$payload" --local "http://localhost:$port"
  spans="$(docker logs "$name" 2>&1 | grep -c '"trace_id"' || true)"
  log "/invocations OK; $spans OpenTelemetry spans emitted by the instrumented package"
fi
