# shellcheck shell=bash
# Shared setup for deploy/*.sh (sourced, not executed).
# Region/profile: AWS_REGION (default us-west-2) and AWS_PROFILE are honoured; nothing else is hard-coded.

set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$DEPLOY_DIR/.." && pwd)"
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
export AWS_DEFAULT_REGION="$AWS_REGION"
# The CLI's anonymous usage analytics are off for the workshop; set AGENTCORE_TELEMETRY_DISABLED=0 to opt back in.
export AGENTCORE_TELEMETRY_DISABLED="${AGENTCORE_TELEMETRY_DISABLED:-1}"
AGENTCORE_BIN="$DEPLOY_DIR/.cli/node_modules/.bin/agentcore"
# The one CloudFormation stack: AgentCore-<project>-<target> (deploy_tool.STACK_NAME; a unit test keeps them equal).
# shellcheck disable=SC2034  # used by the scripts that source this file
STACK_NAME="AgentCore-TransformWorkshop-default"

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

require() {
  for bin in "$@"; do command -v "$bin" >/dev/null 2>&1 || die "'$bin' is required on PATH"; done
}

# Install the version-pinned AgentCore CLI (deploy/.cli) and the vended CDK app deps (deploy/agentcore/cdk).
ensure_cli() {
  require node npm uv
  if [[ ! -x "$AGENTCORE_BIN" ]]; then
    log "installing the pinned AgentCore CLI into deploy/.cli"
    npm ci --prefix "$DEPLOY_DIR/.cli" --no-audit --no-fund --loglevel=error
  fi
  if [[ ! -d "$DEPLOY_DIR/agentcore/cdk/node_modules" ]]; then
    log "installing the CDK app dependencies into deploy/agentcore/cdk"
    npm ci --prefix "$DEPLOY_DIR/agentcore/cdk" --no-audit --no-fund --loglevel=error
  fi
}

# The pinned CLI, run from the AgentCore project root (deploy/).
ac() { (cd "$DEPLOY_DIR" && "$AGENTCORE_BIN" "$@"); }

# Python helpers (deploy_tool.py) in the project's uv environment.
tool() { (cd "$REPO_ROOT" && uv run --quiet python deploy/deploy_tool.py "$@"); }

# deploy_tool.py packaging flags from the environment: NO_BINARIES=1, VENDOR_TERRAFORM=1.
binary_flags() {
  [[ "${NO_BINARIES:-0}" == "1" ]] && printf '%s\n' --no-binaries
  [[ "${VENDOR_TERRAFORM:-0}" == "1" ]] && printf '%s\n' --with-terraform
  return 0
}

check_aws() {
  require aws
  local who
  who="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null)" \
    || die "no valid AWS credentials (profile '${AWS_PROFILE:-default}'). Try: aws sso login --profile <profile>"
  log "AWS identity: $who (region $AWS_REGION)"
}
