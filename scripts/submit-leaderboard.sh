#!/usr/bin/env bash
# Wrapper for `tb admin submit` with Blobfish defaults.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  cat <<USAGE >&2
Usage: $0 <run-id> <agent-name> [model-name] [model-org]
Example:
  $0 2026-03-02__12-00-00 cchuter anthropic/claude-sonnet-4-5 Anthropic
USAGE
  exit 1
fi

RUN_ID="$1"
AGENT_NAME="$2"
MODEL_NAME="${3:-}"
MODEL_ORG="${4:-}"

CMD=(
  tb admin submit
  --run-id "$RUN_ID"
  --agent-name "$AGENT_NAME"
  --agent-org "teamblobfish.com"
)

if [[ -n "$MODEL_NAME" ]]; then
  CMD+=(--model-name "$MODEL_NAME")
fi
if [[ -n "$MODEL_ORG" ]]; then
  CMD+=(--model-org "$MODEL_ORG")
fi

printf '  %q' "${CMD[@]}"
echo
"${CMD[@]}"

