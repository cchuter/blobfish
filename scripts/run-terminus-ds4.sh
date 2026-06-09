#!/usr/bin/env bash
# run-terminus-ds4.sh -- Harbor Terminus 2 wrapper for local DS4.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
HARBOR_BIN="${HARBOR_BIN:-harbor}"

DATASET="terminal-bench@2.0"
AGENT_IMPORT_PATH="blobfish_harbor:Terminus2DS4Agent"
MODEL="deepseek-v4-flash"
API_BASE="http://127.0.0.1:8081"
API_KEY="no-key"
ATTEMPTS=1
N_CONCURRENT=1
TIMEOUT_MULTIPLIER="1.0"
THINKING_TOKENS=0
CONTEXT_TOKENS=196608
OUTPUT_TOKENS=48000
TEMPERATURE="0.7"
API_TIMEOUT_SEC="3000"
MAX_TURNS=""
JOBS_DIR=""
JOB_NAME=""
DEBUG=false
SKIP_PREFLIGHT=false
DRY_RUN=false
TASKS=()
EXCLUDE_TASKS=()
EXTRA_ARGS=()

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options] [-- <extra harbor args>]

Runs Terminal-Bench through a Terminus 2 DS4 adapter against a local DS4 server
exposed through claude-cache-proxy. The adapter calls /v1/messages directly and
does not route model requests through LiteLLM.

Defaults assume:
  ds4-server            http://127.0.0.1:8080
  claude-cache-proxy    http://127.0.0.1:8081

Core options:
  --agent-import-path PATH      Custom Terminus DS4 agent (default: $AGENT_IMPORT_PATH)
  --model MODEL                 DS4 model name (default: $MODEL)
  --api-base URL                Anthropic-compatible DS4/proxy URL (default: $API_BASE)
  --api-key KEY                 API key sent to DS4/proxy (default: no-key)
  --dataset DATASET             Dataset name@version (default: $DATASET)
  -k, --attempts N              Number of attempts per task (default: $ATTEMPTS)
  -n, --concurrent N            Concurrent trials (default: $N_CONCURRENT)
  --timeout-multiplier X        Harbor timeout multiplier (default: $TIMEOUT_MULTIPLIER)
  -t, --task NAME               Include task name/pattern (repeatable)
  -x, --exclude-task NAME       Exclude task name/pattern (repeatable)

Terminus/LLM options:
  --thinking-tokens N           0 disables DS4 thinking; >0 enables it (default: $THINKING_TOKENS)
  --context-tokens N            model_info max_input_tokens (default: $CONTEXT_TOKENS)
  --output-tokens N             model_info max_output_tokens (default: $OUTPUT_TOKENS)
  --temperature X               Terminus sampling temperature (default: $TEMPERATURE)
  --api-timeout-sec N           Direct DS4 request timeout (default: $API_TIMEOUT_SEC)
  --max-turns N                 Optional Terminus max_turns cap

Job/output options:
  --job-name NAME               Harbor job name
  --jobs-dir PATH               Harbor jobs output dir
  --debug                       Enable Harbor debug logging
  --skip-preflight              Do not check API_BASE /v1/models before running
  --dry-run                     Print the command without running it
USAGE
}

die() {
  echo "Error: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --agent-import-path) AGENT_IMPORT_PATH="$2"; shift 2 ;;
    --api-base) API_BASE="$2"; shift 2 ;;
    --api-key) API_KEY="$2"; shift 2 ;;
    --dataset) DATASET="$2"; shift 2 ;;
    -k|--attempts) ATTEMPTS="$2"; shift 2 ;;
    -n|--concurrent) N_CONCURRENT="$2"; shift 2 ;;
    --timeout-multiplier) TIMEOUT_MULTIPLIER="$2"; shift 2 ;;
    -t|--task) TASKS+=("$2"); shift 2 ;;
    -x|--exclude-task) EXCLUDE_TASKS+=("$2"); shift 2 ;;
    --thinking-tokens|--max-thinking-tokens) THINKING_TOKENS="$2"; shift 2 ;;
    --context-tokens) CONTEXT_TOKENS="$2"; shift 2 ;;
    --output-tokens) OUTPUT_TOKENS="$2"; shift 2 ;;
    --temperature) TEMPERATURE="$2"; shift 2 ;;
    --api-timeout-sec) API_TIMEOUT_SEC="$2"; shift 2 ;;
    --max-turns) MAX_TURNS="$2"; shift 2 ;;
    --job-name) JOB_NAME="$2"; shift 2 ;;
    --jobs-dir) JOBS_DIR="$2"; shift 2 ;;
    --debug) DEBUG=true; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
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

command -v "$HARBOR_BIN" >/dev/null 2>&1 || die "Harbor binary not found: $HARBOR_BIN"

if [[ "$N_CONCURRENT" != "1" ]]; then
  echo "Warning: local DS4 is configured as a single-slot server; -n 1 is safest for KV reuse." >&2
fi

if [[ "$SKIP_PREFLIGHT" == false && "$DRY_RUN" == false ]]; then
  if ! curl -fsS --max-time 5 "$API_BASE/v1/models" >/dev/null; then
    cat >&2 <<EOF
Error: $API_BASE/v1/models is not reachable.

Start DS4 and the cache proxy first, for example:
  cd ~/work/ds4
  ./ds4-server --chdir ~/work/ds4 --host 127.0.0.1 --port 8080 --ctx 196608 --tokens 48000 --warm-weights --kv-disk-dir ~/work/ds4-kv-tbench --kv-disk-space-mb 98304 --kv-cache-cold-max-tokens 196608 --kv-cache-continued-interval-tokens 8192 --kv-cache-boundary-align-tokens 2048 --kv-cache-boundary-trim-tokens 32 --tool-memory-max-ids 200000

  cd ~/work/claude-cache-proxy
  python3 cache-proxy.py --port 8081 --upstream http://127.0.0.1:8080 --verbose --rewrite-reminders
EOF
    exit 1
  fi
fi

MODEL_INFO=$(printf '{"max_input_tokens":%s,"max_output_tokens":%s,"input_cost_per_token":0.0,"output_cost_per_token":0.0}' "$CONTEXT_TOKENS" "$OUTPUT_TOKENS")

CMD=(
  "$HARBOR_BIN" run
  -d "$DATASET"
  --agent-import-path "$AGENT_IMPORT_PATH"
  -m "$MODEL"
  -k "$ATTEMPTS"
  -n "$N_CONCURRENT"
  --timeout-multiplier "$TIMEOUT_MULTIPLIER"
  --ak "api_base=$API_BASE"
  --ak "max_thinking_tokens=$THINKING_TOKENS"
  --ak "model_info=$MODEL_INFO"
  --ak "temperature=$TEMPERATURE"
  --ak "llm_kwargs={\"timeout_sec\":$API_TIMEOUT_SEC}"
)

if [[ -n "$MAX_TURNS" ]]; then
  CMD+=( --ak "max_turns=$MAX_TURNS" )
fi
if [[ -n "$JOB_NAME" ]]; then
  CMD+=( --job-name "$JOB_NAME" )
fi
if [[ -n "$JOBS_DIR" ]]; then
  CMD+=( --jobs-dir "$JOBS_DIR" )
fi
if [[ "$DEBUG" == true ]]; then
  CMD+=( --debug )
fi
for task in "${TASKS[@]+"${TASKS[@]}"}"; do
  CMD+=( -t "$task" )
done
for task in "${EXCLUDE_TASKS[@]+"${EXCLUDE_TASKS[@]}"}"; do
  CMD+=( -x "$task" )
done
if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
  CMD+=( "${EXTRA_ARGS[@]}" )
fi

echo "Running:"
printf '  %q' "ANTHROPIC_API_KEY=$API_KEY" "${CMD[@]}"
echo

if [[ "$DRY_RUN" == true ]]; then
  exit 0
fi

cd "$REPO_ROOT"
ANTHROPIC_API_KEY="$API_KEY" "${CMD[@]}"
