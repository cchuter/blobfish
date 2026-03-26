from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def _load_prompt(name: str) -> str:
    prompt_path = Path(__file__).parent / "prompts" / f"{name}.md"
    return prompt_path.read_text()


def _call_claude(system_prompt: str, user_content: str, model: str) -> str:
    """Call claude CLI in print mode. Returns the text response."""
    result = subprocess.run(
        [
            "claude",
            "-p",
            "--output-format", "json",
            "--model", model,
            "--system-prompt", system_prompt,
            "--no-session-persistence",
        ],
        input=user_content,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}): {result.stderr[:500]}"
        )
    # Parse the JSON output to extract the text response
    try:
        data = json.loads(result.stdout)
        # claude --output-format json returns {"type":"result","result":"..."}
        return data.get("result", result.stdout)
    except json.JSONDecodeError:
        return result.stdout


def _parse_json_response(text: str) -> dict:
    """Extract JSON from a response that may contain markdown fences or prose."""
    text = text.strip()
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try extracting from markdown code fence
    fence_match = re.search(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    # Try finding the outermost JSON object
    brace_match = re.search(r'\{[\s\S]*\}', text)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError(f"Could not parse JSON from response: {text[:200]}")


def propose(
    research_log: str,
    agent_files: dict[str, str],
    trajectory_text: str,
    apply_error: str | None,
    model: str,
    max_tokens: int,
    thinking_budget: int,
) -> dict:
    """Call Claude to propose a change.
    Returns dict with keys: file, hypothesis, old_string, new_string
    OR: file, hypothesis, full_content"""
    user_content = "## Research Log\n\n" + research_log + "\n\n"
    user_content += "## Current Agent Files\n\n"
    for path, content in agent_files.items():
        user_content += f"### {path}\n```\n{content}\n```\n\n"
    user_content += "## Last Trial Trajectory\n\n" + trajectory_text

    if apply_error:
        user_content += (
            f"\n\n## Previous Apply Error\n\n"
            f"Your last proposed diff failed to apply: {apply_error}\n"
            f"Please ensure old_string matches the file content exactly."
        )

    text = _call_claude(_load_prompt("propose"), user_content, model)
    return _parse_json_response(text)


def evaluate(
    change: dict,
    before_trajectory: str,
    before_result: int,
    after_trajectory: str,
    after_result: int,
    model: str,
    max_tokens: int,
    thinking_budget: int,
) -> dict:
    """Call Claude to evaluate a change.
    Returns dict with keys: verdict, reasoning, key_observations, next_direction"""
    user_content = "## Change Made\n\n"
    user_content += f"**File:** {change['file']}\n"
    user_content += f"**Hypothesis:** {change['hypothesis']}\n\n"
    user_content += f"## Before (result: {'PASS' if before_result else 'FAIL'})\n\n"
    user_content += before_trajectory + "\n\n"
    user_content += f"## After (result: {'PASS' if after_result else 'FAIL'})\n\n"
    user_content += after_trajectory

    text = _call_claude(_load_prompt("evaluate"), user_content, model)
    return _parse_json_response(text)
