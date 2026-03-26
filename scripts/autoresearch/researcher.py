from __future__ import annotations

import json
import anthropic
from pathlib import Path


def _read_oauth_token() -> str:
    creds_path = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(creds_path.read_text())
        token = data["claudeAiOauth"]["accessToken"]
        if not token:
            raise ValueError("accessToken is empty")
        return token
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError) as e:
        raise RuntimeError(
            f"Cannot read Claude OAuth token from {creds_path}: {e}\n"
            "Please log in to Claude Code first (run 'claude' and authenticate)."
        ) from e


def _get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=_read_oauth_token())


def _load_prompt(name: str) -> str:
    prompt_path = Path(__file__).parent / "prompts" / f"{name}.md"
    return prompt_path.read_text()


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
    client = _get_client()

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

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        thinking={
            "type": "enabled",
            "budget_tokens": thinking_budget,
        },
        system=_load_prompt("propose"),
        messages=[{"role": "user", "content": user_content}],
    )

    # Extract text content (skip thinking blocks)
    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text
            break

    # Parse JSON from response (may be wrapped in ```json ... ```)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


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
    client = _get_client()

    user_content = "## Change Made\n\n"
    user_content += f"**File:** {change['file']}\n"
    user_content += f"**Hypothesis:** {change['hypothesis']}\n\n"
    user_content += f"## Before (result: {'PASS' if before_result else 'FAIL'})\n\n"
    user_content += before_trajectory + "\n\n"
    user_content += f"## After (result: {'PASS' if after_result else 'FAIL'})\n\n"
    user_content += after_trajectory

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        thinking={
            "type": "enabled",
            "budget_tokens": thinking_budget,
        },
        system=_load_prompt("evaluate"),
        messages=[{"role": "user", "content": user_content}],
    )

    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text
            break

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)
