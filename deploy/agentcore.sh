#!/usr/bin/env bash
# Run the version-pinned AgentCore CLI against this project (deploy/), from any directory:
#   deploy/agentcore.sh status
#   deploy/agentcore.sh invoke --runtime hello "List the files and report."
#   deploy/agentcore.sh logs --runtime terraform --since 1h
#   deploy/agentcore.sh traces list --runtime discovery
# shellcheck source=deploy/_common.sh disable=SC1091
source "$(dirname "$0")/_common.sh"

ensure_cli
ac "$@"
