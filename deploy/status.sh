#!/usr/bin/env bash
# Show what is deployed: the AgentCore CLI view (per-runtime status) and the ARNs the orchestrator will use.
# shellcheck source=deploy/_common.sh disable=SC1091
source "$(dirname "$0")/_common.sh"

ensure_cli
ac status "$@" || true
echo
tool status
