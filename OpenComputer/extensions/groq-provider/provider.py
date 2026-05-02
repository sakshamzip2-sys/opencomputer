"""Groq provider — ultra-fast inference via GroqCloud's OpenAI-compatible API.

Default endpoint: https://api.groq.com/openai/v1.
Requires GROQ_API_KEY env var (or explicit api_key constructor arg).

GroqCloud delivers 276-1500 t/s depending on model, making it suitable for
latency-sensitive use cases like realtime assistants and coding agents.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from plugin_sdk.core import Message, ToolCall
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
SIGNUP_URL = "https://console.groq.com/keys"

# OpenAI finish_reason → OC stop_reason vocabulary
_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    None: "end_turn",
}


class GroqProvider(BaseProvider):
    name = "groq"
    default_model = "llama-3.3-70b-versatile"
    _api_key_env: str = "GROQ_API_KEY"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        resolved_key = (api_key or os.environ.get(self._api_key_env) or "").strip()
        if not resolved_key:
            raise RuntimeError(
                f"Groq API key not set. Export {self._api_key_env} or pass api_key. "
                f"Sign up for a free key at {SIGNUP_URL}."
            )
        self._api_key = resolved_key
        self._base_url = (
            (base_url or os.environ.get("GROQ_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        )

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
        runtime_extras: dict | None = None,
    ) -> ProviderResponse:
        body = self._build_body(
            model=model, messages=messages, system=system, tools=tools,
            max_tokens=max_tokens, temperature=temperature, stream=False,
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            r = await client.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            r.raise_for_status()
            data = r.json()
        return self._parse_response(data)

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,
    ) -> AsyncIterator[StreamEvent]:
        body = self._build_body(
            model=model, messages=messages, system=system, tools=tools,
            max_tokens=max_tokens, temperature=temperature, stream=True,
        )
        content_parts: list[str] = []
        # Tool calls stream as partial deltas across multiple chunks.
        # Accumulate by index, materialise as ToolCall objects in the
        # final `done` event. Same pattern as openai-provider.
        tool_calls_accum: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage = Usage()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                async with client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ) as r:
                    try:
                        r.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        logger.warning(
                            "groq stream HTTP %s: %s", e.response.status_code, e
                        )
                        raise
                    async for line in r.aiter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        if text := delta.get("content"):
                            content_parts.append(text)
                            yield StreamEvent(kind="text_delta", text=text)
                        for tc in delta.get("tool_calls") or []:
                            slot = tool_calls_accum.setdefault(
                                tc.get("index", 0),
                                {"id": "", "name": "", "arguments": ""},
                            )
                            if tc_id := tc.get("id"):
                                slot["id"] = tc_id
                            fn = tc.get("function") or {}
                            if fn_name := fn.get("name"):
                                slot["name"] = fn_name
                            if fn_args := fn.get("arguments"):
                                slot["arguments"] += fn_args
                        if fr := choices[0].get("finish_reason"):
                            finish_reason = fr
                        if u := chunk.get("usage"):
                            usage = Usage(
                                input_tokens=u.get("prompt_tokens", 0),
                                output_tokens=u.get("completion_tokens", 0),
                            )
        finally:
            # The agent loop awaits a `done` sentinel — emit it even on
            # exception so the consumer doesn't hang. Partial content is
            # surfaced; the original exception still propagates after.
            tool_calls = tuple(
                ToolCall(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=_safe_json(slot["arguments"]),
                )
                for slot in tool_calls_accum.values()
                if slot["id"] or slot["name"]
            )
            final_msg = Message(
                role="assistant",
                content="".join(content_parts),
                tool_calls=tool_calls or None,
            )
            yield StreamEvent(
                kind="done",
                response=ProviderResponse(
                    message=final_msg,
                    stop_reason=_STOP_MAP.get(finish_reason, "end_turn"),
                    usage=usage,
                ),
            )

    def _build_body(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str,
        tools: list[ToolSchema] | None,
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(self._msg(m) for m in messages)
        body: dict = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if tools:
            body["tools"] = [t.to_openai_format() for t in tools]
        return body

    def _parse_response(self, data: dict) -> ProviderResponse:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"groq returned empty choices: {data!r}")
        choice = choices[0]
        msg_data = choice.get("message") or {}
        finish = choice.get("finish_reason")
        u = data.get("usage") or {}
        return ProviderResponse(
            message=Message(
                role="assistant",
                content=msg_data.get("content") or "",
                tool_calls=msg_data.get("tool_calls") or None,
            ),
            stop_reason=_STOP_MAP.get(finish, "end_turn"),
            usage=Usage(
                input_tokens=u.get("prompt_tokens", 0),
                output_tokens=u.get("completion_tokens", 0),
            ),
        )

    @staticmethod
    def _msg(m: Message) -> dict:
        d = {"role": m.role, "content": m.content or ""}
        if getattr(m, "tool_calls", None):
            d["tool_calls"] = m.tool_calls
        if getattr(m, "tool_call_id", None):
            d["tool_call_id"] = m.tool_call_id
        return d


def _safe_json(s: str) -> dict[str, Any]:
    """Tolerate truncated tool-call argument JSON during streaming aborts."""
    if not s:
        return {}
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return {}


__all__ = ["GroqProvider"]
