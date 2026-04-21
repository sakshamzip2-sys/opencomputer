"""
Anthropic provider — wraps the Anthropic SDK behind BaseProvider.

This is the first concrete provider. Later it can be moved to an
extension package (extensions/anthropic-provider/) for dogfooding the
plugin system, but for Phase 1 it lives in-tree so we can ship quickly.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from anthropic.types import Message as AnthropicMessage

from plugin_sdk.core import Message, ToolCall
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
from plugin_sdk.tool_contract import ToolSchema


async def _strip_x_api_key(request: httpx.Request) -> None:
    """httpx event hook: remove x-api-key header before sending.

    Used when talking to proxies that authenticate via Bearer tokens and
    forward x-api-key unchanged to the upstream Anthropic API (which then
    rejects it as invalid). Must run at the last moment so the Anthropic
    SDK's own auth path is undisturbed.
    """
    request.headers.pop("x-api-key", None)


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    default_model = "claude-opus-4-7"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        auth_mode: str | None = None,
    ) -> None:
        """
        Args:
            api_key: API key / proxy key. Defaults to $ANTHROPIC_API_KEY.
            base_url: Override the API endpoint (for proxies like Claude Router).
                      Defaults to $ANTHROPIC_BASE_URL, or None (direct Anthropic).
            auth_mode: "x-api-key" (Anthropic native) or "bearer"
                      (Authorization: Bearer header — for proxies that require it).
                      Defaults to $ANTHROPIC_AUTH_MODE, or "x-api-key".
        """
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError(
                "Anthropic API key not set. Export ANTHROPIC_API_KEY or pass api_key."
            )
        base = base_url or os.environ.get("ANTHROPIC_BASE_URL") or None
        mode = (auth_mode or os.environ.get("ANTHROPIC_AUTH_MODE") or "x-api-key").lower()

        kwargs: dict[str, Any] = {"api_key": key}
        if base:
            kwargs["base_url"] = base
        if mode == "bearer":
            # For proxies like Claude Router: add Authorization: Bearer AND
            # strip x-api-key on the way out (the SDK adds it automatically
            # from api_key, and some proxies forward it to upstream Anthropic
            # which then rejects the proxy key as "invalid x-api-key").
            kwargs["default_headers"] = {"Authorization": f"Bearer {key}"}
            kwargs["http_client"] = httpx.AsyncClient(
                event_hooks={"request": [_strip_x_api_key]},
                timeout=httpx.Timeout(60.0, connect=10.0),
            )
        elif mode != "x-api-key":
            raise RuntimeError(
                f"Unknown ANTHROPIC_AUTH_MODE: {mode!r} (expected 'x-api-key' or 'bearer')"
            )
        self.client = AsyncAnthropic(**kwargs)

    # ─── message conversion ─────────────────────────────────────────

    def _to_anthropic_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert our canonical Message list to Anthropic's message format."""
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                # system messages are passed separately, not in messages[]
                continue
            if m.role == "assistant" and m.tool_calls:
                content: list[dict[str, Any]] = []
                if m.content:
                    content.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                out.append({"role": "assistant", "content": content})
            elif m.role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id,
                                "content": m.content,
                            }
                        ],
                    }
                )
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    def _parse_response(self, resp: AnthropicMessage) -> ProviderResponse:
        """Convert an Anthropic response back to our canonical Message + metadata."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input) if block.input else {},
                    )
                )
        msg = Message(
            role="assistant",
            content="\n".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
        )
        usage = Usage(
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
        return ProviderResponse(
            message=msg,
            stop_reason=resp.stop_reason or "end_turn",
            usage=usage,
        )

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
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": self._to_anthropic_messages(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [t.to_anthropic_format() for t in tools]
        resp = await self.client.messages.create(**kwargs)
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
    ) -> AsyncIterator[str]:
        # Phase 1 uses non-streaming for simplicity; streaming in Phase 1.5
        resp = await self.complete(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
        )
        yield resp.message.content


__all__ = ["AnthropicProvider"]
