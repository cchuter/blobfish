"""Terminus 2 adapter that calls a DS4 Anthropic-compatible endpoint directly."""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from harbor.agents.terminus_2 import Terminus2
from harbor.llms.base import (
    BaseLLM,
    ContextLengthExceededError,
    LLMResponse,
    OutputLengthExceededError,
)
from harbor.models.metric import UsageInfo


class DirectAnthropicLLM(BaseLLM):
    """Minimal Anthropic Messages client for DS4.

    This avoids LiteLLM at request time while preserving the small BaseLLM
    interface Terminus 2 expects.
    """

    def __init__(
        self,
        model_name: str,
        api_base: str = "http://127.0.0.1:8081",
        api_key: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 48000,
        max_thinking_tokens: int | None = None,
        reasoning_effort: str | None = None,
        context_limit: int = 196608,
        output_limit: int = 48000,
        timeout_sec: float = 3000.0,
    ):
        super().__init__()
        self._model_name = model_name
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or "no-key"
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._max_thinking_tokens = max_thinking_tokens
        self._reasoning_effort = reasoning_effort
        self._context_limit = context_limit
        self._output_limit = output_limit
        self._timeout_sec = timeout_sec

    async def call(
        self,
        prompt: str,
        message_history: list[dict[str, Any]] | None = None,
        logging_path: Path | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        response_format = kwargs.pop("response_format", None)
        kwargs.pop("previous_response_id", None)
        if response_format is not None:
            prompt = (
                "Respond in JSON matching this schema:\n"
                f"{json.dumps(response_format, default=str)}\n\n{prompt}"
            )

        messages = list(message_history or [])
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self._model_name,
            "messages": messages,
            "max_tokens": self._max_tokens,
            "stream": False,
            "temperature": self._temperature,
        }
        if self._max_thinking_tokens is not None:
            payload["thinking"] = {
                "type": "disabled" if self._max_thinking_tokens <= 0 else "enabled",
                "budget_tokens": max(0, self._max_thinking_tokens),
            }
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort

        data = await asyncio.to_thread(self._post_messages, payload, logging_path)
        content, reasoning = _extract_anthropic_content(data.get("content"))
        usage = _extract_usage(data.get("usage"))

        if data.get("stop_reason") == "max_tokens":
            raise OutputLengthExceededError(
                f"Model {self._model_name} hit max_tokens limit.",
                truncated_response=content,
            )

        return LLMResponse(
            content=content,
            reasoning_content=reasoning,
            usage=usage,
            response_id=data.get("id"),
        )

    def get_model_context_limit(self) -> int:
        return self._context_limit

    def get_model_output_limit(self) -> int | None:
        return self._output_limit

    def _post_messages(
        self, payload: dict[str, Any], logging_path: Path | None
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self._api_base}/v1/messages",
            data=body,
            headers={
                "content-type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            lowered = raw.lower()
            if "context" in lowered and "length" in lowered:
                raise ContextLengthExceededError(raw) from exc
            raise RuntimeError(f"DS4 request failed with HTTP {exc.code}: {raw}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"DS4 request failed: {exc}") from exc

        if logging_path is not None:
            logging_path.write_text(raw)
        return json.loads(raw)


class Terminus2DS4Agent(Terminus2):
    """Harbor Terminus 2 agent using DirectAnthropicLLM for DS4."""

    @staticmethod
    def name() -> str:
        return "terminus-2-ds4"

    def _init_llm(
        self,
        llm_backend: Any,
        model_name: str,
        temperature: float,
        collect_rollout_details: bool,
        llm_kwargs: dict | None,
        api_base: str | None,
        session_id: str | None,
        max_thinking_tokens: int | None,
        reasoning_effort: str | None,
        model_info: dict | None,
        use_responses_api: bool,
    ) -> BaseLLM:
        del llm_backend, collect_rollout_details, session_id, use_responses_api
        llm_kwargs = dict(llm_kwargs or {})
        timeout_sec = float(llm_kwargs.pop("timeout_sec", 3000.0))
        api_key = llm_kwargs.pop("api_key", None)
        output_limit = int((model_info or {}).get("max_output_tokens", 48000))
        context_limit = int((model_info or {}).get("max_input_tokens", 196608))
        return DirectAnthropicLLM(
            model_name=model_name,
            api_base=api_base or "http://127.0.0.1:8081",
            api_key=api_key,
            temperature=temperature,
            max_tokens=output_limit,
            max_thinking_tokens=max_thinking_tokens,
            reasoning_effort=reasoning_effort,
            context_limit=context_limit,
            output_limit=output_limit,
            timeout_sec=timeout_sec,
        )


def _extract_anthropic_content(content: Any) -> tuple[str, str | None]:
    if isinstance(content, str):
        return content, None
    if not isinstance(content, list):
        return "", None

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(str(block.get("text") or ""))
        elif block_type == "thinking":
            reasoning_parts.append(str(block.get("thinking") or ""))
    reasoning = "\n".join(part for part in reasoning_parts if part) or None
    return "".join(text_parts), reasoning


def _extract_usage(usage: Any) -> UsageInfo | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("input_tokens") or 0)
    cache_read_tokens = int(usage.get("cache_read_input_tokens") or 0)
    cache_write_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    return UsageInfo(
        prompt_tokens=input_tokens + cache_read_tokens + cache_write_tokens,
        completion_tokens=int(usage.get("output_tokens") or 0),
        cache_tokens=cache_read_tokens,
        cost_usd=0.0,
    )
