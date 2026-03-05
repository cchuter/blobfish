#!/usr/bin/env bash
# run-terminal-bench.sh -- Harbor run wrapper for BlobfishAgent/CchuterAgent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARBOR_BIN="harbor"

DATASET="terminal-bench@2.0"
AGENT_IMPORT_PATH="blobfish_harbor:BlobfishAgent"
ATTEMPTS=1
N_CONCURRENT=4
TIMEOUT_MULTIPLIER="1.0"
BACKEND="claude"
MODEL=""
AGENT_NAME="${GITHUB_ACTOR:-${USER:-blobfish}}"
AGENT_ORG="teamblobfish.com"
JOB_NAME=""
JOBS_DIR=""
TASKS=()
EXCLUDE_TASKS=()
ROUTING_ENABLED=false
ROUTING_TABLE=""
DEFAULT_MODEL=""
OPENAI_BASE_URL=""
OPENAI_API_KEY=""
MAX_THINKING_TOKENS=""
NO_PROMPT=false
SLIM_PROMPT=false
EXTRA_ARGS=()

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options] [-- <extra harbor args>]

Core options:
  --agent-import-path PATH       Import path (default: blobfish_harbor:BlobfishAgent)
  --backend claude|codex         Backend for BlobfishAgent (default: claude)
  --model MODEL                  Harbor model flag (-m), e.g. anthropic/claude-sonnet-4-5
  --agent-name NAME              Leaderboard agent name (default: env/user)
  --agent-org ORG                Leaderboard agent org (default: teamblobfish.com)
  --dataset DATASET              Dataset name@version (default: terminal-bench@2.0)
  -k, --attempts N               Number of attempts per task (default: 1)
  -n, --concurrent N             Number of concurrent trials (default: 4)
  --timeout-multiplier X         Timeout multiplier (default: 1.0)
  -t, --task NAME                Include task name/pattern (repeatable)
  -x, --exclude-task NAME        Exclude task name/pattern (repeatable)

Routing options:
  --routing                      Enable routing kwargs
  --routing-table PATH           JSON routing table path (implies --routing)
  --default-model SELECTOR       Default selector (backend or model string)

Prompt options:
  --no-prompt                    Disable prompt template (use_prompt=false)
  --slim-prompt                  Use slim prompt variant (prompt_variant=slim)

Claude options:
  --max-thinking-tokens N        MAX_THINKING_TOKENS passed to BlobfishAgent

Codex/local model options:
  --openai-base-url URL          Passed as openai_base_url
  --openai-api-key KEY           Exported as OPENAI_API_KEY for this run only

Job/output options:
  --job-name NAME                Harbor job name
  --jobs-dir PATH                Harbor jobs output dir
USAGE
}

die() {
  echo "Error: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent-import-path) AGENT_IMPORT_PATH="$2"; shift 2 ;;
    --backend) BACKEND="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --agent-name) AGENT_NAME="$2"; shift 2 ;;
    --agent-org) AGENT_ORG="$2"; shift 2 ;;
    --dataset) DATASET="$2"; shift 2 ;;
    -k|--attempts) ATTEMPTS="$2"; shift 2 ;;
    -n|--concurrent) N_CONCURRENT="$2"; shift 2 ;;
    --timeout-multiplier) TIMEOUT_MULTIPLIER="$2"; shift 2 ;;
    -t|--task) TASKS+=("$2"); shift 2 ;;
    -x|--exclude-task) EXCLUDE_TASKS+=("$2"); shift 2 ;;
    --routing) ROUTING_ENABLED=true; shift ;;
    --routing-table) ROUTING_ENABLED=true; ROUTING_TABLE="$2"; shift 2 ;;
    --default-model) DEFAULT_MODEL="$2"; shift 2 ;;
    --max-thinking-tokens) MAX_THINKING_TOKENS="$2"; shift 2 ;;
    --no-prompt) NO_PROMPT=true; shift ;;
    --slim-prompt) SLIM_PROMPT=true; shift ;;
    --openai-base-url) OPENAI_BASE_URL="$2"; shift 2 ;;
    --openai-api-key) OPENAI_API_KEY="$2"; shift 2 ;;
    --job-name) JOB_NAME="$2"; shift 2 ;;
    --jobs-dir) JOBS_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --)
      shift
      EXTRA_ARGS+=("$@")
      break
      ;;
    *)
      die "Unknown argument: $1"
      ;;
  esac
done

[[ -x "$HARBOR_BIN" ]] || die "Harbor binary not found: $HARBOR_BIN"
[[ "$BACKEND" == "claude" || "$BACKEND" == "codex" ]] || die "--backend must be claude or codex"

PROFILE_FILE="$REPO_ROOT/agents/$AGENT_NAME/agent.env"
if [[ -f "$PROFILE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$PROFILE_FILE"
  AGENT_NAME="${BLOBFISH_AGENT_NAME:-$AGENT_NAME}"
  AGENT_ORG="${BLOBFISH_AGENT_ORG:-$AGENT_ORG}"
fi

CMD=(
  "$HARBOR_BIN" run
  -d "$DATASET"
  --agent-import-path "$AGENT_IMPORT_PATH"
  -k "$ATTEMPTS"
  -n "$N_CONCURRENT"
  --timeout-multiplier "$TIMEOUT_MULTIPLIER"
  --ak "backend=$BACKEND"
  --ak "agent_name=$AGENT_NAME"
  --ak "agent_org=$AGENT_ORG"
)

if [[ -n "$MODEL" ]]; then
  CMD+=( -m "$MODEL" )
fi
if [[ -n "$JOB_NAME" ]]; then
  CMD+=( --job-name "$JOB_NAME" )
fi
if [[ -n "$JOBS_DIR" ]]; then
  CMD+=( --jobs-dir "$JOBS_DIR" )
fi
for task in "${TASKS[@]+"${TASKS[@]}"}"; do
  CMD+=( -t "$task" )
done
for task in "${EXCLUDE_TASKS[@]+"${EXCLUDE_TASKS[@]}"}"; do
  CMD+=( -x "$task" )
done

if [[ "$ROUTING_ENABLED" == true ]]; then
  if [[ -n "$ROUTING_TABLE" ]]; then
    if [[ ! -f "$ROUTING_TABLE" ]]; then
      if [[ -f "$REPO_ROOT/$ROUTING_TABLE" ]]; then
        ROUTING_TABLE="$REPO_ROOT/$ROUTING_TABLE"
      else
        die "Routing table not found: $ROUTING_TABLE"
      fi
    fi
    CMD+=( --ak "routing_table=$ROUTING_TABLE" )
  fi
  if [[ -n "$DEFAULT_MODEL" ]]; then
    CMD+=( --ak "default_model=$DEFAULT_MODEL" )
  fi
fi

# When ANTHROPIC_BASE_URL is set (local model), pass env vars via --ae
# and use host networking so the container can reach the host server.
ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
if [[ -n "$ANTHROPIC_BASE_URL" ]]; then
  DOCKER_BASE_URL="$ANTHROPIC_BASE_URL"
  # Rewrite localhost for Docker
  DOCKER_BASE_URL="${DOCKER_BASE_URL//localhost/host.docker.internal}"
  DOCKER_BASE_URL="${DOCKER_BASE_URL//127.0.0.1/host.docker.internal}"
  CMD+=( --ae "ANTHROPIC_BASE_URL=$DOCKER_BASE_URL" )
  CMD+=( --ae "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-no-key}" )
  CMD+=( --ek "network_mode=host" )
  CMD+=( --no-force-build )
fi

if [[ -n "$OPENAI_BASE_URL" ]]; then
  CMD+=( --ak "openai_base_url=$OPENAI_BASE_URL" )
fi
if [[ -n "$MAX_THINKING_TOKENS" ]]; then
  CMD+=( --ak "max_thinking_tokens=$MAX_THINKING_TOKENS" )
fi
if [[ "$NO_PROMPT" == true ]]; then
  CMD+=( --ak "use_prompt=false" )
elif [[ "$SLIM_PROMPT" == true ]]; then
  CMD+=( --ak "prompt_variant=slim" )
fi
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  CMD+=( "${EXTRA_ARGS[@]}" )
fi

echo "Running:"
printf '  %q' "${CMD[@]}"
echo

cd "$REPO_ROOT"
if [[ -n "$OPENAI_API_KEY" ]]; then
  OPENAI_API_KEY="$OPENAI_API_KEY" "${CMD[@]}"
else
  "${CMD[@]}"
fi
