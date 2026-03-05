#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<USAGE
Usage: ./scripts/run-blobfish.sh <model> [task] [--slim] [--fast] [--leaderboard] [--claude|--codex]
Example:
  ./scripts/run-blobfish.sh minimax/minimax-m2.5 fix-git
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

MODEL="${1:-}"
if [[ -z "$MODEL" ]]; then
  usage >&2
  exit 1
fi

TASK="${2:-}"
PROMPT_VARIANT="full"
NO_PROMPT=false
TIMEOUT_MULT="1.0"
K=1
N=1
BACKEND="${BLOBFISH_BACKEND:-claude}"
AGENT_NAME="${BLOBFISH_AGENT_NAME:-${GITHUB_ACTOR:-${USER:-blobfish}}}"
EXTRA_ARGS=()

for arg in "$@"; do
  case "$arg" in
    --slim) PROMPT_VARIANT="slim" ;;
    --no-prompt) NO_PROMPT=true ;;
    --fast) TIMEOUT_MULT="0.5" ;;
    --leaderboard) K=5; TIMEOUT_MULT="1.0"; N=4 ;;
    --claude) BACKEND="claude" ;;
    --codex) BACKEND="codex" ;;
  esac
done

echo "Model: $MODEL (blobfish agent)"
if [[ -n "$TASK" ]]; then
  echo "Task: $TASK"
  EXTRA_ARGS+=( -t "${TASK}*" )
fi

if [[ "$NO_PROMPT" == true ]]; then
  EXTRA_ARGS+=( --no-prompt )
elif [[ "$PROMPT_VARIANT" == "slim" ]]; then
  EXTRA_ARGS+=( --slim-prompt )
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CMD=(
  "$SCRIPT_DIR/run-terminal-bench.sh"
  --backend "$BACKEND"
  --model "$MODEL"
  --agent-name "$AGENT_NAME"
  -k "$K"
  -n "$N"
  --timeout-multiplier "$TIMEOUT_MULT"
)

if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  CMD+=( "${EXTRA_ARGS[@]}" )
fi

"${CMD[@]}"
