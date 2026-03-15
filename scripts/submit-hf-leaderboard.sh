#!/usr/bin/env bash
# Prepare and submit a Terminal-Bench 2.0 leaderboard entry to the
# HuggingFace dataset repo via Pull Request.
#
# Prerequisites:
#   - git, git-lfs
#   - A HuggingFace account with a fork of the leaderboard repo
#
# Usage:
#   ./scripts/submit-hf-leaderboard.sh \
#       --job-dir jobs/2026-03-08__22-02-04 \
#       --agent-display-name "Blobfish" \
#       --agent-org-display-name "teamblobfish.com" \
#       --agent-url "https://github.com/teamblobfish/blobfish" \
#       --model-name "minimax-m2.5" \
#       --model-provider "minimax" \
#       --model-display-name "Minimax-m2.5" \
#       --model-org-display-name "Minimax" \
#       --hf-user "<your-hf-username>"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- Defaults ----------------------------------------------------------
HF_UPSTREAM="harborframework/terminal-bench-2-leaderboard"
AGENT_DISPLAY_NAME="cchuter"
AGENT_ORG_DISPLAY_NAME="teamblobfish.com"
AGENT_URL="https://github.com/cchuter/blobfish"
MODEL_NAME=""
MODEL_PROVIDER=""
MODEL_DISPLAY_NAME=""
MODEL_ORG_DISPLAY_NAME=""
HF_USER=""
JOB_DIR=""
WORK_DIR=""
DRY_RUN=false
SKIP_CLONE=false

# --- Parse args --------------------------------------------------------
usage() {
  cat <<EOF >&2
Usage: $0 --job-dir <path> --hf-user <user> [options]

Required:
  --job-dir DIR                 Path to Harbor job directory (e.g. jobs/2026-03-08__22-02-04)
  --hf-user USER                Your HuggingFace username

Optional:
  --agent-display-name NAME     Agent display name (default: Blobfish)
  --agent-org-display-name ORG  Agent org (default: teamblobfish.com)
  --agent-url URL               Agent URL (default: github.com/teamblobfish/blobfish)
  --model-name NAME             Model identifier (auto-detected from results if omitted)
  --model-provider PROVIDER     Model provider (auto-detected if omitted)
  --model-display-name NAME     Model display name (auto-detected if omitted)
  --model-org-display-name ORG  Model org display name (auto-detected if omitted)
  --work-dir DIR                Working directory for clone (default: /tmp/tb-leaderboard-submit)
  --dry-run                     Prepare submission dir but don't push or create PR
  --skip-clone                  Reuse existing clone in work-dir
  -h, --help                    Show this help
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --job-dir)              JOB_DIR="$2"; shift 2 ;;
    --hf-user)              HF_USER="$2"; shift 2 ;;
    --agent-display-name)   AGENT_DISPLAY_NAME="$2"; shift 2 ;;
    --agent-org-display-name) AGENT_ORG_DISPLAY_NAME="$2"; shift 2 ;;
    --agent-url)            AGENT_URL="$2"; shift 2 ;;
    --model-name)           MODEL_NAME="$2"; shift 2 ;;
    --model-provider)       MODEL_PROVIDER="$2"; shift 2 ;;
    --model-display-name)   MODEL_DISPLAY_NAME="$2"; shift 2 ;;
    --model-org-display-name) MODEL_ORG_DISPLAY_NAME="$2"; shift 2 ;;
    --work-dir)             WORK_DIR="$2"; shift 2 ;;
    --dry-run)              DRY_RUN=true; shift ;;
    --skip-clone)           SKIP_CLONE=true; shift ;;
    -h|--help)              usage ;;
    *)                      echo "Unknown option: $1" >&2; usage ;;
  esac
done

[[ -z "$JOB_DIR" ]] && { echo "Error: --job-dir is required" >&2; usage; }
[[ -z "$HF_USER" ]] && { echo "Error: --hf-user is required" >&2; usage; }

# Resolve job dir
if [[ ! "$JOB_DIR" = /* ]]; then
  JOB_DIR="$REPO_ROOT/$JOB_DIR"
fi
JOB_DIR="$(cd "$JOB_DIR" && pwd)"

[[ ! -d "$JOB_DIR" ]] && { echo "Error: job directory not found: $JOB_DIR" >&2; exit 1; }

# --- Auto-detect model info from first result.json --------------------
auto_detect_model() {
  local first_result
  first_result=$(find "$JOB_DIR" -maxdepth 2 -name result.json -print -quit 2>/dev/null)
  if [[ -z "$first_result" ]]; then
    echo "Error: no result.json found in $JOB_DIR" >&2
    exit 1
  fi
  if [[ -z "$MODEL_NAME" ]]; then
    MODEL_NAME=$(python3 -c "
import json, sys
r = json.load(open('$first_result'))
info = r.get('agent_info', {}).get('model_info', {})
print(info.get('name', ''))
")
  fi
  if [[ -z "$MODEL_PROVIDER" ]]; then
    MODEL_PROVIDER=$(python3 -c "
import json, sys
r = json.load(open('$first_result'))
info = r.get('agent_info', {}).get('model_info', {})
print(info.get('provider', ''))
")
  fi
  if [[ -z "$MODEL_DISPLAY_NAME" ]]; then
    # Capitalize first letter of each segment
    MODEL_DISPLAY_NAME=$(echo "$MODEL_NAME" | sed 's/\b\(.\)/\u\1/g; s/-/ /g' | sed 's/ /-/g')
    # Fallback: just use the raw name
    [[ -z "$MODEL_DISPLAY_NAME" ]] && MODEL_DISPLAY_NAME="$MODEL_NAME"
  fi
  if [[ -z "$MODEL_ORG_DISPLAY_NAME" ]]; then
    MODEL_ORG_DISPLAY_NAME=$(echo "$MODEL_PROVIDER" | sed 's/\b\(.\)/\u\1/g')
    [[ -z "$MODEL_ORG_DISPLAY_NAME" ]] && MODEL_ORG_DISPLAY_NAME="$MODEL_PROVIDER"
  fi
  return 0
}

auto_detect_model

echo "=== Terminal-Bench 2.0 Leaderboard Submission ==="
echo ""
echo "  Job dir   : $JOB_DIR"
echo "  Agent     : $AGENT_DISPLAY_NAME ($AGENT_ORG_DISPLAY_NAME)"
echo "  Agent URL : $AGENT_URL"
echo "  Model     : $MODEL_DISPLAY_NAME ($MODEL_ORG_DISPLAY_NAME)"
echo "  Provider  : $MODEL_PROVIDER"
echo "  HF user   : $HF_USER"
echo ""

# --- Validate job before proceeding -----------------------------------
echo "--- Validating job data ---"

TRIAL_COUNT=0
ERROR_COUNT=0

for trial_dir in "$JOB_DIR"/*/; do
  [[ ! -d "$trial_dir" ]] && continue
  trial_name=$(basename "$trial_dir")

  # Must have result.json
  if [[ ! -f "$trial_dir/result.json" ]]; then
    echo "  WARN: $trial_name missing result.json"
    ERROR_COUNT=$((ERROR_COUNT + 1))
    continue
  fi

  # Must have config.json
  if [[ ! -f "$trial_dir/config.json" ]]; then
    echo "  WARN: $trial_name missing config.json"
    ERROR_COUNT=$((ERROR_COUNT + 1))
    continue
  fi

  TRIAL_COUNT=$((TRIAL_COUNT + 1))
done

echo "  Trials found: $TRIAL_COUNT"
if [[ $ERROR_COUNT -gt 0 ]]; then
  echo "  Warnings: $ERROR_COUNT"
fi

# Validate timeout_multiplier and overrides from a sample config
SAMPLE_CONFIG=$(find "$JOB_DIR" -maxdepth 2 -name config.json -print -quit)
if [[ -n "$SAMPLE_CONFIG" ]]; then
  VALIDATION=$(python3 -c "
import json, sys

c = json.load(open('$SAMPLE_CONFIG'))
errors = []

tm = c.get('timeout_multiplier')
if tm != 1.0:
    errors.append(f'timeout_multiplier is {tm}, must be 1.0')

agent = c.get('agent', {})
for field in ['override_timeout_sec', 'max_timeout_sec', 'override_setup_timeout_sec']:
    if agent.get(field) is not None:
        errors.append(f'agent.{field} is set ({agent[field]}), must be null')

env = c.get('environment', {})
for field in ['override_cpus', 'override_memory_mb', 'override_storage_mb']:
    if env.get(field) is not None:
        errors.append(f'environment.{field} is set ({env[field]}), must be null')

verifier = c.get('verifier', {})
for field in ['override_timeout_sec', 'max_timeout_sec']:
    if verifier.get(field) is not None:
        errors.append(f'verifier.{field} is set ({verifier[field]}), must be null')

if errors:
    print('FAIL')
    for e in errors:
        print(f'  - {e}')
    sys.exit(1)
else:
    print('PASS')
")
  echo "  Validation: $VALIDATION"
fi

# Check trials per task
TASKS_UNDER_5=$(python3 -c "
import os, collections
job_dir = '$JOB_DIR'
counts = collections.Counter()
for d in os.listdir(job_dir):
    if os.path.isdir(os.path.join(job_dir, d)):
        task = d.rsplit('__', 1)[0]
        counts[task] += 1
under = {t: c for t, c in counts.items() if c < 5}
if under:
    for t, c in sorted(under.items()):
        print(f'  WARN: {t} has only {c} trial(s) (need 5)')
    print(f'UNDER_5={len(under)}')
else:
    print('OK: all tasks have >= 5 trials')
")
echo "  $TASKS_UNDER_5"

echo ""

# --- Compute submission directory name --------------------------------
SUBMISSION_DIR_NAME="${AGENT_DISPLAY_NAME}__${MODEL_DISPLAY_NAME}"
JOB_FOLDER_NAME=$(basename "$JOB_DIR")

echo "  Submission path: submissions/terminal-bench/2.0/${SUBMISSION_DIR_NAME}/"
echo "  Job folder:      ${JOB_FOLDER_NAME}"
echo ""

# --- Clone / setup HuggingFace repo ----------------------------------
WORK_DIR="${WORK_DIR:-/tmp/tb-leaderboard-submit}"

if [[ "$SKIP_CLONE" == false ]]; then
  echo "--- Cloning leaderboard repo ---"
  rm -rf "$WORK_DIR"
  mkdir -p "$WORK_DIR"

  # Clone the user's fork
  HF_FORK_URL="https://huggingface.co/datasets/${HF_USER}/terminal-bench-2-leaderboard"
  echo "  Cloning from: $HF_FORK_URL"
  cd "$WORK_DIR"
  GIT_LFS_SKIP_SMUDGE=1 git clone "$HF_FORK_URL" repo
  cd repo

  # Add upstream remote
  git remote add upstream "https://huggingface.co/datasets/${HF_UPSTREAM}" 2>/dev/null || true
  git fetch upstream main
  git checkout -b "submit/${SUBMISSION_DIR_NAME}" upstream/main
else
  echo "--- Reusing existing clone ---"
  cd "$WORK_DIR/repo"
fi

# --- Create submission directory and copy data ------------------------
echo "--- Preparing submission ---"

DEST="submissions/terminal-bench/2.0/${SUBMISSION_DIR_NAME}"
mkdir -p "${DEST}"

# Write metadata.yaml
cat > "${DEST}/metadata.yaml" <<YAML
agent_url: "${AGENT_URL}"
agent_display_name: "${AGENT_DISPLAY_NAME}"
agent_org_display_name: "${AGENT_ORG_DISPLAY_NAME}"

models:
  - model_name: "${MODEL_NAME}"
    model_provider: "${MODEL_PROVIDER}"
    model_display_name: "${MODEL_DISPLAY_NAME}"
    model_org_display_name: "${MODEL_ORG_DISPLAY_NAME}"
YAML

echo "  Created metadata.yaml"

# Copy job folder with trial data
echo "  Copying job data from ${JOB_DIR} ..."
cp -r "$JOB_DIR" "${DEST}/${JOB_FOLDER_NAME}"
echo "  Copied $(find "${DEST}/${JOB_FOLDER_NAME}" -maxdepth 1 -type d | wc -l | tr -d ' ') trial directories"

# Strip sensitive env vars (API keys, base URLs) from config.json files
echo "  Stripping sensitive env vars from config.json files ..."
find "${DEST}" -name config.json -exec python3 -c "
import json, sys

for path in sys.argv[1:]:
    with open(path) as f:
        c = json.load(f)
    agent = c.get('agent', {})
    if 'env' in agent:
        agent['env'] = {k: '<redacted>' for k in agent['env']}
    with open(path, 'w') as f:
        json.dump(c, f, indent=4)
        f.write('\n')
" {} +
echo "  Done"

# Strip sensitive info from result.json files (embedded config copies)
echo "  Stripping sensitive env vars from result.json files ..."
find "${DEST}" -name result.json -exec python3 -c "
import json, sys

for path in sys.argv[1:]:
    with open(path) as f:
        r = json.load(f)
    config = r.get('config', {})
    agent = config.get('agent', {})
    if 'env' in agent:
        agent['env'] = {k: '<redacted>' for k in agent['env']}
    # Strip local file paths from trial_uri
    if 'trial_uri' in r and r['trial_uri'].startswith('file:///'):
        r['trial_uri'] = ''
    with open(path, 'w') as f:
        json.dump(r, f, indent=4)
        f.write('\n')
" {} +
echo "  Done"

# --- Summary ----------------------------------------------------------
TOTAL_SIZE=$(du -sh "${DEST}" | cut -f1)
echo ""
echo "=== Submission ready ==="
echo "  Path: $(pwd)/${DEST}"
echo "  Size: ${TOTAL_SIZE}"
echo ""
cat "${DEST}/metadata.yaml"
echo ""

if [[ "$DRY_RUN" == true ]]; then
  echo "--- Dry run: skipping git operations ---"
  echo "  Review the submission at: $(pwd)/${DEST}"
  exit 0
fi

# --- Git add, commit, push --------------------------------------------
echo "--- Committing and pushing ---"
git add "${DEST}"
git commit -m "Add ${SUBMISSION_DIR_NAME} submission"
git push -u origin "submit/${SUBMISSION_DIR_NAME}"

echo ""
echo "=== Next steps ==="
echo ""
echo "  1. Go to: https://huggingface.co/datasets/${HF_UPSTREAM}/discussions"
echo "  2. Create a new Pull Request from your branch:"
echo "     ${HF_USER}:submit/${SUBMISSION_DIR_NAME} → main"
echo "  3. Wait for the validation bot to check your submission"
echo "  4. If validation passes, a maintainer will merge it"
echo ""
echo "  Or create the PR directly at:"
echo "  https://huggingface.co/datasets/${HF_USER}/terminal-bench-2-leaderboard/discussions/new?type=pull_request"
echo ""
