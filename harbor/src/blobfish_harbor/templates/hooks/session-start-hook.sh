#!/bin/sh
hook_log=/logs/agent/hooks.log
timeout=${TASK_TIMEOUT_SECS:-unknown}
state_file=/tmp/blobfish-hook/run_state_summary

mkdir -p /logs/agent
printf 'SessionStart timeout=%s\n' "$timeout" >> "$hook_log"

# Build the additionalContext string
context="Runtime control: actual task timeout is ${timeout}s. Hook budget reminders are authoritative. Preserve observed evidence exactly; do not mutate observed strings to satisfy heuristics. If you have a plausible evidence-backed answer, write the required output artifact immediately; you can overwrite it later."

# Append run state if it exists (post-compression recovery)
if [ -s "$state_file" ]; then
    # Read up to 1KB to prevent bloating context window
    state=$(head -c 1024 "$state_file")
    # Use Perl for robust JSON escaping (matches hook_common.pl json_escape)
    state=$(printf '%s' "$state" | perl -pe 's/\\/\\\\/g; s/"/\\"/g; s/\n/\\n/g; s/\r/\\r/g; s/\t/\\t/g')
    context="${context}\\n\\nRun state recovered after context compression:\\n${state}"
    printf 'SessionStart injected run_state_summary\n' >> "$hook_log"
fi

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}' "$context"
