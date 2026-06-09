"""Pi coding-agent Harbor adapter for local DS4/Terminal-Bench runs."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent, ExecInput
from harbor.models.agent.context import AgentContext
from harbor.models.trial.result import AgentInfo, ModelInfo

from .agent import (
    DEFAULT_AGENT_ORG,
    TEMPLATES_DIR,
    _normalize_prompt_variant,
    _prompt_template_path,
    _project_claude_md,
    _resolve_agent_name,
    _resolve_prompt_variant,
    _resolve_task_timeout_sec,
    _rewrite_localhost_for_docker,
)


class BlobfishPiAgent(BaseInstalledAgent):
    """Harbor agent that runs Pi in JSON mode against a DS4 Anthropic endpoint."""

    def __init__(
        self,
        backend: str | None = None,
        agent_name: str | None = None,
        agent_org: str = DEFAULT_AGENT_ORG,
        default_model: str | None = None,
        api_base: str | None = None,
        max_thinking_tokens: int | str | None = 1024,
        pi_provider: str = "ds4",
        pi_thinking: str = "high",
        context_tokens: int | str = 196608,
        output_tokens: int | str = 8192,
        temperature: float | str | None = 0.0,
        hard_gate_tool_calls: int | str = 6,
        hard_gate_elapsed_pct: float | str = 15,
        task_timeout_multiplier: float | str | None = None,
        use_prompt: bool = True,
        prompt_variant: str = "auto",
        *args: Any,
        **kwargs: Any,
    ):
        del backend

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
        self._default_model = (default_model or "").strip() or None
        self._api_base = (api_base or "").strip() or None
        self._max_thinking_tokens = _optional_int(max_thinking_tokens)
        self._pi_provider = (pi_provider or "ds4").strip() or "ds4"
        self._pi_thinking = (pi_thinking or "high").strip().lower() or "high"
        self._context_tokens = _int_value(context_tokens, 196608)
        self._output_tokens = _int_value(output_tokens, 8192)
        self._temperature = _optional_float(temperature)
        self._hard_gate_tool_calls = _int_value(hard_gate_tool_calls, 6)
        self._hard_gate_elapsed_pct = _float_value(hard_gate_elapsed_pct, 15.0)
        self._task_timeout_multiplier = _optional_float(task_timeout_multiplier) or 1.0
        self._prompt_variant = requested_prompt_variant

    @staticmethod
    def name() -> str:
        return "blobfish-pi"

    @property
    def _install_agent_template_path(self) -> Path:
        return TEMPLATES_DIR / "install-pi.sh.j2"

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

    def _resolve_model_name(self) -> str:
        model = self._default_model or self.model_name or "deepseek-v4-flash"
        return model.split("/", 1)[1] if "/" in model else model

    def _resolve_api_base(self) -> str:
        base_url = self._api_base or os.environ.get("ANTHROPIC_BASE_URL") or "http://127.0.0.1:8081"
        return _rewrite_localhost_for_docker(base_url) or base_url

    def create_run_agent_commands(self, instruction: str) -> list[ExecInput]:
        model_name = self._resolve_model_name()
        api_base = self._resolve_api_base()
        prompt_variant = _resolve_prompt_variant(self._prompt_variant, model_name)
        task_timeout_sec = _resolve_task_timeout_sec(
            self._get_task_name(),
            multiplier=self._task_timeout_multiplier,
        )
        system_prompt = _project_claude_md(prompt_variant)

        env: dict[str, str] = {
            "BLOBFISH_AGENT_NAME": self._agent_name,
            "BLOBFISH_AGENT_ORG": self._agent_org,
            "BLOBFISH_PI_MODEL": model_name,
            "BLOBFISH_PI_PROVIDER": self._pi_provider,
            "BLOBFISH_PI_API_BASE": api_base,
            "BLOBFISH_PI_CONTEXT_TOKENS": str(self._context_tokens),
            "BLOBFISH_PI_OUTPUT_TOKENS": str(self._output_tokens),
            "BLOBFISH_PI_HARD_GATE_TOOL_CALLS": str(self._hard_gate_tool_calls),
            "BLOBFISH_PI_HARD_GATE_ELAPSED_PCT": str(self._hard_gate_elapsed_pct),
            "BLOBFISH_PI_SYSTEM_PROMPT_FILE": "/tmp/blobfish-pi/system.md",
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "no-key") or "no-key",
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
            "PI_SKIP_VERSION_CHECK": "1",
        }
        if self._max_thinking_tokens is not None:
            env["BLOBFISH_PI_MAX_THINKING_TOKENS"] = str(self._max_thinking_tokens)
        if self._temperature is not None:
            env["BLOBFISH_PI_TEMPERATURE"] = str(self._temperature)
        if task_timeout_sec is not None:
            env["TASK_TIMEOUT_SECS"] = str(task_timeout_sec)

        setup_cmd = _pi_setup_cmd(
            models_json=_pi_models_json(
                provider=self._pi_provider,
                model_name=model_name,
                api_base=api_base,
                context_tokens=self._context_tokens,
                output_tokens=self._output_tokens,
            ),
            extension_source=_pi_extension_source(),
            system_prompt=system_prompt,
        )

        thinking_flag = ""
        if self._pi_thinking:
            thinking_flag = f"--thinking {shlex.quote(self._pi_thinking)} "

        escaped_instruction = shlex.quote(instruction)
        run_cmd = (
            "export NVM_DIR=\"$HOME/.nvm\"; "
            "[ -s \"$NVM_DIR/nvm.sh\" ] && . \"$NVM_DIR/nvm.sh\"; "
            "export PATH=\"$HOME/.local/bin:$PATH\"; "
            "pi --mode json --offline --no-session "
            "--no-extensions -e /tmp/blobfish-pi/blobfish-pi.ts "
            "--no-context-files --no-skills --no-prompt-templates --no-themes "
            f"--provider {shlex.quote(self._pi_provider)} "
            f"--model {shlex.quote(model_name)} "
            f"{thinking_flag}"
            f"-- {escaped_instruction} "
            "2>&1 | tee /logs/agent/blobfish-pi-output.jsonl"
        )

        return [ExecInput(command=setup_cmd, env=env), ExecInput(command=run_cmd, env=env)]

    def populate_context_post_run(self, context: AgentContext) -> None:
        output_path = self.logs_dir / "command-1" / "stdout.txt"
        if not output_path.exists():
            return

        total_input = 0
        total_output = 0
        total_cache = 0
        found_usage = False
        seen_messages: set[str] = set()

        for line in output_path.read_text(errors="replace").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            message = event.get("message") if isinstance(event, dict) else None
            if event.get("type") == "message_end" and isinstance(message, dict):
                msg_id = str(message.get("id") or id(message))
                if msg_id in seen_messages:
                    continue
                seen_messages.add(msg_id)
                usage = message.get("usage")
                if isinstance(usage, dict):
                    found_usage = True
                    input_tokens = int(usage.get("input") or usage.get("input_tokens") or 0)
                    output_tokens = int(usage.get("output") or usage.get("output_tokens") or 0)
                    cache_tokens = int(usage.get("cacheRead") or 0) + int(
                        usage.get("cacheWrite") or 0
                    )
                    total_input += input_tokens + cache_tokens
                    total_output += output_tokens
                    total_cache += cache_tokens

        if found_usage:
            context.n_input_tokens = total_input
            context.n_output_tokens = total_output
            context.n_cache_tokens = total_cache


def _pi_setup_cmd(*, models_json: str, extension_source: str, system_prompt: str) -> str:
    return (
        "mkdir -p /tmp/blobfish-pi /root/.pi/agent /logs/agent && "
        f"printf %s {shlex.quote(models_json)} > /root/.pi/agent/models.json && "
        f"printf %s {shlex.quote(extension_source)} > /tmp/blobfish-pi/blobfish-pi.ts && "
        f"printf %s {shlex.quote(system_prompt)} > /tmp/blobfish-pi/system.md && "
        "{ echo '=== SYSTEM ===' && uname -a && "
        "cat /etc/os-release 2>/dev/null | head -3; "
        "echo '=== TOOLS ===' && command -v node npm pi python3 bash; "
        "echo '=== PI MODEL CONFIG ===' && cat /root/.pi/agent/models.json; "
        "echo '=== /app ===' && ls /app; } > /tmp/orient.txt 2>&1"
    )


def _pi_models_json(
    *,
    provider: str,
    model_name: str,
    api_base: str,
    context_tokens: int,
    output_tokens: int,
) -> str:
    data = {
        "providers": {
            provider: {
                "name": "DS4",
                "baseUrl": api_base.rstrip("/"),
                "api": "anthropic-messages",
                "apiKey": "ANTHROPIC_API_KEY",
                "compat": {
                    "supportsEagerToolInputStreaming": False,
                    "supportsLongCacheRetention": False,
                },
                "models": [
                    {
                        "id": model_name,
                        "name": model_name,
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": context_tokens,
                        "maxTokens": output_tokens,
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    }
                ],
            }
        }
    }
    return json.dumps(data, indent=2)


def _pi_extension_source() -> str:
    return r'''
import { existsSync, readFileSync } from "node:fs";

function envInt(name: string, fallback: number): number {
  const value = Number.parseInt(process.env[name] ?? "", 10);
  return Number.isFinite(value) ? value : fallback;
}

function envFloat(name: string, fallback: number): number {
  const value = Number.parseFloat(process.env[name] ?? "");
  return Number.isFinite(value) ? value : fallback;
}

function toolPath(input: any): string {
  return String(input?.path ?? input?.file_path ?? input?.filePath ?? "");
}

function isAppPath(path: string): boolean {
  if (!path) return false;
  return path === "/app" || path.startsWith("/app/") || !path.startsWith("/");
}

function commandWritesApp(command: string): boolean {
  return /(>|>>|\btee\b|\bcp\b|\bmv\b|\btouch\b).{0,200}\/app\//s.test(command);
}

export default function (pi: any) {
  const startMs = Date.now();
  const maxThinking = envInt("BLOBFISH_PI_MAX_THINKING_TOKENS", 1024);
  const maxOutput = envInt("BLOBFISH_PI_OUTPUT_TOKENS", 8192);
  const temperature = envFloat("BLOBFISH_PI_TEMPERATURE", Number.NaN);
  const gateToolCalls = envInt("BLOBFISH_PI_HARD_GATE_TOOL_CALLS", 6);
  const gateElapsedPct = envFloat("BLOBFISH_PI_HARD_GATE_ELAPSED_PCT", 15);
  const taskTimeout = envInt("TASK_TIMEOUT_SECS", 0);
  const systemPromptFile = process.env.BLOBFISH_PI_SYSTEM_PROMPT_FILE ?? "";
  let toolCalls = 0;
  let outputWritten = false;

  pi.on("before_agent_start", (event) => {
    if (!systemPromptFile || !existsSync(systemPromptFile)) return;
    const extraPrompt = readFileSync(systemPromptFile, "utf8");
    return { systemPrompt: `${event.systemPrompt}\n\n${extraPrompt}` };
  });

  pi.on("before_provider_request", (event) => {
    const payload: any = { ...event.payload };
    if (maxOutput > 0) payload.max_tokens = maxOutput;
    if (maxThinking <= 0) {
      payload.thinking = { type: "disabled" };
      payload.max_thinking_tokens = 0;
    } else {
      payload.thinking = { type: "enabled", budget_tokens: maxThinking };
      payload.max_thinking_tokens = maxThinking;
    }
    if (Number.isFinite(temperature)) payload.temperature = temperature;
    return payload;
  });

  pi.on("tool_call", (event) => {
    toolCalls += 1;
    const name = event.toolName.toLowerCase();
    const path = toolPath(event.input);
    const command = String((event.input as any)?.command ?? "");

    if ((name === "write" || name === "edit") && isAppPath(path)) {
      outputWritten = true;
    }
    if (name === "bash" && commandWritesApp(command)) {
      outputWritten = true;
    }

    const elapsedMs = Date.now() - startMs;
    const elapsedPct = taskTimeout > 0 ? (elapsedMs / 1000 / taskTimeout) * 100 : 0;
    const gateOpen =
      !outputWritten &&
      (toolCalls >= gateToolCalls || (taskTimeout > 0 && elapsedPct >= gateElapsedPct));
    const exploratory = ["read", "grep", "find", "ls", "bash"].includes(name);
    const bashIsWriting = name === "bash" && commandWritesApp(command);

    if (gateOpen && exploratory && !bashIsWriting) {
      return {
        block: true,
        reason:
          "No deliverable has been written yet. Your next tool call must write or edit the best current candidate under /app; you can overwrite it later.",
      };
    }
  });
}
'''.lstrip()


def _int_value(value: int | str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _optional_int(value: int | str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_value(value: float | str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _optional_float(value: float | str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
