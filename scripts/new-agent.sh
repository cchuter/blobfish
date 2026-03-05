#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <github-username>" >&2
  exit 1
fi

AGENT_NAME="$1"
if [[ ! "$AGENT_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}$ ]]; then
  echo "Invalid GitHub username: $AGENT_NAME" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PROFILE_DIR="$REPO_ROOT/agents/$AGENT_NAME"
PROFILE_FILE="$PROFILE_DIR/agent.env"

mkdir -p "$PROFILE_DIR"

cat >"$PROFILE_FILE" <<EOF
BLOBFISH_AGENT_NAME=$AGENT_NAME
BLOBFISH_AGENT_ORG=teamblobfish.com
EOF

echo "Created $PROFILE_FILE"
echo "Next: ./scripts/run-terminal-bench.sh --agent-name $AGENT_NAME"

