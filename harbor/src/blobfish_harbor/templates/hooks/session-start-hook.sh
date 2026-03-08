#!/bin/sh
hook_log=/logs/agent/hooks.log
timeout=${TASK_TIMEOUT_SECS:-unknown}

mkdir -p /logs/agent
printf 'SessionStart timeout=%s\n' "$timeout" >> "$hook_log"
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"Runtime control: actual task timeout is %ss. Hook budget reminders are authoritative. Preserve observed evidence exactly; do not mutate observed strings to satisfy heuristics. If you have a plausible evidence-backed answer, write the required output artifact immediately; you can overwrite it later."}}' "$timeout"
