#!/usr/bin/env bash
# Deploy the workshop agents to Amazon Bedrock AgentCore Runtime with the AgentCore CLI (CodeZip, arm64).
#
#   deploy/deploy.sh                     every stage that has transform_agents/<stage>/runtime.py, plus hello
#   deploy/deploy.sh --all               the same, explicitly
#   deploy/deploy.sh hello discovery     a subset; agents that are already deployed stay deployed (destroy.sh
#                                        removes them). What is deployed is read from the CloudFormation stack
#                                        (read-only), or from the CLI state if that lookup fails. If neither can
#                                        tell, a subset deploy is refused: use --all, or FORCE_SUBSET=1 to deploy
#                                        only the named agents (CloudFormation then deletes any other runtime).
#   DRY_RUN=1 deploy/deploy.sh           package + synthesize only; stops before creating anything
#
# Env: AWS_PROFILE, AWS_REGION (default us-west-2), MODEL_<STAGE>/MODEL_ID/MODEL_MAX_TOKENS (copied into the
# runtimes), TERRAFORM_OFFLINE (copied into the terraform runtime), VENDOR_TERRAFORM=1 (ship the terraform binary
# for real `terraform validate` in the cloud: slow cold init), NO_BINARIES=1 (no kubeconform/kubectl/uv either),
# FORCE_SUBSET=1 (see above), VERBOSE=1 (CloudFormation resource events).
#
# The first real deploy bootstraps CDK in the account/region (CDKToolkit stack) and enables CloudWatch
# Transaction Search if needed. Writes deploy/agent-arns.json ({stage: runtimeArn}) for the orchestrator.
# shellcheck source=deploy/_common.sh disable=SC1091
source "$(dirname "$0")/_common.sh"

ensure_cli
check_aws

force_subset=""
[[ "${FORCE_SUBSET:-0}" == "1" ]] && force_subset="--force-subset"

log "preparing agent code directories (deploy/app/<stage>) and agentcore.json"
# shellcheck disable=SC2046  # flags are plain words
tool prepare "$@" ${force_subset:+"$force_subset"} --region "$AWS_REGION" $(binary_flags)
ac validate

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  log "dry run: packaging + CloudFormation synth (no resources are created)"
  ac deploy --dry-run || true
  log "synthesized template: deploy/agentcore/cdk/cdk.out/$STACK_NAME.template.json"
  exit 0
fi

deploy_args=(--yes)
[[ "${VERBOSE:-0}" == "1" ]] && deploy_args+=(--verbose)
log "agentcore deploy (CloudFormation stack $STACK_NAME)"
ac deploy "${deploy_args[@]}"

tool arns --region "$AWS_REGION"
log "done. Try: deploy/invoke.sh hello '{\"prompt\": \"hi\"}'  |  deploy/status.sh  |  deploy/logs.sh hello"
