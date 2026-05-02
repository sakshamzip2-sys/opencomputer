"""Ollama provider — local LLM via Ollama's OpenAI-compatible API.

Default endpoint: http://localhost:11434/v1. Reads OLLAMA_BASE_URL override.

Differs from openai-provider with OPENAI_BASE_URL=http://localhost:11434/v1 by:
- Cleaner config UX (defaults right out of the box, no env fiddling)
- Logical home for Ollama-specific extensions later (modelfile mgmt, etc.)
"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import httpx

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema

DEFAULT_BASE_URL = "http://localhost:11434/v1"

# OpenAI finish_reason → OC stop_reason vocabulary
_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    None: "end_turn",
}


class OllamaProvider(BaseProvider):
    name = "ollama"
    default_model = "llama3"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        # Ollama doesn't require auth by default but accepts arbitrary tokens.
        self._api_key = (api_key or "ollama").strip()
        self._base_url = (
            (base_url or os.environ.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
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
        finish_reason: str | None = None
        usage = Usage()
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            async with client.stream(
                "POST",
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as r:
                r.raise_for_status()
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
                    if fr := choices[0].get("finish_reason"):
                        finish_reason = fr
                    if u := chunk.get("usage"):
                        usage = Usage(
                            input_tokens=u.get("prompt_tokens", 0),
                            output_tokens=u.get("completion_tokens", 0),
                        )
        # Final event — done, carrying the full ProviderResponse
        final_msg = Message(role="assistant", content="".join(content_parts))
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
        choice = data["choices"][0]
        msg_data = choice["message"]
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


__all__ = ["OllamaProvider"]
