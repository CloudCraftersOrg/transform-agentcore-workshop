#!/usr/bin/env bash
# Pre-stage checks for the workshop: credentials, Bedrock models, docker, kind, tools and cache warm-ups.
# Run `make preflight` (or `bash scripts/preflight.sh`) ~15 minutes before the talk.
# Exit code: 0 when every hard check passes (warnings and info lines never fail), 1 otherwise.
# Env: AWS_PROFILE (optional, used only when set), AWS_REGION (default us-west-2), KIND_CLUSTER, NO_COLOR.
# Compatible with macOS /bin/bash 3.2.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export AWS_REGION="${AWS_REGION:-us-west-2}"
CLUSTER="${KIND_CLUSTER:-transform-workshop}"
CONTEXT="kind-$CLUSTER"
BIN="$ROOT/.tools/bin"
PROFILE_HINT="${AWS_PROFILE:-<your-profile>}"
UV="${UV:-uv}"

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; BLUE=$'\033[36m'; DIM=$'\033[2m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; YELLOW=""; BLUE=""; DIM=""; BOLD=""; RESET=""
fi

PASSED=0; WARNED=0; FAILED=0; INFOS=0
FAILURES=""
START_TOTAL=$(date +%s)
CHECK_START=$START_TOTAL

begin() { CHECK_START=$(date +%s); printf '%s▸ %s%s\n' "$BOLD" "$1" "$RESET"; }
elapsed() { echo "$(( $(date +%s) - CHECK_START ))s"; }
ok() { PASSED=$((PASSED + 1)); printf '  ✅ %s%s%s %s(%s)%s\n' "$GREEN" "$1" "$RESET" "$DIM" "$(elapsed)" "$RESET"; }
warn() { WARNED=$((WARNED + 1)); printf '  ⚠️  %s%s%s\n' "$YELLOW" "$1" "$RESET"; }
info() { INFOS=$((INFOS + 1)); printf '  ℹ️  %s%s%s\n' "$BLUE" "$1" "$RESET"; }
fail() { FAILED=$((FAILED + 1)); FAILURES="$FAILURES\n    - $1"; printf '  ❌ %s%s%s\n' "$RED" "$1" "$RESET"; }
hint() { printf '     %s→ %s%s\n' "$DIM" "$1" "$RESET"; }
indent() { sed 's/^/     /'; }

echo "${BOLD}Transform × AgentCore preflight${RESET}  (AWS_PROFILE=${AWS_PROFILE:-<unset: default credential chain>}, AWS_REGION=$AWS_REGION)"
echo

# --------------------------------------------------------------------------- AWS credentials
begin "AWS credentials"
ident=""
CREDS_OK=0
if command -v aws >/dev/null 2>&1; then
  if ident="$(aws sts get-caller-identity --output text --query '[Account,Arn]' 2>&1)"; then
    CREDS_OK=1; ok "signed in: $(echo "$ident" | tr '\t' ' ')"
  else
    fail "aws sts get-caller-identity failed: $(echo "$ident" | tail -1)"
    hint "aws sso login --profile $PROFILE_HINT   (then: export AWS_PROFILE=$PROFILE_HINT)"
  fi
else
  if ident="$("$UV" run --quiet python -c 'import boto3; i = boto3.client("sts").get_caller_identity(); print(i["Account"], i["Arn"])' 2>&1)"; then
    CREDS_OK=1; ok "signed in (boto3; aws CLI not installed): $(echo "$ident" | tail -1)"
  else
    fail "STS get-caller-identity failed (aws CLI not installed, boto3 fallback): $(echo "$ident" | tail -1)"
    hint "aws sso login --profile $PROFILE_HINT   (then: export AWS_PROFILE=$PROFILE_HINT)"
  fi
fi

# --------------------------------------------------------------------------- Bedrock models
begin "Bedrock model access (one tiny Converse call per distinct model)"
BEDROCK_PY='
import sys, time
import boto3
from botocore.config import Config
from transform_agents.core.config import STAGES, model_id_for, region
models = []
for agent in STAGES:
    m = model_id_for(agent)
    if m not in models:
        models.append(m)
client = boto3.client("bedrock-runtime", region_name=region(),
                      config=Config(read_timeout=60, connect_timeout=10, retries={"max_attempts": 2}))
bad = 0
for m in models:
    users = ",".join(a for a in STAGES if model_id_for(a) == m)
    t = time.time()
    try:
        client.converse(modelId=m, messages=[{"role": "user", "content": [{"text": "Reply with OK."}]}],
                        inferenceConfig={"maxTokens": 5})
        print(f"OK\t{m}\t{time.time() - t:.1f}s\t{users}")
    except Exception as exc:
        bad += 1
        print(f"FAIL\t{m}\t{type(exc).__name__}: {str(exc)[:160]}\t{users}")
sys.exit(1 if bad else 0)
'
if [[ "$CREDS_OK" -eq 0 ]]; then
  bedrock_out=""; bedrock_rc=0
else
  bedrock_out="$("$UV" run --quiet python -c "$BEDROCK_PY" 2>&1)" && bedrock_rc=0 || bedrock_rc=$?
fi
if [[ "$CREDS_OK" -eq 0 ]]; then
  fail "Bedrock models not checked: no valid AWS credentials (see above)"
  hint "aws sso login --profile $PROFILE_HINT, then re-run make preflight"
elif echo "$bedrock_out" | grep -qE '^(OK|FAIL)'; then
  while IFS="$(printf '\t')" read -r verdict model detail users; do
    case "$verdict" in
      OK) ok "$model responds in $detail  [$users]" ;;
      FAIL) fail "$model: $detail  [$users]"
            if echo "$detail" | grep -qiE 'token|credential|expired|sso'; then
              hint "aws sso login --profile $PROFILE_HINT"
            else
              hint "enable model access in the Bedrock console ($AWS_REGION) or override with MODEL_<AGENT>=..."
            fi ;;
    esac
  done <<EOF
$(echo "$bedrock_out" | grep -E '^(OK|FAIL)')
EOF
else
  fail "could not run the Bedrock check (exit $bedrock_rc): $(echo "$bedrock_out" | tail -1)"
  hint "make install   (and check the AWS credentials above)"
fi

# --------------------------------------------------------------------------- docker
begin "Docker daemon"
if ! command -v docker >/dev/null 2>&1; then
  fail "docker not installed (needed to build the image and run kind)"
  hint "install Docker Desktop / OrbStack / colima"
elif docker_ver="$(docker info --format '{{.ServerVersion}}' 2>&1)"; then
  ok "docker daemon running (server $docker_ver)"
else
  fail "docker daemon not reachable: $(echo "$docker_ver" | tail -1)"
  hint "start Docker Desktop (or colima start) and re-run"
fi

# --------------------------------------------------------------------------- repo-local tools
begin "Repo-local tools (.tools/bin)"
for tool in kind kubeconform; do
  if [[ -x "$BIN/$tool" ]]; then
    case "$tool" in
      kind) ver="$("$BIN/kind" version 2>/dev/null | awk '{print $2}' || true)" ;;
      kubeconform) ver="$("$BIN/kubeconform" -v 2>/dev/null || true)" ;;
    esac
    ok "$tool ${ver:-?}"
  else
    fail "$BIN/$tool missing"
    hint "make tools"
  fi
done

# --------------------------------------------------------------------------- kind cluster
begin "kind cluster '$CLUSTER'"
if [[ ! -x "$BIN/kind" ]]; then
  fail "cannot check the cluster without kind"
  hint "make tools && make kind-up"
elif ! "$BIN/kind" get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  fail "kind cluster '$CLUSTER' does not exist"
  hint "make kind-up"
else
  KUBECTL="$(command -v kubectl 2>/dev/null || true)"
  [[ -z "$KUBECTL" && -x "$BIN/kubectl" ]] && KUBECTL="$BIN/kubectl"
  if [[ -n "$KUBECTL" ]]; then
    if nodes="$("$KUBECTL" --context "$CONTEXT" get nodes --no-headers --request-timeout=10s 2>&1)"; then
      total="$(echo "$nodes" | grep -c . || true)"
      ready="$(echo "$nodes" | awk '$2 == "Ready"' | grep -c . || true)"
      if [[ "$total" -gt 0 && "$ready" -eq "$total" ]]; then
        ok "cluster exists, $ready/$total node(s) Ready (context $CONTEXT)"
      else
        fail "cluster exists but only $ready/$total node(s) Ready"
        echo "$nodes" | indent
        hint "make kind-up   (idempotent: waits for the node), or make kind-down && make kind-up"
      fi
    else
      fail "kubectl --context $CONTEXT get nodes failed: $(echo "$nodes" | tail -1)"
      hint "make kind-up   (re-exports the kubeconfig)"
    fi
  else
    running="$(docker inspect -f '{{.State.Running}}' "$CLUSTER-control-plane" 2>/dev/null || echo false)"
    if [[ "$running" == "true" ]]; then
      ok "cluster exists, control-plane container running"
      warn "kubectl not found: could not confirm the node is Ready (live deploy needs kubectl)"
    else
      fail "cluster exists but the $CLUSTER-control-plane container is not running"
      hint "make kind-down && make kind-up"
    fi
  fi
fi

# --------------------------------------------------------------------------- terraform
begin "Terraform binary + module/provider warm-up"
if command -v terraform >/dev/null 2>&1; then
  tf_ver="$(terraform version 2>/dev/null | head -1 || true)"
  if warm="$("$UV" run --quiet python -m transform_agents.terraform.tools --warmup 2>&1)"; then
    if echo "$warm" | grep -qE '^warm run: VALID'; then
      ok "$tf_ver; blueprint $(echo "$warm" | grep -E '^warm run:' | head -1); $(echo "$warm" | tail -1)"
    else
      warn "$tf_ver found, but the blueprint warm-up did not validate (the agent falls back to offline layers):"
      echo "$warm" | tail -6 | indent
    fi
  else
    warn "$tf_ver found, but the warm-up exited non-zero:"
    echo "$warm" | tail -4 | indent
  fi
else
  warn "terraform not installed: the Terraform agent will validate offline only (layers 1 and 2)"
  hint "brew install hashicorp/tap/terraform"
fi

# --------------------------------------------------------------------------- modernization test env
begin "Modernization test environment prewarm"
PREWARM_PY='
import sys
from transform_agents.modernization import checks
fn = getattr(checks, "prewarm_test_env", None)
if fn is None:
    print("MISSING")
    sys.exit(0)
print("OK" if fn() else "FAILED")
'
prewarm="$("$UV" run --quiet python -c "$PREWARM_PY" 2>&1 || true)"
case "$(echo "$prewarm" | tail -1)" in
  OK) ok "microservice test env resolved (first pytest run on stage will be fast)" ;;
  MISSING) warn "transform_agents.modernization.checks.prewarm_test_env() not found: skipped" ;;
  FAILED) warn "prewarm_test_env() returned False (uv missing or dependency resolution failed; tests run cold)" ;;
  *) warn "prewarm failed: $(echo "$prewarm" | tail -1)" ;;
esac

# --------------------------------------------------------------------------- kubeconform cache
begin "kubeconform schema cache warm-up"
if [[ -x "$BIN/kubeconform" ]]; then
  mkdir -p "$ROOT/.tools/kubeconform-cache"
  manifest='apiVersion: v1
kind: Namespace
metadata: {name: preflight}
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: preflight, namespace: preflight}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: preflight, namespace: preflight}
data: {LOG_LEVEL: info}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: preflight, namespace: preflight}
spec:
  selector: {matchLabels: {app: preflight}}
  template:
    metadata: {labels: {app: preflight}}
    spec:
      containers:
        - name: app
          image: inventory-service:dev
          ports: [{containerPort: 8080}]
---
apiVersion: v1
kind: Service
metadata: {name: preflight, namespace: preflight}
spec:
  selector: {app: preflight}
  ports: [{port: 80, targetPort: 8080}]
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: {name: preflight, namespace: preflight}
spec:
  scaleTargetRef: {apiVersion: apps/v1, kind: Deployment, name: preflight}
  minReplicas: 2
  maxReplicas: 6
  metrics:
    - type: Resource
      resource: {name: cpu, target: {type: Utilization, averageUtilization: 70}}
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata: {name: preflight, namespace: preflight}
spec:
  minAvailable: 1
  selector: {matchLabels: {app: preflight}}
---
apiVersion: batch/v1
kind: CronJob
metadata: {name: preflight, namespace: preflight}
spec:
  schedule: "0 2 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: report
              image: inventory-service:dev
'
  if kc="$(echo "$manifest" | "$BIN/kubeconform" -strict -summary -ignore-missing-schemas \
             -cache "$ROOT/.tools/kubeconform-cache" - 2>&1)"; then
    ok "schemas cached for 8 kinds: $(echo "$kc" | tail -1)"
  else
    warn "kubeconform warm-up failed (agent falls back to builtin checks): $(echo "$kc" | tail -1)"
  fi
else
  warn "kubeconform missing: skipped the schema cache warm-up (make tools)"
fi

# --------------------------------------------------------------------------- optional: AgentCore
begin "AgentCore Runtime mode (optional)"
if [[ -f deploy/agent-arns.json ]]; then
  arns="$("$UV" run --quiet python -c '
import json
from transform_agents.core.config import STAGES
arns = json.load(open("deploy/agent-arns.json"))
print(sum(1 for s in STAGES if arns.get(s)), len(STAGES))
' 2>/dev/null || echo "? 6")"
  info "deploy/agent-arns.json: ${arns% *}/${arns#* } stage runtime ARNs (AgentCore mode ready when 6/6)"
else
  info "AgentCore mode not deployed (no deploy/agent-arns.json): run make deploy-agents"
fi

# --------------------------------------------------------------------------- optional: legacy EC2
begin "Legacy EC2 estate (optional)"
TF_LEGACY="legacy-infra/terraform"
if [[ -s "$TF_LEGACY/terraform.tfstate" ]] && grep -q '"resources": \[$' "$TF_LEGACY/terraform.tfstate" 2>/dev/null \
   && command -v terraform >/dev/null 2>&1; then
  if url="$(terraform -chdir="$TF_LEGACY" output -raw app_url 2>/dev/null)" && [[ -n "$url" ]]; then
    if curl -fsS --max-time 5 -o /dev/null "$url/status" 2>/dev/null || curl -fsS --max-time 5 -o /dev/null "$url" 2>/dev/null; then
      info "legacy app reachable at $url (live discovery: Live AWS toggle / --live-aws)"
    else
      info "legacy state found but $url did not answer within 5s (discovery still works from Terraform)"
    fi
  else
    info "legacy state found but no app_url output"
  fi
else
  info "legacy EC2 not deployed (no state in $TF_LEGACY): discovery uses the Terraform files; make legacy-up to deploy"
fi

# --------------------------------------------------------------------------- disk space
begin "Free disk space"
free_kb="$(df -Pk "$ROOT" | awk 'NR == 2 {print $4}')"
free_gb=$((free_kb / 1024 / 1024))
if [[ "$free_kb" -lt $((1024 * 1024)) ]]; then
  fail "only ${free_gb} GB free on the repo volume (image builds and kind need space)"
  hint "docker system prune; free some disk"
elif [[ "$free_kb" -lt $((5 * 1024 * 1024)) ]]; then
  warn "${free_gb} GB free on the repo volume (under 5 GB): consider docker system prune"
else
  ok "${free_gb} GB free"
fi

# --------------------------------------------------------------------------- summary
TOTAL=$(( $(date +%s) - START_TOTAL ))
echo
echo "${BOLD}Summary${RESET}: ${GREEN}${PASSED} passed${RESET}, ${YELLOW}${WARNED} warnings${RESET}, ${RED}${FAILED} failed${RESET}, ${INFOS} info  (total ${TOTAL}s)"
if [[ "$FAILED" -gt 0 ]]; then
  printf '%s❌ Not ready for stage:%s' "$RED" "$RESET"
  printf '%b\n' "$FAILURES"
  exit 1
fi
echo "${GREEN}✅ Ready for stage.${RESET}"
