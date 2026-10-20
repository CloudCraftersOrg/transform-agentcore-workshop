#!/usr/bin/env bash
# Tear down the workshop agents.
#
#   deploy/destroy.sh                    delete the CloudFormation stack: every runtime and its IAM role
#   deploy/destroy.sh terraform finops   remove only these runtimes (the others are redeployed unchanged)
#   AUTO_APPROVE=1 deploy/destroy.sh     skip the confirmation prompt (needed when stdin is not a terminal)
#
# What is deployed is read from the CloudFormation stack (read-only) before anything changes, so this also works
# from a fresh clone that has no local state. It prints what it will delete and asks first.
# The CDK bootstrap stack (CDKToolkit) is left in place; see deploy/README.md to remove it too.
# shellcheck source=deploy/_common.sh disable=SC1091
source "$(dirname "$0")/_common.sh"

confirm() {
  [[ "${AUTO_APPROVE:-0}" == "1" ]] && return 0
  [[ -t 0 ]] || die "stdin is not a terminal: re-run with AUTO_APPROVE=1 to skip the confirmation"
  local answer
  read -r -p "$1 [y/N] " answer
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]] || die "aborted: nothing was changed"
}

require uv
check_aws

log "looking up what is deployed (read-only)"
rc=0
plan="$(tool plan-destroy "$@" --region "$AWS_REGION")" || rc=$?
if [[ $rc -eq 3 ]]; then
  # Nothing in AWS. After a full destroy request, also drop any stale local record of a deployment.
  [[ $# -eq 0 ]] && tool forget
  exit 0
fi
[[ $rc -eq 0 ]] || die "nothing was changed"
{ read -r stack; read -r remaining || true; } <<<"$plan"

if [[ -z "$remaining" ]]; then
  confirm "Delete stack $stack and everything listed above?"
  log "deleting CloudFormation stack $stack (this takes a few minutes)"
  aws cloudformation delete-stack --stack-name "$stack"
  aws cloudformation wait stack-delete-complete --stack-name "$stack" \
    || die "stack $stack did not delete cleanly: see its events in the CloudFormation console, then re-run"
  tool forget
  log "destroyed. The CDKToolkit bootstrap stack and its asset bucket remain (deploy/README.md: full cleanup)."
  exit 0
fi

confirm "Remove the runtimes listed above and redeploy the rest ($remaining)?"
ensure_cli
log "rebuilding the remaining agents ($remaining) and redeploying"
# shellcheck disable=SC2046,SC2086  # stage names and flags are plain words
tool prepare $remaining --exact --region "$AWS_REGION" $(binary_flags)
ac validate
ac deploy --yes
tool arns --region "$AWS_REGION"
