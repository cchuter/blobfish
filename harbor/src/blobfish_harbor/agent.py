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
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from harbor.agents.installed.base import BaseInstalledAgent, ExecInput
from harbor.models.agent.context import AgentContext
from harbor.models.trial.result import AgentInfo, ModelInfo

DEFAULT_AGENT_ORG = "teamblobfish.com"
TEMPLATES_DIR = Path(__file__).parent / "templates"


class BlobfishAgent(BaseInstalledAgent):
    """Harbor agent that runs Claude or Codex in headless mode for benchmarks."""

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

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        escaped = shlex.quote(instruction)
        backend, model_name = self._resolve_backend_and_model()
        if backend == "codex":
            return self._create_codex_run_commands(escaped, model_name=model_name)
        return self._create_claude_run_commands(escaped, model_name=model_name)

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
        env["TASK_TIMEOUT_SECS"] = os.environ.get("HARBOR_TASK_TIMEOUT", "1800")
        prompt_variant = _resolve_prompt_variant(self._prompt_variant, model_name)
        claude_md = shlex.quote(_project_claude_md(prompt_variant))

        setup_cmd = (
            "mkdir -p $CLAUDE_CONFIG_DIR/debug $CLAUDE_CONFIG_DIR/projects/-app "
            "$CLAUDE_CONFIG_DIR/shell-snapshots $CLAUDE_CONFIG_DIR/statsig "
            "$CLAUDE_CONFIG_DIR/todos && "
            "if [ -d ~/.claude/skills ]; then "
            "cp -r ~/.claude/skills $CLAUDE_CONFIG_DIR/skills 2>/dev/null || true; "
            "fi && "
            f"printf %s {claude_md} > $CLAUDE_CONFIG_DIR/projects/-app/CLAUDE.md && "
            "timed() { local s=$(date +%s); timeout ${TIMED_LIMIT:-120} \"$@\"; local rc=$?; "
            "if [ $rc -eq 124 ]; then echo \"[TIMING] KILLED after $(($(date +%s)-s))s\"; "
            "else echo \"[TIMING] $(($(date +%s)-s))s (exit $rc)\"; fi; return $rc; } && "
            "{ echo '=== SYSTEM ===' && uname -a && "
            "cat /etc/os-release 2>/dev/null | head -3; "
            "echo '=== TOOLS ===' && "
            "command -v python3 python gcc g++ make cmake node npm cargo rustc go java javac; "
            "echo '=== /app ===' && ls /app; "
            "git -C /app log --oneline -3 2>/dev/null; "
            "echo '=== orient.txt written ==='; } > /tmp/orient.txt 2>&1"
        )

        run_cmd = (
            "export PATH=\"$HOME/.local/bin:$PATH\" && "
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


class CchuterAgent(BlobfishAgent):
    """Sample GitHub-username agent that matches BlobfishAgent behavior."""

    @staticmethod
    def name() -> str:
        return "cchuter"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("agent_name", "cchuter")
        kwargs.setdefault("agent_org", DEFAULT_AGENT_ORG)
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
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        return url

    host_gateway = os.environ.get("BLOBFISH_DOCKER_HOST_GATEWAY", "host.docker.internal").strip()
    if not host_gateway:
        return url

    netloc = parsed.netloc.replace(parsed.hostname, host_gateway, 1)
    return urlunparse(parsed._replace(netloc=netloc))
