#!/usr/bin/env bash
# Invoke a deployed agent and stream its SSE events to the terminal.
#
#   deploy/invoke.sh hello 'List the files and report.'                    plain text -> {"instruction": ...}
#   deploy/invoke.sh hello '{"instruction": "...", "files": {"a.txt": "hi"}}'
#   deploy/invoke.sh discovery @payload.json                                payload from a file ('-' = stdin)
#   deploy/invoke.sh hello '...' --raw                                      one JSON event per line
#   deploy/invoke.sh hello '...' --out events.jsonl                         also save the events
#   deploy/invoke.sh hello '...' --local http://localhost:8080              a local agent server instead
#
# Uses boto3 InvokeAgentRuntime with the ARN from deploy/agent-arns.json (qualifier DEFAULT, new session id).
# The AgentCore CLI equivalent is: deploy/agentcore.sh invoke --runtime hello "List the files"
# shellcheck source=deploy/_common.sh disable=SC1091
source "$(dirname "$0")/_common.sh"

[[ $# -ge 1 ]] || die "usage: deploy/invoke.sh <stage> ['<json payload>' | text | @file.json | -] [--raw] [--out FILE] [--local URL]"
tool invoke "$@"
