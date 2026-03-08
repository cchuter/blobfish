import json
import os
import re
import time

HOOK_LOG = "/logs/agent/hooks.log"
STATE_DIR = "/tmp/blobfish-hook"
VALIDATION_MARKERS = (
    "/tests/",
    "pytest",
    "unittest",
    "cargo test",
    "go test",
    "npm test",
    "pnpm test",
    "yarn test",
    "bun test",
    "ctest",
    "make test",
    "verify",
    "cat /app/",
    "sed -n ",
    "grep ",
)


def state_path(name: str) -> str:
    return os.path.join(STATE_DIR, name)


def ensure_dirs() -> None:
    os.makedirs("/logs/agent", exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)


def log_line(line: str) -> None:
    ensure_dirs()
    with open(HOOK_LOG, "a", encoding="utf-8") as fh:
        fh.write(f"{line}\n")


def read_int(path: str, default: int = 0) -> int:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return default
    match = re.search(r"(-?\d+)", text or "")
    return int(match.group(1)) if match else default


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in fh if line.rstrip("\n")]
    except OSError:
        return []


def strong_tokens(lines: list[str]) -> list[str]:
    seen = set()
    tokens: list[str] = []
    for line in lines:
        for token in re.findall(r"([A-Z0-9]{8,})", line):
            if not (re.search(r"[A-Z]", token) and re.search(r"\d", token)):
                continue
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
            if len(tokens) >= 4:
                return tokens
    return tokens


def read_json_stdin() -> dict:
    try:
        raw = os.read(0, 10_000_000).decode("utf-8", "replace")
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def emit_hook(payload: dict) -> None:
    print(json.dumps(payload), end="")


def current_timing() -> tuple[int, int, int]:
    now = int(time.time())
    start = int(os.environ.get("TASK_START_EPOCH", str(now)))
    timeout = int(os.environ.get("TASK_TIMEOUT_SECS", "0") or "0")
    return now, max(0, now - start), timeout


def denied_edit_path(tool_name: str, tool_input: dict) -> str:
    if tool_name not in {"Write", "Edit", "MultiEdit"}:
        return ""
    path = tool_input.get("file_path")
    if not isinstance(path, str):
        return ""
    for part in ("/tests/", "/verifier/", "/.claude/", "/CLAUDE.md"):
        if part in path:
            return path
    return ""


def mutated_evidence_reason(tool_name: str, tool_input: dict) -> str:
    if tool_name != "Write":
        return ""
    path = tool_input.get("file_path", "")
    if not isinstance(path, str) or not path.startswith("/app/"):
        return ""
    content = tool_input.get("content", "")
    if not isinstance(content, str):
        return ""
    candidates = [
        token
        for token in re.findall(r"([A-Z0-9]{8,})", content)
        if re.search(r"[A-Z]", token) and re.search(r"\d", token)
    ]
    if len(candidates) != 1:
        return ""
    candidate = candidates[0]
    tokens = [token for token in strong_tokens(read_lines(state_path("recent_evidence"))) if len(token) >= 10]
    if len(tokens) < 2:
        return ""
    missing = [token for token in tokens if token not in candidate]
    if not missing:
        return ""
    token_summary = " || ".join(tokens[:3])
    return (
        "The content you are writing mutates or drops exact observed token fragments. "
        "Preserve observed evidence exactly before writing. "
        f"Recent exact tokens: {token_summary}."
    )


def measured_overwrite_reason(tool_name: str, tool_input: dict) -> str:
    if tool_name != "Write":
        return ""
    path = tool_input.get("file_path", "")
    if not isinstance(path, str) or not path.startswith("/app/"):
        return ""
    measured_path = read_lines(state_path("measured_path"))
    if not measured_path or measured_path[0] != path:
        return ""
    notice = read_lines(state_path("measured_notice"))
    if notice and notice[0] == path:
        return ""
    write_text(state_path("measured_notice"), f"{path}\n")
    backup = read_lines(state_path("measured_backup"))
    backup_path = backup[0] if backup else "the measured artifact snapshot"
    return (
        f"You are overwriting a previously measured /app artifact at {path}. "
        f"Preserve the measured version before replacement. A snapshot is available at {backup_path}. "
        "If the new variant regresses, restore the measured version instead of throwing it away."
    )


def collect_strings(node, out: list[str]) -> None:
    if node is None:
        return
    if isinstance(node, str):
        if node:
            out.append(node)
        return
    if isinstance(node, list):
        for item in node:
            collect_strings(item, out)
        return
    if isinstance(node, dict):
        for item in node.values():
            collect_strings(item, out)


def salient_evidence_lines(tool_response) -> list[str]:
    strings: list[str] = []
    collect_strings(tool_response, strings)
    seen = set()
    lines: list[str] = []
    for text in strings:
        if len(text) > 6000:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or len(line) > 120:
                continue
            if re.match(
                r"^(Elapsed:|Remaining:|=== /tmp/run_state\.md ===|# Run state|- Goal:|- Best known result:|- Next step:|Exit code \d+|No matches found)$",
                line,
            ):
                continue
            if not re.search(
                r"(PASSWORD=|[A-Z0-9]{8,}|launchcode|/(app|logs)/|\b(pass|fail|score|wins?|matches?|error|timeout|constraint)\b)",
                line,
                re.I,
            ):
                continue
            if line in seen:
                continue
            seen.add(line)
            lines.append(line)
            if len(lines) >= 4:
                return lines
    return lines


def update_evidence(tool_response) -> list[str]:
    new_lines = salient_evidence_lines(tool_response)
    if not new_lines:
        return read_lines(state_path("recent_evidence"))
    existing = read_lines(state_path("recent_evidence"))
    merged: list[str] = []
    seen = set()
    for line in existing + new_lines:
        if line in seen:
            continue
        seen.add(line)
        merged.append(line)
    if len(merged) > 4:
        merged = merged[-4:]
    write_text(state_path("recent_evidence"), "\n".join(merged) + "\n")
    return merged


def mark_pending_validation() -> None:
    write_text(state_path("pending_validation"), "1\n")
    write_text(state_path("stop_blocked"), "0\n")


def clear_pending_validation() -> None:
    write_text(state_path("pending_validation"), "0\n")
    write_text(state_path("stop_blocked"), "0\n")


def has_pending_validation() -> bool:
    return read_int(state_path("pending_validation"), 0) > 0


def looks_like_validation(tool_name: str, tool_input: dict) -> bool:
    if tool_name == "Read":
        file_path = tool_input.get("file_path", "")
        return isinstance(file_path, str) and file_path.startswith("/app/")
    if tool_name != "Bash":
        return False
    command = tool_input.get("command", "")
    if not isinstance(command, str):
        return False
    lowered = command.lower()
    return any(marker in lowered for marker in VALIDATION_MARKERS)


def phase_message(elapsed: int, timeout: int, output_written: bool) -> str:
    if timeout <= 0:
        return "Preserve observed evidence exactly. If you have a plausible answer, write it now."
    remaining = timeout - elapsed
    pct = int((elapsed * 100) / timeout)
    if remaining < 120:
        return "FINAL: <120s left. Write your best answer now and verify."
    if pct >= 75:
        return "75%+ elapsed. Finalize your best solution and write required outputs before time runs out."
    if pct >= 50:
        return "50%+ elapsed. Simplify if not on track. Preserve exact evidence and write your best plausible answer early."
    if not output_written:
        return "If you have a plausible evidence-backed answer, write it now; you can overwrite it later."
    return "Keep work concise and preserve observed evidence exactly."


def snapshot_measured_artifact(tool_name: str) -> None:
    if tool_name != "Bash":
        return
    output_written = read_lines(state_path("output_written"))
    if not output_written:
        return
    path = output_written[0]
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return
    snapshot = state_path("measured_output.snapshot")
    write_text(snapshot, content)
    write_text(state_path("measured_path"), f"{path}\n")
    write_text(state_path("measured_backup"), f"{snapshot}\n")
    write_text(state_path("measured_notice"), "")
