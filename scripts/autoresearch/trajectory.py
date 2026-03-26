"""
Trajectory extraction module for autoresearch.

Reads ATIF trajectory JSON files from Harbor benchmark runs and compresses
them into smaller dicts suitable for Claude API prompts.
"""

from __future__ import annotations

import json
import os
import re


def load_trajectory(trial_dir: str) -> dict | None:
    """Read <trial_dir>/agent/trajectory.json; return None if missing."""
    path = os.path.join(trial_dir, "agent", "trajectory.json")
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def _truncate(value, max_len: int) -> str:
    """Truncate a string to max_len chars, appending '...' if truncated."""
    s = str(value)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


def _extract_runtime_signals(content: str) -> list[str]:
    """
    Extract timing and status lines from observation content.

    Looks for:
    - Lines matching 'Elapsed: \\d+s' or 'Remaining: \\d+s'
    - Lines from /tmp/run_state.md dumps (section after === /tmp/run_state.md ===)
    - <system-reminder> content
    """
    signals = []

    # Timing lines
    for line in content.splitlines():
        line = line.strip()
        if re.search(r"Elapsed:\s*\d+s", line) or re.search(r"Remaining:\s*\d+s", line):
            signals.append(line)

    # /tmp/run_state.md section
    run_state_match = re.search(
        r"=== /tmp/run_state\.md ===\n(.*?)(?:\n===|\Z)", content, re.DOTALL
    )
    if run_state_match:
        for line in run_state_match.group(1).splitlines():
            line = line.strip()
            if line:
                signals.append(f"[run_state] {line}")

    # <system-reminder> content
    for match in re.finditer(r"<system-reminder>(.*?)</system-reminder>", content, re.DOTALL):
        reminder_text = match.group(1).strip()
        if reminder_text:
            signals.append(f"[system-reminder] {reminder_text[:300]}")

    return signals


def _args_summary(arguments: dict, max_per_value: int = 200) -> dict:
    """Build a truncated copy of the arguments dict."""
    return {k: _truncate(v, max_per_value) for k, v in arguments.items()}


def _observation_output(results: list, max_len: int = 500) -> str:
    """Concatenate all result content strings, truncated to max_len total."""
    parts = []
    for r in results:
        c = r.get("content", "")
        if c:
            parts.append(c)
    combined = "\n".join(parts)
    return _truncate(combined, max_len)


def extract_trajectory(trajectory: dict) -> dict:
    """
    Compress an ATIF trajectory dict.

    Returns a dict with:
      total_steps      - int
      tool_calls       - list of {tool, args_summary, output}
      runtime_signals  - list of timing/status strings from observations
      final_output     - {path, content} or None (last Write to /app/)
      errors           - list of error strings
    """
    steps = trajectory.get("steps", [])
    total_steps = len(steps)

    tool_calls = []
    runtime_signals = []
    final_output = None
    errors = []

    for step in steps:
        # Extract tool calls and their observations
        step_tool_calls = step.get("tool_calls", [])
        step_results = step.get("observation", {}).get("results", []) if "observation" in step else []

        for idx, tc in enumerate(step_tool_calls):
            fn = tc.get("function_name", "unknown")
            args = tc.get("arguments", {})
            args_sum = _args_summary(args)

            # Match this tool call to its result by index
            result_content = ""
            if idx < len(step_results):
                result_content = step_results[idx].get("content", "")
            elif step_results:
                # Fallback: concatenate all results for this step
                result_content = "\n".join(r.get("content", "") for r in step_results)

            output = _truncate(result_content, 500)

            tool_calls.append({
                "tool": fn,
                "args_summary": args_sum,
                "output": output,
            })

            # Track final Write to /app/
            if fn == "Write":
                file_path = args.get("file_path", "")
                if file_path.startswith("/app/"):
                    content_val = args.get("content", "")
                    final_output = {
                        "path": file_path,
                        "content": content_val,
                    }

            # Extract errors from result content
            if result_content:
                if "[error]" in result_content or re.search(r"^Exit code [^0]", result_content, re.MULTILINE):
                    # Grab first non-empty error line
                    for line in result_content.splitlines():
                        line = line.strip()
                        if line and not line.startswith("Elapsed:") and not line.startswith("Remaining:"):
                            errors.append(_truncate(line, 200))
                            break

        # Extract runtime signals from all results in this step
        for r in step_results:
            content = r.get("content", "")
            if content:
                sigs = _extract_runtime_signals(content)
                runtime_signals.extend(sigs)

        # Also check message field for system-reminder (appears in step messages)
        message = step.get("message", "")
        if message and "<system-reminder>" in message:
            sigs = _extract_runtime_signals(message)
            runtime_signals.extend(sigs)

    return {
        "total_steps": total_steps,
        "tool_calls": tool_calls,
        "runtime_signals": runtime_signals,
        "final_output": final_output,
        "errors": errors,
    }


def to_text(compressed: dict) -> str:
    """
    Format a compressed trajectory as readable text for API prompts.

    If the result exceeds 60000 chars, re-compresses with 200-char output
    truncation and retries once.
    """

    def _render(compressed: dict, output_max: int = 500) -> str:
        lines = []
        lines.append(f"Total steps: {compressed['total_steps']}")
        lines.append(f"Tool calls: {len(compressed['tool_calls'])}")
        lines.append("")

        if compressed["runtime_signals"]:
            lines.append("## Runtime Signals")
            for sig in compressed["runtime_signals"]:
                lines.append(f"  {sig}")
            lines.append("")

        if compressed["errors"]:
            lines.append("## Errors")
            for err in compressed["errors"]:
                lines.append(f"  - {err}")
            lines.append("")

        lines.append("## Tool Calls")
        for i, tc in enumerate(compressed["tool_calls"]):
            args_str = ", ".join(f"{k}={v!r}" for k, v in tc["args_summary"].items())
            output = _truncate(tc["output"], output_max)
            lines.append(f"[{i}] {tc['tool']}")
            lines.append(f"  args: {args_str}")
            lines.append(f"  output: {output}")

        lines.append("")
        if compressed["final_output"] is not None:
            fo = compressed["final_output"]
            lines.append("## Final Output")
            lines.append(f"  path: {fo['path']}")
            lines.append(f"  content: {_truncate(fo['content'], 500)}")
        else:
            lines.append("## Final Output")
            lines.append("  (none)")

        return "\n".join(lines)

    text = _render(compressed, output_max=500)
    if len(text) > 60000:
        text = _render(compressed, output_max=200)
    return text
