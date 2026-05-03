"""Codex (OpenAI Responses API) provider plugin for OpenComputer."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

try:
    from codex_responses_adapter import (  # plugin-loader mode
        messages_to_responses_input,
        responses_output_to_provider,
        tools_to_responses_tools,
    )
except ImportError:
    from extensions.codex_provider.codex_responses_adapter import (  # package mode
        messages_to_responses_input,
        responses_output_to_provider,
        tools_to_responses_tools,
    )

from plugin_sdk import BaseProvider, Message, ProviderResponse, StreamEvent, ToolSchema, Usage

_RESPONSES_URL = "https://api.openai.com/v1/responses"


class CodexProvider(BaseProvider):
    """OpenAI Responses API provider (Codex / xAI / GitHub Models)."""

    def __init__(self) -> None:
        self._api_key = os.environ.get("OPENAI_API_KEY", "")
        self._base_url = os.environ.get("CODEX_BASE_URL", _RESPONSES_URL)

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
        response_schema=None,
        site: str = "agent_loop",
    ) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": model,
            "input": messages_to_responses_input(messages),
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system:
            payload["instructions"] = system
        if tools:
            payload["tools"] = tools_to_responses_tools(tools)
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
            r.raise_for_status()
            return responses_output_to_provider(r.json())

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
        response_schema=None,
        site: str = "agent_loop",
    ) -> AsyncIterator[StreamEvent]:
        payload: dict[str, Any] = {
            "model": model,
            "input": messages_to_responses_input(messages),
            "max_output_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if system:
            payload["instructions"] = system
        if tools:
            payload["tools"] = tools_to_responses_tools(tools)
        text_buf = ""
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream(
                "POST",
                self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    for item in chunk.get("delta", {}).get("output", []):
                        for part in item.get("content", []):
                            if part.get("type") in ("output_text", "text"):
                                delta = part.get("text", "")
                                text_buf += delta
                                yield StreamEvent(kind="text_delta", text=delta)
        msg = Message(role="assistant", content=text_buf or None)
        resp_obj = ProviderResponse(message=msg, usage=Usage(), stop_reason="end_turn")
        yield StreamEvent(kind="done", response=resp_obj)


def register(api) -> None:
    api.register_provider("codex", CodexProvider)
