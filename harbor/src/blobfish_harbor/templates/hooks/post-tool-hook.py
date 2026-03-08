from hook_common import (
    clear_pending_validation,
    current_timing,
    emit_hook,
    ensure_dirs,
    has_pending_validation,
    log_line,
    looks_like_validation,
    mark_pending_validation,
    phase_message,
    read_int,
    read_json_stdin,
    read_lines,
    snapshot_measured_artifact,
    state_path,
    update_evidence,
    write_text,
)


def main() -> None:
    ensure_dirs()
    payload = read_json_stdin()
    event = payload.get("hook_event_name", "PostToolUse")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    tool_response = payload.get("tool_response")
    _, elapsed, timeout = current_timing()

    if event == "PostToolUseFailure":
        failures = read_int(state_path("failures"), 0) + 1
        write_text(state_path("failures"), f"{failures}\n")
        log_line(f"{event} tool={tool_name} failures={failures} elapsed={elapsed} timeout={timeout}")
        emit_hook(
            {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": (
                        "Tool failed. Do not repeat the same failing path more than twice; simplify, pivot, or write the best evidence-backed answer you have."
                    ),
                }
            }
        )
        return

    write_text(state_path("failures"), "0\n")

    if tool_name in {"Write", "Edit", "MultiEdit"}:
        file_path = tool_input.get("file_path", "")
        if isinstance(file_path, str) and file_path.startswith("/app/"):
            write_text(state_path("output_written"), f"{file_path}\n")
            mark_pending_validation()

    if looks_like_validation(tool_name, tool_input):
        clear_pending_validation()

    snapshot_measured_artifact(tool_name)
    recent_evidence = update_evidence(tool_response)
    output_written = bool(read_lines(state_path("output_written")))
    pending_validation = has_pending_validation()

    msg = f"[{elapsed}s / {timeout}s] {phase_message(elapsed, timeout, output_written)}"
    if pending_validation:
        msg += " You have unvalidated /app changes; validate the final artifact or test results before stopping."
    if recent_evidence:
        msg += " Recent evidence: " + " || ".join(recent_evidence) + "."
    if not output_written and len(recent_evidence) >= 2:
        msg += (
            " You have multiple short evidence lines already. Before deeper searching, form the simplest exact candidate from the observed lines and write it now. "
            "Prefer exact concatenation or exact observed overlap only; do not alter observed characters to satisfy heuristics."
        )

    log_line(
        f"{event} tool={tool_name} elapsed={elapsed} timeout={timeout} output_written={int(output_written)} pending_validation={int(pending_validation)} evidence_count={len(recent_evidence)}"
    )
    emit_hook({"hookSpecificOutput": {"hookEventName": event, "additionalContext": msg}})


if __name__ == "__main__":
    main()
