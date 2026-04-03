"""Blobfish Harbor adapter for Terminal-Bench runs.

Open-source Harbor adapter with support for prompt variants and
backend/model selection.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from harbor.agents.installed.base import BaseInstalledAgent

try:
    from harbor.agents.installed.base import ExecInput
except ImportError:
    from dataclasses import dataclass, field

    @dataclass
    class ExecInput:
        command: str
        env: dict[str, str] = field(default_factory=dict)

from harbor.models.agent.context import AgentContext

try:
    from harbor.models.trial.result import AgentInfo, ModelInfo
except ImportError:
    AgentInfo = None
    ModelInfo = None

DEFAULT_AGENT_ORG = "teamblobfish.com"
TEMPLATES_DIR = Path(__file__).parent / "templates"


class BlobfishAgent(BaseInstalledAgent):
    """Harbor agent that runs Claude or Codex in headless mode for benchmarks."""

    SUPPORTS_ATIF: bool = True

    def __init__(
        self,
        backend: str = "claude",
        agent_name: str | None = None,
        agent_org: str = DEFAULT_AGENT_ORG,
        routing_table: str | Path | None = None,
        default_model: str | None = None,
        codex_model: str = "gpt-5.3-codex",
        openai_base_url: str | None = None,
        openai_api_key: str | None = None,
        reasoning_effort: str | None = "high",
        max_thinking_tokens: int | None = None,
        use_prompt: bool = True,
        prompt_variant: str = "auto",
        claude_runtime_profile: str = "blobfish",
        *args,
        **kwargs,
    ):
        requested_prompt_variant = _normalize_prompt_variant(prompt_variant)
        resolved_prompt_variant = _resolve_prompt_variant(
            requested_prompt_variant,
            kwargs.get("model_name") or default_model,
        )
        if not use_prompt or str(use_prompt).lower() == "false":
            kwargs["prompt_template_path"] = None
        else:
            kwargs.setdefault("prompt_template_path", _prompt_template_path(resolved_prompt_variant))

        super().__init__(*args, **kwargs)

        self._agent_name = _resolve_agent_name(agent_name)
        self._agent_org = (agent_org or DEFAULT_AGENT_ORG).strip() or DEFAULT_AGENT_ORG

        self._default_backend = (backend or "claude").strip().lower()
        if self._default_backend not in {"claude", "codex"}:
            raise ValueError("backend must be one of: claude, codex")
        self._prompt_variant = requested_prompt_variant

        self._default_model_selector = (default_model or "").strip() or None
        self._codex_model = (codex_model or "").strip() or None
        self._reasoning_effort = reasoning_effort
        self._max_thinking_tokens = max_thinking_tokens
        self._claude_runtime_profile = _normalize_claude_runtime_profile(claude_runtime_profile)

        self._routing: dict[str, dict] = {}
        if routing_table:
            path = Path(routing_table)
            if path.exists():
                try:
                    data = json.loads(path.read_text())
                    if isinstance(data, dict):
                        self._routing = data
                except json.JSONDecodeError:
                    self._routing = {}

        self._openai_base_url = (openai_base_url or "").strip() or None
        self._openai_api_key = (openai_api_key or "").strip() or None

    @staticmethod
    def name() -> str:
        return "blobfish"

    @property
    def _install_agent_template_path(self) -> Path:
        backend, _ = self._resolve_backend_and_model()
        if backend == "codex":
            return TEMPLATES_DIR / "install-codex.sh.j2"
        return TEMPLATES_DIR / "install-claude.sh.j2"

    def to_agent_info(self) -> AgentInfo:
        model_info = None
        if self._parsed_model_name and self._parsed_model_provider:
            model_info = ModelInfo(
                name=self._parsed_model_name,
                provider=self._parsed_model_provider,
            )
        return AgentInfo(
            name=self._agent_name,
            version=self.version() or "unknown",
            model_info=model_info,
        )

    def _get_task_name(self) -> str | None:
        trial_dir_name = self.logs_dir.parent.name
        if "__" in trial_dir_name:
            return trial_dir_name.rsplit("__", 1)[0]
        return None

    def _resolve_backend_and_model(self) -> tuple[str, str | None]:
        backend = self._default_backend
        model_name = self.model_name

        if model_name and "codex" in model_name.lower():
            backend = "codex"

        backend, model_name = _apply_selector(
            self._default_model_selector,
            backend=backend,
            model_name=model_name,
        )

        task_name = self._get_task_name()
        route = self._routing.get(task_name, {}) if task_name else {}
        if isinstance(route, dict):
            selectors = [route.get("backend"), route.get("model_name"), route.get("model")]
            for selector in selectors:
                backend, model_name = _apply_selector(
                    selector,
                    backend=backend,
                    model_name=model_name,
                )

        if _looks_incompatible_model_for_backend(model_name, backend):
            model_name = None

        if backend == "codex" and not model_name and self._codex_model:
            model_name = self._codex_model

        return backend, model_name

    # --- Harbor 0.1.x API (ExecInput-based) ---

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        escaped = shlex.quote(instruction)
        backend, model_name = self._resolve_backend_and_model()
        if backend == "codex":
            return self._create_codex_run_commands(escaped, model_name=model_name)
        return self._create_claude_run_commands(escaped, model_name=model_name)

    # --- Harbor 0.3.x API (async run/install) ---

    async def install(self, environment) -> None:
        """Install Claude Code CLI inside the container (Harbor 0.3+ API)."""
        from harbor.agents.installed.base import BaseInstalledAgent
        # Use exec_as_root if available (0.3+), otherwise skip
        if not hasattr(self, "exec_as_root"):
            return
        await self.exec_as_root(
            environment,
            command=(
                "if command -v apt-get &> /dev/null; then"
                "  apt-get update && apt-get install -y curl;"
                " fi"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "curl -fsSL https://claude.ai/install.sh | bash -s -- && "
                'export PATH="$HOME/.local/bin:$PATH" && '
                "claude --version"
            ),
        )

    async def run(self, instruction: str, environment, context) -> None:
        """Run the agent (Harbor 0.3+ API). Mirrors create_run_agent_commands logic."""
        if not hasattr(self, "exec_as_agent"):
            raise NotImplementedError(
                "BlobfishAgent.run() requires Harbor 0.3+ with exec_as_agent support"
            )
        escaped = shlex.quote(instruction)
        backend, model_name = self._resolve_backend_and_model()

        if backend == "codex":
            cmds = self._create_codex_run_commands(escaped, model_name=model_name)
        else:
            cmds = self._create_claude_run_commands(escaped, model_name=model_name)

        for cmd in cmds:
            await self.exec_as_agent(
                environment,
                command=cmd.command,
                env=cmd.env if cmd.env else None,
            )

    def _create_claude_run_commands(
        self, escaped_instruction: str, model_name: str | None = None
    ) -> list[ExecInput]:
        env: dict[str, str] = {
            "BLOBFISH_AGENT_NAME": self._agent_name,
            "BLOBFISH_AGENT_ORG": self._agent_org,
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
            "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        }

        base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
        if base_url:
            env["ANTHROPIC_BASE_URL"] = _rewrite_localhost_for_docker(base_url) or base_url

        if not env.get("ANTHROPIC_API_KEY") and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            token = _read_oauth_token()
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token

        env = {k: v for k, v in env.items() if v}

        if model_name:
            selected_model = model_name.split("/", 1)[1] if "/" in model_name else model_name
            env["ANTHROPIC_MODEL"] = selected_model

        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["IS_SANDBOX"] = "1"
        env["FORCE_AUTO_BACKGROUND_TASKS"] = "1"
        env["ENABLE_BACKGROUND_TASKS"] = "1"
        env["CLAUDE_CONFIG_DIR"] = "/logs/agent/sessions"

        if self._max_thinking_tokens is not None:
            env["MAX_THINKING_TOKENS"] = str(self._max_thinking_tokens)
        elif "MAX_THINKING_TOKENS" in os.environ:
            env["MAX_THINKING_TOKENS"] = os.environ["MAX_THINKING_TOKENS"]
        else:
            env["MAX_THINKING_TOKENS"] = "10000"

        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = os.environ.get(
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "48000"
        )
        env["TASK_START_EPOCH"] = str(int(time.time()))
        task_timeout_sec = _resolve_task_timeout_sec(self._get_task_name())
        if task_timeout_sec is not None:
            env["TASK_TIMEOUT_SECS"] = str(task_timeout_sec)
        existing_path = os.environ.get("PATH", "")
        env["PATH"] = f"/tmp/blobfish-bin:{existing_path}" if existing_path else "/tmp/blobfish-bin"
        prompt_variant = _resolve_prompt_variant(self._prompt_variant, model_name)
        claude_md_text = _project_claude_md(prompt_variant)
        if self._claude_runtime_profile == "simple":
            setup_cmd = _claude_simple_setup_cmd(claude_md=claude_md_text)
        else:
            setup_cmd = _claude_setup_cmd(
                claude_md=claude_md_text,
                session_start_hook=_hook_template_text("session-start-hook.sh"),
                hook_common_pl=_hook_template_text("hook_common.pl"),
                pre_tool_hook=_hook_template_text("pre-tool-hook.pl"),
                post_tool_hook=_hook_template_text("post-tool-hook.pl"),
                stop_hook=_hook_template_text("stop-hook.pl"),
                task_completed_hook=_hook_template_text("task-completed-hook.pl"),
                constraint_rule=_project_rule_text("constraint-first-debugging.md"),
                constraint_skill=_project_skill_text("constraint-first-debugging/SKILL.md"),
                deadline_rule=_project_rule_text("deadline-aware-delivery.md"),
                deadline_skill=_project_skill_text("deadline-aware-delivery/SKILL.md"),
            )

        run_cmd = (
            "umask 0022 && "
            "export PATH=\"/tmp/blobfish-bin:$HOME/.local/bin:$PATH\" && "
            "claude --verbose --output-format stream-json "
            "--permission-mode bypassPermissions "
            f"-p {escaped_instruction} 2>&1 </dev/null | tee /logs/agent/blobfish-output.txt"
        )

        return [ExecInput(command=setup_cmd, env=env), ExecInput(command=run_cmd, env=env)]

    def _create_codex_run_commands(
        self, escaped_instruction: str, model_name: str | None = None
    ) -> list[ExecInput]:
        env: dict[str, str] = {
            "BLOBFISH_AGENT_NAME": self._agent_name,
            "BLOBFISH_AGENT_ORG": self._agent_org,
            "CODEX_HOME": "/root/.codex",
        }

        openai_base_url = self._openai_base_url or os.environ.get("OPENAI_BASE_URL")
        openai_base_url = _rewrite_localhost_for_docker(openai_base_url)
        if openai_base_url:
            env["OPENAI_BASE_URL"] = openai_base_url

        env["_CODEX_AUTH_B64"] = base64.b64encode(
            _read_codex_auth(
                openai_base_url=openai_base_url,
                explicit_api_key=self._openai_api_key,
            ).encode()
        ).decode()

        setup_cmd = (
            'mkdir -p /tmp/codex-secrets "$CODEX_HOME"\n'
            'echo "$_CODEX_AUTH_B64" | base64 -d > /tmp/codex-secrets/auth.json\n'
            'ln -sf /tmp/codex-secrets/auth.json "$CODEX_HOME/auth.json"'
        )

        model_flag = ""
        if model_name:
            model = model_name
            if model.startswith("openai/"):
                model = model.split("/", 1)[1]
            model_flag = f"--model {shlex.quote(model)} "

        reasoning_flag = (
            f"-c model_reasoning_effort={self._reasoning_effort} "
            if self._reasoning_effort
            else ""
        )

        run_cmd = (
            'trap \'rm -rf /tmp/codex-secrets "$CODEX_HOME/auth.json"\' EXIT TERM INT; '
            "codex exec --json "
            "--dangerously-bypass-approvals-and-sandbox "
            "--skip-git-repo-check "
            f"{model_flag}"
            "--enable unified_exec "
            f"{reasoning_flag}"
            "-- "
            f"{escaped_instruction} 2>&1 </dev/null | tee /logs/agent/blobfish-output.txt"
        )

        return [ExecInput(command=setup_cmd, env=env), ExecInput(command=run_cmd, env=env)]

    def populate_context_post_run(self, context: AgentContext) -> None:
        output_path = self.logs_dir / "command-0" / "stdout.txt"
        alt_path = self.logs_dir / "command-1" / "stdout.txt"
        if alt_path.exists():
            output_path = alt_path

        total_input = 0
        total_output = 0
        total_cache = 0
        found_usage = False

        if output_path.exists():
            for line in output_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = None
                if isinstance(event, dict):
                    msg = event.get("message")
                    if isinstance(msg, dict):
                        usage = msg.get("usage")
                if isinstance(usage, dict):
                    found_usage = True
                    total_input += usage.get("input_tokens", 0)
                    total_output += usage.get("output_tokens", 0)
                    total_cache += usage.get("cache_read_input_tokens", 0)

                usage = event.get("usage") if isinstance(event, dict) else None
                if isinstance(usage, dict) and "input_tokens" in usage:
                    found_usage = True
                    total_input += usage.get("input_tokens", 0)
                    total_output += usage.get("output_tokens", 0)

        if found_usage:
            context.n_input_tokens = total_input + total_cache
            context.n_output_tokens = total_output
            context.n_cache_tokens = total_cache

        # Generate ATIF trajectory from Claude Code session logs
        self._write_atif_trajectory(context)


    def _write_atif_trajectory(self, context: AgentContext) -> None:
        """Generate ATIF trajectory.json by delegating to Harbor's ClaudeCode converter."""
        try:
            from harbor.agents.installed.claude_code import ClaudeCode
        except ImportError:
            return

        # Create a thin ClaudeCode instance so _convert_events_to_trajectory
        # can call its own helper methods (e.g. _extract_text_reasoning_tool_uses).
        proxy = object.__new__(ClaudeCode)
        proxy.logs_dir = self.logs_dir
        proxy.model_name = self.model_name

        session_dir = ClaudeCode._get_session_dir(proxy)
        if not session_dir:
            return

        try:
            trajectory = proxy._convert_events_to_trajectory(session_dir)
        except Exception as exc:
            print(f"Failed to convert Claude Code events to ATIF trajectory: {exc}")
            return

        if not trajectory:
            return

        trajectory_path = self.logs_dir / "trajectory.json"
        try:
            with open(trajectory_path, "w", encoding="utf-8") as handle:
                json.dump(
                    trajectory.to_json_dict(), handle, indent=2, ensure_ascii=False
                )
            print(f"Wrote ATIF trajectory to {trajectory_path}")
        except OSError as exc:
            print(f"Failed to write ATIF trajectory {trajectory_path}: {exc}")


class CchuterAgent(BlobfishAgent):
    """Sample GitHub-username agent that matches BlobfishAgent behavior."""

    @staticmethod
    def name() -> str:
        return "cchuter"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("agent_name", "cchuter")
        kwargs.setdefault("agent_org", DEFAULT_AGENT_ORG)
        super().__init__(*args, **kwargs)


class BlobfishSimpleAgent(BlobfishAgent):
    """Minimal Claude agent intended to stay close to the baseline behavior."""

    @staticmethod
    def name() -> str:
        return "blobfish-simple"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("prompt_variant", "full")
        kwargs.setdefault("claude_runtime_profile", "simple")
        super().__init__(*args, **kwargs)


def _resolve_agent_name(explicit_name: str | None) -> str:
    candidate = (
        explicit_name
        or os.environ.get("BLOBFISH_AGENT_NAME")
        or os.environ.get("GITHUB_ACTOR")
        or os.environ.get("USER")
        or "blobfish"
    )
    candidate = candidate.strip().lower()
    candidate = re.sub(r"[^a-z0-9-]", "-", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
    if not candidate:
        return "blobfish"
    return candidate[:39]


def _normalize_claude_runtime_profile(value: str | None) -> str:
    profile = (value or "blobfish").strip().lower()
    if profile not in {"blobfish", "simple"}:
        raise ValueError("claude_runtime_profile must be one of: blobfish, simple")
    return profile


def _normalize_prompt_variant(prompt_variant: str | None) -> str:
    value = (prompt_variant or "auto").strip().lower()
    if not value:
        value = "auto"
    aliases = {
        "minimax": "minimax-m2.5",
        "minimax-m25": "minimax-m2.5",
        "minimax_m25": "minimax-m2.5",
    }
    value = aliases.get(value, value)
    if value not in {"auto", "full", "slim", "minimax-m2.5"}:
        raise ValueError("prompt_variant must be one of: auto, full, slim, minimax-m2.5")
    return value


def _resolve_prompt_variant(prompt_variant: str, model_name: str | None) -> str:
    if prompt_variant != "auto":
        return prompt_variant
    if _is_minimax_m25(model_name):
        return "minimax-m2.5"
    return "full"


def _is_minimax_m25(model_name: str | None) -> bool:
    if not model_name:
        return False
    low = model_name.lower()
    return "minimax-m2.5" in low


def _prompt_template_path(prompt_variant: str) -> Path:
    if prompt_variant == "slim":
        return TEMPLATES_DIR / "prompt-slim.md.j2"
    if prompt_variant == "minimax-m2.5":
        return TEMPLATES_DIR / "prompt-minimax-m25.md.j2"
    return TEMPLATES_DIR / "prompt.md.j2"


def _project_claude_md(prompt_variant: str) -> str:
    if prompt_variant == "minimax-m2.5":
        path = TEMPLATES_DIR / "claude-project-minimax-m25.md"
    else:
        path = TEMPLATES_DIR / "claude-project-default.md"
    return path.read_text()


def _hook_template_text(name: str) -> str:
    return (TEMPLATES_DIR / "hooks" / name).read_text()


def _project_rule_text(name: str) -> str:
    return (TEMPLATES_DIR / "project-rules" / name).read_text()


def _project_skill_text(path: str) -> str:
    return (TEMPLATES_DIR / "project-skills" / path).read_text()


def _claude_settings_json() -> str:
    settings = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/session-start-hook"}],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/pre-tool-hook"}],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/post-tool-hook"}],
                }
            ],
            "PostToolUseFailure": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/post-tool-hook"}],
                }
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/stop-hook"}],
                }
            ],
            "TaskCompleted": [
                {
                    "matcher": "",
                    "hooks": [{"type": "command", "command": "/tmp/blobfish-bin/task-completed-hook"}],
                }
            ],
        }
    }
    return json.dumps(settings, indent=2)


def _claude_simple_setup_cmd(*, claude_md: str) -> str:
    timed_script = """#!/bin/sh
s=$(date +%s)
timeout "${TIMED_LIMIT:-120}" "$@"
rc=$?
if [ "$rc" -eq 124 ]; then
  echo "[TIMING] KILLED after $(($(date +%s)-s))s"
else
  echo "[TIMING] $(($(date +%s)-s))s (exit $rc)"
fi
exit "$rc"
"""
    return (
        "mkdir -p $CLAUDE_CONFIG_DIR/debug $CLAUDE_CONFIG_DIR/projects/-app "
        "$CLAUDE_CONFIG_DIR/shell-snapshots $CLAUDE_CONFIG_DIR/statsig "
        "$CLAUDE_CONFIG_DIR/todos /tmp/blobfish-bin && "
        f"printf %s {shlex.quote(claude_md)} > $CLAUDE_CONFIG_DIR/projects/-app/CLAUDE.md && "
        f"printf %s {shlex.quote(timed_script)} > /tmp/blobfish-bin/timed && "
        "chmod +x /tmp/blobfish-bin/timed && "
        "{ echo '=== SYSTEM ===' && uname -a && "
        "cat /etc/os-release 2>/dev/null | head -3; "
        "echo '=== TOOLS ===' && "
        "command -v python3 python gcc g++ make cmake node npm cargo rustc go java javac timed; "
        "echo '=== /app ===' && ls /app; "
        "git -C /app log --oneline -3 2>/dev/null; "
        "echo '=== orient.txt written ==='; } > /tmp/orient.txt 2>&1"
    )


def _claude_setup_cmd(
    *,
    claude_md: str,
    session_start_hook: str,
    hook_common_pl: str,
    pre_tool_hook: str,
    post_tool_hook: str,
    stop_hook: str,
    task_completed_hook: str,
    constraint_rule: str,
    constraint_skill: str,
    deadline_rule: str,
    deadline_skill: str,
) -> str:
    timed_script = """#!/bin/sh
s=$(date +%s)
timeout "${TIMED_LIMIT:-120}" "$@"
rc=$?
if [ "$rc" -eq 124 ]; then
  echo "[TIMING] KILLED after $(($(date +%s)-s))s"
else
  echo "[TIMING] $(($(date +%s)-s))s (exit $rc)"
fi
exit "$rc"
"""
    run_state = """# Run state
- Goal:
- Best known result:
- Next step:
"""
    status_script = """#!/bin/sh
now=$(date +%s)
start=${TASK_START_EPOCH:-$now}
timeout=${TASK_TIMEOUT_SECS:-unknown}
elapsed=$((now-start))
echo "Elapsed: ${elapsed}s"
if [ "$timeout" != "unknown" ]; then
  remaining=$((timeout-elapsed))
  echo "Remaining: ${remaining}s of ${timeout}s"
fi
if [ -f /tmp/run_state.md ]; then
  echo "=== /tmp/run_state.md ==="
  cat /tmp/run_state.md
fi
"""
    return (
        "mkdir -p $CLAUDE_CONFIG_DIR/debug $CLAUDE_CONFIG_DIR/projects/-app "
        "$CLAUDE_CONFIG_DIR/shell-snapshots $CLAUDE_CONFIG_DIR/statsig "
        "$CLAUDE_CONFIG_DIR/todos /tmp/blobfish-bin /tmp/blobfish-hook "
        "/app/.claude/rules /app/.claude/skills/constraint-first-debugging "
        "/app/.claude/skills/deadline-aware-delivery && "
        "if [ -d ~/.claude/skills ]; then "
        "cp -r ~/.claude/skills $CLAUDE_CONFIG_DIR/skills 2>/dev/null || true; "
        "fi && "
        f"printf %s {shlex.quote(claude_md)} > $CLAUDE_CONFIG_DIR/projects/-app/CLAUDE.md && "
        f"printf %s {shlex.quote(constraint_rule)} > /app/.claude/rules/constraint-first-debugging.md && "
        f"printf %s {shlex.quote(constraint_skill)} > /app/.claude/skills/constraint-first-debugging/SKILL.md && "
        f"printf %s {shlex.quote(deadline_rule)} > /app/.claude/rules/deadline-aware-delivery.md && "
        f"printf %s {shlex.quote(deadline_skill)} > /app/.claude/skills/deadline-aware-delivery/SKILL.md && "
        f"printf %s {shlex.quote(timed_script)} > /tmp/blobfish-bin/timed && "
        "chmod +x /tmp/blobfish-bin/timed && "
        f"printf %s {shlex.quote(run_state)} > /tmp/run_state.md && "
        f"printf %s {shlex.quote(status_script)} > /tmp/blobfish-bin/status && "
        "chmod +x /tmp/blobfish-bin/status && "
        f"printf %s {shlex.quote(session_start_hook)} > /tmp/blobfish-bin/session-start-hook && "
        "chmod +x /tmp/blobfish-bin/session-start-hook && "
        f"printf %s {shlex.quote(hook_common_pl)} > /tmp/blobfish-bin/hook_common.pl && "
        f"printf %s {shlex.quote(pre_tool_hook)} > /tmp/blobfish-bin/pre-tool-hook && "
        f"printf %s {shlex.quote(post_tool_hook)} > /tmp/blobfish-bin/post-tool-hook && "
        f"printf %s {shlex.quote(stop_hook)} > /tmp/blobfish-bin/stop-hook && "
        f"printf %s {shlex.quote(task_completed_hook)} > /tmp/blobfish-bin/task-completed-hook && "
        "chmod +x /tmp/blobfish-bin/pre-tool-hook /tmp/blobfish-bin/post-tool-hook /tmp/blobfish-bin/stop-hook /tmp/blobfish-bin/task-completed-hook && "
        f"printf %s {shlex.quote(_claude_settings_json())} > $CLAUDE_CONFIG_DIR/settings.json && "
        "{ echo '=== SYSTEM ===' && uname -a && "
        "cat /etc/os-release 2>/dev/null | head -3; "
        "echo '=== TOOLS ===' && "
        "command -v python3 python gcc g++ make cmake node npm cargo rustc go java javac timed status perl; "
        "echo '=== /app ===' && ls /app; "
        "git -C /app log --oneline -3 2>/dev/null; "
        "echo '=== orient.txt written ==='; "
        "echo '=== STATUS ==='; status; } > /tmp/orient.txt 2>&1"
    )


def _resolve_task_timeout_sec(task_name: str | None) -> int | None:
    if not task_name:
        return None

    task_tomls = sorted(
        (Path.home() / ".cache" / "harbor" / "tasks").glob(f"*/{task_name}/task.toml"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for task_toml in task_tomls:
        try:
            data = tomllib.loads(task_toml.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        timeout = data.get("agent", {}).get("timeout_sec")
        if timeout is None:
            continue
        try:
            return int(float(timeout))
        except (TypeError, ValueError):
            continue
    return None


def _read_oauth_token() -> str | None:
    creds_path = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(creds_path.read_text())
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_codex_auth(
    openai_base_url: str | None = None,
    explicit_api_key: str | None = None,
) -> str:
    if openai_base_url:
        api_key = explicit_api_key or os.environ.get("OPENAI_API_KEY") or "lm-studio"
        return json.dumps({"OPENAI_API_KEY": api_key})

    codex_auth_path = Path.home() / ".codex" / "auth.json"
    try:
        data = json.loads(codex_auth_path.read_text())
        if data.get("tokens") or data.get("auth_mode") == "chatgpt":
            return json.dumps(data)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    api_key = os.environ.get("OPENAI_API_KEY", "")
    return json.dumps({"OPENAI_API_KEY": api_key})


def _apply_selector(
    selector: object, backend: str, model_name: str | None
) -> tuple[str, str | None]:
    if not isinstance(selector, str):
        return backend, model_name
    value = selector.strip()
    if not value:
        return backend, model_name

    low = value.lower()
    if low in {"claude", "codex"}:
        return low, model_name

    inferred = _infer_backend_from_model(value)
    return inferred or backend, value


def _infer_backend_from_model(model_name: str) -> str | None:
    low = model_name.lower()
    if "codex" in low or low.startswith("openai/"):
        return "codex"
    if "claude" in low or low.startswith("anthropic/"):
        return "claude"
    return None


def _looks_incompatible_model_for_backend(model_name: str | None, backend: str) -> bool:
    if not model_name:
        return False
    low = model_name.lower()
    if backend == "claude":
        return "codex" in low or low.startswith("openai/")
    if backend == "codex":
        return "claude" in low or low.startswith("anthropic/")
    return False


def _rewrite_localhost_for_docker(url: str | None) -> str | None:
    """Rewrite localhost URLs so they reach the host from inside a Docker container.

    On macOS/Windows (Docker Desktop), host.docker.internal works out of the box.
    On Linux, Docker bridge containers can't reach host via localhost —
    use the bridge gateway IP (typically 172.17.0.1) instead.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return url

    if sys.platform == "linux":
        default_gateway = _docker_bridge_gateway() or "172.17.0.1"
    else:
        default_gateway = "host.docker.internal"

    host_gateway = os.environ.get("BLOBFISH_DOCKER_HOST_GATEWAY", default_gateway).strip()
    if not host_gateway:
        return url

    netloc = parsed.netloc.replace(parsed.hostname, host_gateway, 1)
    return urlunparse(parsed._replace(netloc=netloc))


def _docker_bridge_gateway() -> str | None:
    """Return the gateway IP of the default Docker bridge network."""
    try:
        result = subprocess.run(
            ["docker", "network", "inspect", "bridge",
             "--format", "{{(index .IPAM.Config 0).Gateway}}"],
            capture_output=True, text=True, timeout=5,
        )
        ip = result.stdout.strip()
        return ip if ip else None
    except (OSError, subprocess.TimeoutExpired):
        return None
