"""Research log module for autoresearch system.

Manages an append-only markdown file that tracks experiment history.
"""

import os
import re
from datetime import datetime, timezone


def append_entry(log_path: str, entry: dict) -> None:
    """Append a research log entry. entry has keys: iteration, timestamp,
    target_task, hypothesis, changed_file, diff_summary, result, verdict,
    trajectory_analysis, conclusion, next_direction.
    Writes markdown format like:

    ## Iteration N — YYYY-MM-DDTHH:MM:SS
    **Target task:** <task name>
    **Hypothesis:** <text>
    **Changed file:** <path>
    **Diff summary:** <text>
    **Result:** PASS | FAIL
    **Verdict:** BETTER | WORSE | NEUTRAL
    **Trajectory analysis:** <text>
    **Conclusion:** <text>
    **Next direction:** <text>
    """
    lines = [
        f"\n## Iteration {entry['iteration']} — {entry['timestamp']}\n",
        f"**Target task:** {entry['target_task']}\n",
        f"**Hypothesis:** {entry['hypothesis']}\n",
        f"**Changed file:** {entry['changed_file']}\n",
        f"**Diff summary:** {entry['diff_summary']}\n",
        f"**Result:** {entry['result']}\n",
        f"**Verdict:** {entry['verdict']}\n",
        f"**Trajectory analysis:** {entry['trajectory_analysis']}\n",
        f"**Conclusion:** {entry['conclusion']}\n",
        f"**Next direction:** {entry['next_direction']}\n",
    ]
    with open(log_path, "a") as f:
        f.writelines(lines)


def last_iteration(log_path: str) -> int:
    """Parse log, return the iteration number of the last COMPLETE entry.
    A complete entry has all fields through '**Next direction:**'.
    Returns 0 if log is empty or missing."""
    if not os.path.exists(log_path):
        return 0

    with open(log_path, "r") as f:
        content = f.read()

    if not content.strip():
        return 0

    # Split into sections by "## Iteration" headers
    # A complete entry must contain all required fields
    required_fields = [
        r"\*\*Target task:\*\*",
        r"\*\*Hypothesis:\*\*",
        r"\*\*Changed file:\*\*",
        r"\*\*Diff summary:\*\*",
        r"\*\*Result:\*\*",
        r"\*\*Verdict:\*\*",
        r"\*\*Trajectory analysis:\*\*",
        r"\*\*Conclusion:\*\*",
        r"\*\*Next direction:\*\*",
    ]

    # Find all iteration headers and their positions
    header_pattern = re.compile(r"^## Iteration (\d+) — ", re.MULTILINE)
    matches = list(header_pattern.finditer(content))

    last_complete = 0
    for i, match in enumerate(matches):
        iteration_num = int(match.group(1))
        # Get the text of this entry (up to the next header or end of file)
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        entry_text = content[start:end]

        # Check all required fields are present
        if all(re.search(field, entry_text) for field in required_fields):
            last_complete = iteration_num

    return last_complete


def truncate_incomplete(log_path: str) -> None:
    """If the last entry is incomplete (missing fields), remove it.
    Used by --resume to clean up after crashes."""
    if not os.path.exists(log_path):
        return

    with open(log_path, "r") as f:
        content = f.read()

    if not content.strip():
        return

    required_fields = [
        r"\*\*Target task:\*\*",
        r"\*\*Hypothesis:\*\*",
        r"\*\*Changed file:\*\*",
        r"\*\*Diff summary:\*\*",
        r"\*\*Result:\*\*",
        r"\*\*Verdict:\*\*",
        r"\*\*Trajectory analysis:\*\*",
        r"\*\*Conclusion:\*\*",
        r"\*\*Next direction:\*\*",
    ]

    # Find all "## Iteration" headers (not "## Skipped")
    header_pattern = re.compile(r"^(## Iteration \d+ — )", re.MULTILINE)
    matches = list(header_pattern.finditer(content))

    if not matches:
        return

    last_match = matches[-1]
    last_start = last_match.start()
    last_entry_text = content[last_start:]

    # Check if the last entry is complete
    if all(re.search(field, last_entry_text) for field in required_fields):
        # Already complete, nothing to truncate
        return

    # Remove the incomplete last entry
    # Preserve any trailing newline from the previous content
    truncated = content[:last_start].rstrip("\n")
    if truncated:
        truncated += "\n"

    with open(log_path, "w") as f:
        f.write(truncated)


def append_skip(log_path: str, iteration: int, reason: str) -> None:
    """Append a short skip entry for apply failures. Does not count
    as a real iteration. Format:

    ## Skipped — YYYY-MM-DDTHH:MM:SS
    **Reason:** <reason>
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    lines = [
        f"\n## Skipped — {timestamp}\n",
        f"**Reason:** {reason}\n",
    ]
    with open(log_path, "a") as f:
        f.writelines(lines)
