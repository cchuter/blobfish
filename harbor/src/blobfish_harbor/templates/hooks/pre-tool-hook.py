from hook_common import (
    current_timing,
    denied_edit_path,
    emit_hook,
    ensure_dirs,
    log_line,
    measured_overwrite_reason,
    mutated_evidence_reason,
    read_int,
    read_json_stdin,
    write_text,
    state_path,
)


def main() -> None:
    ensure_dirs()
    payload = read_json_stdin()
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}

    blocked_path = denied_edit_path(tool_name, tool_input)
    if blocked_path:
        log_line(f"PreToolUse deny tool={tool_name} path={blocked_path}")
        emit_hook(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Do not modify tests, verifiers, or Claude settings unless the task explicitly requires it."
                    ),
                }
            }
        )
        return

    mutated_reason = mutated_evidence_reason(tool_name, tool_input)
    if mutated_reason:
        log_line(f"PreToolUse deny_mutated_evidence tool={tool_name}")
        emit_hook(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": mutated_reason,
                }
            }
        )
        return

    measured_reason = measured_overwrite_reason(tool_name, tool_input)
    if measured_reason:
        log_line(f"PreToolUse deny_measured_overwrite tool={tool_name}")
        emit_hook(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": measured_reason,
                }
            }
        )
        return

    if tool_name != "Bash":
        return

    command = tool_input.get("command", "")
    if not isinstance(command, str) or "status" in command:
        return

    _, elapsed, timeout = current_timing()
    remaining = timeout - elapsed if timeout else 0
    phase = 0
    if timeout > 0:
        pct = int((elapsed * 100) / timeout)
        if remaining < 120:
            phase = 3
        elif pct >= 75:
            phase = 2
        elif pct >= 50:
            phase = 1

    last_phase = read_int(state_path("phase"), -1)
    consecutive_failures = read_int(state_path("failures"), 0)
    should_inject = (
        not state_path("first_bash")
        or not __import__("os").path.exists(state_path("first_bash"))
        or phase > last_phase
        or consecutive_failures >= 2
    )
    if not should_inject:
        return

    write_text(state_path("first_bash"), "1\n")
    write_text(state_path("phase"), f"{phase}\n")
    if consecutive_failures >= 2:
        write_text(state_path("failures"), "0\n")

    new_command = f"status; {command}"
    log_line(
        f"PreToolUse inject_status phase={phase} elapsed={elapsed} remaining={remaining} failures={consecutive_failures}"
    )
    emit_hook(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "permissionDecisionReason": "Injected status checkpoint before Bash command",
                "updatedInput": {"command": new_command},
                "additionalContext": (
                    "Use the status output as the authoritative budget/current-state snapshot for your next decision."
                ),
            }
        }
    )


if __name__ == "__main__":
    main()
