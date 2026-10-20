#!/usr/bin/env bash
# Runtime logs and traces of one agent, through the AgentCore CLI.
#
#   deploy/logs.sh hello                     last 30 minutes of logs
#   deploy/logs.sh hello --since 2h --level error
#   deploy/logs.sh hello traces              recent traces (CloudWatch Transaction Search / GenAI Observability)
#   deploy/logs.sh hello trace <traceId>     download one trace as JSON
# shellcheck source=deploy/_common.sh disable=SC1091
source "$(dirname "$0")/_common.sh"

[[ $# -ge 1 ]] || die "usage: deploy/logs.sh <stage> [traces | trace <id> | agentcore logs flags...]"
stage="$1"; shift
ensure_cli
case "${1:-}" in
  traces) shift; ac traces list --runtime "$stage" "$@" ;;
  trace) shift; ac traces get --runtime "$stage" "$@" ;;
  *) [[ " $* " == *" --since "* ]] || set -- --since 30m "$@"; ac logs --runtime "$stage" "$@" ;;
esac
