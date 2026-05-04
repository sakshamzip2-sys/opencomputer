"""Cerebras Inference provider — OpenAI-compatible HTTP API.

Targets https://api.cerebras.ai/v1. Auth: Bearer $CEREBRAS_API_KEY.
"""

from __future__ import annotations

import json as _json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema

CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
DEFAULT_MODELS: tuple[str, ...] = (
    "llama-3.3-70b",
    "llama3.1-8b",
    "qwen-3-32b",
    "gpt-oss-120b",
)
DEFAULT_TIMEOUT_S = 60.0


class CerebrasProvider(BaseProvider):
    """OpenAI-compatible client targeting Cerebras Inference."""

    name = "cerebras"
    default_model = DEFAULT_MODELS[0]

    def __init__(self, base_url: str | None = None, **_: Any) -> None:
        self.base_url = base_url or CEREBRAS_BASE_URL

    def _api_key(self) -> str:
        key = os.environ.get("CEREBRAS_API_KEY")
        if not key:
            raise RuntimeError(
                "CEREBRAS_API_KEY environment variable is required for the Cerebras provider"
            )
        return key

    def _msg_to_dict(self, m: Message) -> dict:
        # Cerebras follows OpenAI's chat shape.
        return {
            "role": m.role,
            "content": m.content if isinstance(m.content, str) else "",
        }

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,  # noqa: ARG002 — tools not yet wired
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,  # noqa: ARG002
        runtime_extras: dict | None = None,  # noqa: ARG002
        response_schema: Any | None = None,  # noqa: ARG002
        site: str = "agent_loop",  # noqa: ARG002
    ) -> ProviderResponse:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(self._msg_to_dict(m) for m in messages)
        body = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key()}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        usage_in = data.get("usage", {})
        # Map OpenAI-style finish_reason to OC's stop_reason vocabulary.
        finish = choice.get("finish_reason", "stop")
        stop_reason = "max_tokens" if finish == "length" else "end_turn"
        return ProviderResponse(
            message=Message(role=msg["role"], content=msg.get("content", "") or ""),
            stop_reason=stop_reason,
            usage=Usage(
                input_tokens=usage_in.get("prompt_tokens", 0),
                output_tokens=usage_in.get("completion_tokens", 0),
            ),
        )

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,  # noqa: ARG002
        max_tokens: int = 4096,
        temperature: float = 1.0,
        runtime_extras: dict | None = None,  # noqa: ARG002
        response_schema: Any | None = None,  # noqa: ARG002
        site: str = "agent_loop",  # noqa: ARG002
    ) -> AsyncIterator[StreamEvent]:
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(self._msg_to_dict(m) for m in messages)
        body = {
            "model": model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        text_chunks: list[str] = []
        usage_in: dict = {}
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key()}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = _json.loads(payload)
                    except _json.JSONDecodeError:
                        continue
                    if "usage" in chunk and chunk["usage"]:
                        usage_in = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {}) or {}
                    if delta.get("content"):
                        text_chunks.append(delta["content"])
                        yield StreamEvent(kind="text_delta", text=delta["content"])
        final_text = "".join(text_chunks)
        final_response = ProviderResponse(
            message=Message(role="assistant", content=final_text),
            stop_reason="end_turn",
            usage=Usage(
                input_tokens=usage_in.get("prompt_tokens", 0),
                output_tokens=usage_in.get("completion_tokens", 0),
            ),
        )
        yield StreamEvent(kind="done", response=final_response)
