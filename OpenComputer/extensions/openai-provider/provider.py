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

from opencomputer.agent.credential_pool import CredentialPool
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

    _api_key_env: str = "OPENAI_API_KEY"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """
        Args:
            api_key:  Defaults to $OPENAI_API_KEY. Comma-separated value triggers pool mode (PR-A).
            base_url: Defaults to $OPENAI_BASE_URL, else OpenAI's native endpoint.
        """
        # Optional credential pool (PR-A): comma-separated env value triggers pool mode.
        # Single key (no comma) → no pool, behavior IDENTICAL to today (regression-tested).
        api_key_raw = api_key or os.environ.get(self._api_key_env, "")
        if "," in api_key_raw:
            keys = [k.strip() for k in api_key_raw.split(",") if k.strip()]
            self._credential_pool: CredentialPool | None = CredentialPool(keys=keys) if len(keys) > 1 else None
            self._api_key = keys[0] if keys else api_key_raw
        else:
            self._credential_pool = None
            self._api_key = api_key_raw.strip()

        key = self._api_key
        if not key:
            raise RuntimeError(
                "OpenAI API key not set. Export OPENAI_API_KEY or pass api_key."
            )
        base = base_url or os.environ.get("OPENAI_BASE_URL") or None
        self._base = base
        kwargs: dict[str, Any] = {"api_key": key}
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

    def _build_client_for_key(self, key: str) -> AsyncOpenAI:
        """Build an AsyncOpenAI client for the given key (used in pool rotation)."""
        kwargs: dict[str, Any] = {"api_key": key}
        if self._base:
            kwargs["base_url"] = self._base
        return AsyncOpenAI(**kwargs)

    async def _do_complete(
        self,
        key: str,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> ProviderResponse:
        """Low-level complete using the given API key (pool-rotation target)."""
        client = self._build_client_for_key(key) if key != self._api_key else self.client
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = [t.to_openai_format() for t in tools]
        resp = await client.chat.completions.create(**kwargs)
        return self._parse_response(resp)

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
        if self._credential_pool is None:
            return await self._do_complete(
                self._api_key,
                model=model,
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        def _is_auth_failure(exc: Exception) -> bool:
            return "401" in str(exc) or "authentication" in str(exc).lower()

        return await self._credential_pool.with_retry(
            lambda key: self._do_complete(
                key,
                model=model,
                messages=messages,
                system=system,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
            ),
            is_auth_failure=_is_auth_failure,
        )

    async def _do_stream_complete(
        self,
        key: str,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> ProviderResponse:
        """Low-level streaming that aggregates into a ProviderResponse (pool target)."""
        client = self._build_client_for_key(key) if key != self._api_key else self.client
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages, system),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = [t.to_openai_format() for t in tools]

        content_parts: list[str] = []
        tool_calls_accum: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: Usage = Usage()

        stream = await client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue
            if delta.content:
                content_parts.append(delta.content)
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
        return ProviderResponse(
            message=msg,
            stop_reason=stop_map.get(finish_reason, "end_turn"),
            usage=usage,
        )

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
        if self._credential_pool is not None:
            # Pool path: on auth failure, rotate and re-try non-streaming.
            def _is_auth_failure(exc: Exception) -> bool:
                return "401" in str(exc) or "authentication" in str(exc).lower()

            response = await self._credential_pool.with_retry(
                lambda key: self._do_stream_complete(
                    key,
                    model=model,
                    messages=messages,
                    system=system,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                ),
                is_auth_failure=_is_auth_failure,
            )
            if response.message.content:
                yield StreamEvent(kind="text_delta", text=response.message.content)
            yield StreamEvent(kind="done", response=response)
            return

        # No pool — native streaming path (unchanged behavior).
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
