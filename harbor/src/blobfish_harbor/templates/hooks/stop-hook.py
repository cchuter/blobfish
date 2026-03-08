from hook_common import emit_hook, ensure_dirs, log_line, read_int, read_lines, state_path, write_text


def main() -> None:
    ensure_dirs()
    pending_validation = read_int(state_path("pending_validation"), 0) > 0
    stop_blocks = read_int(state_path("stop_blocked"), 0)
    evidence = read_lines(state_path("recent_evidence"))

    if pending_validation and stop_blocks < 1:
        write_text(state_path("stop_blocked"), "1\n")
        reason = (
            "You changed /app files since your last validation. Before stopping, run a direct validation step such as reading the final artifact or running the authoritative task test path. "
            "Preserve observed evidence exactly and do not drop characters from observed strings to satisfy heuristics."
        )
        if evidence:
            reason += " Recent evidence: " + " || ".join(evidence) + "."
        log_line("Stop block pending_validation=1")
        emit_hook({"decision": "block", "reason": reason})
        return

    log_line(f"Stop allow pending_validation={int(pending_validation)} stop_blocks={stop_blocks}")


if __name__ == "__main__":
    main()
