"""
OpenAI provider — BaseProvider implementation for OpenAI Chat Completions API.

Also works with OpenAI-compatible endpoints (OpenRouter, Together AI,
local Ollama, etc.) via OPENAI_BASE_URL env var.

Auth: OpenAI's native scheme is `Authorization: Bearer <key>`, so no
proxy quirks — the official SDK already sends what proxies expect.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion

from plugin_sdk.core import Message, ToolCall
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema


class OpenAIProvider(BaseProvider):
    name = "openai"
    default_model = "gpt-5.4"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """
        Args:
            api_key:  Defaults to $OPENAI_API_KEY.
            base_url: Defaults to $OPENAI_BASE_URL, else OpenAI's native endpoint.
        """
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError(
                "OpenAI API key not set. Export OPENAI_API_KEY or pass api_key."
            )
        kwargs: dict[str, Any] = {"api_key": key}
        base = base_url or os.environ.get("OPENAI_BASE_URL") or None
        if base:
            kwargs["base_url"] = base
        self.client = AsyncOpenAI(**kwargs)

    # ─── message conversion ─────────────────────────────────────────

    def _to_openai_messages(
        self, messages: list[Message], system: str = ""
    ) -> list[dict[str, Any]]:
        """Convert our canonical Message list to OpenAI's chat format."""
        out: list[dict[str, Any]] = []
        if system:
            out.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "system":
                continue  # passed separately via `system` param
            if m.role == "assistant" and m.tool_calls:
                out.append(
                    {
                        "role": "assistant",
                        "content": m.content or None,
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(tc.arguments),
                                },
                            }
                            for tc in m.tool_calls
                        ],
                    }
                )
            elif m.role == "tool":
                out.append(
                    {
                        "role": "tool",
                        "content": m.content,
                        "tool_call_id": m.tool_call_id,
                    }
                )
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    def _parse_response(self, resp: ChatCompletion) -> ProviderResponse:
        """Convert an OpenAI response to our canonical Message + metadata."""
        choice = resp.choices[0]
        raw_msg = choice.message

        tool_calls: list[ToolCall] = []
        if raw_msg.tool_calls:
            for tc in raw_msg.tool_calls:
                fn = tc.function
                try:
                    args = json.loads(fn.arguments) if fn.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=tc.id, name=fn.name, arguments=args))

        msg = Message(
            role="assistant",
            content=raw_msg.content or "",
            tool_calls=tool_calls if tool_calls else None,
        )
        usage = Usage(
            input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            output_tokens=resp.usage.completion_tokens if resp.usage else 0,
        )
        # OpenAI stop_reason names aren't identical to Anthropic's; normalize.
        finish = choice.finish_reason or "stop"
        stop_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "end_turn",
        }
        stop_reason = stop_map.get(finish, "end_turn")
        return ProviderResponse(message=msg, stop_reason=stop_reason, usage=usage)

    # ─── completion ────────────────────────────────────────────────

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
    ) -> ProviderResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = [t.to_openai_format() for t in tools]
        resp = await self.client.chat.completions.create(**kwargs)
        return self._parse_response(resp)

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> AsyncIterator[StreamEvent]:
        """Stream via OpenAI's chat.completions.create(stream=True)."""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = [t.to_openai_format() for t in tools]

        # Aggregate state while streaming — used to build the final ProviderResponse
        content_parts: list[str] = []
        tool_calls_accum: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: Usage = Usage()

        stream = await self.client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
                yield StreamEvent(kind="text_delta", text=delta.content)
            # Accumulate tool calls by index
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    slot = tool_calls_accum.setdefault(
                        idx, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if getattr(chunk, "usage", None):
                usage = Usage(
                    input_tokens=chunk.usage.prompt_tokens or 0,
                    output_tokens=chunk.usage.completion_tokens or 0,
                )

        # Reconstruct the final ProviderResponse
        tool_calls: list[ToolCall] = []
        for slot in tool_calls_accum.values():
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))

        msg = Message(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tool_calls if tool_calls else None,
        )
        stop_map = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "end_turn",
        }
        final = ProviderResponse(
            message=msg,
            stop_reason=stop_map.get(finish_reason, "end_turn"),
            usage=usage,
        )
        yield StreamEvent(kind="done", response=final)


__all__ = ["OpenAIProvider"]
