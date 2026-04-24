"""
Anthropic provider — wraps the Anthropic SDK behind BaseProvider.

This is the first concrete provider. Later it can be moved to an
extension package (extensions/anthropic-provider/) for dogfooding the
plugin system, but for Phase 1 it lives in-tree so we can ship quickly.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from anthropic import AsyncAnthropic
from anthropic.types import Message as AnthropicMessage
from pydantic import BaseModel, Field

from plugin_sdk.core import Message, ToolCall
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema


class AnthropicProviderConfig(BaseModel):
    """Pydantic schema for AnthropicProvider construction kwargs.

    Wired onto ``AnthropicProvider.config_schema`` (Task I.6). The
    plugin registry uses this to validate ``provider.config`` at
    ``register_provider`` time and raise ``ValueError`` on shape
    mismatch — catching bad config at plugin load instead of at first
    request.

    Fields mirror the ``__init__`` signature: all three are optional
    because construction also reads from env vars
    (``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL``,
    ``ANTHROPIC_AUTH_MODE``).

    ``auth_mode`` accepts the legacy ``"x-api-key"`` spelling AND the
    newer ``"api_key"`` spelling. The construction logic coerces both
    to the same effective behavior (``"x-api-key"`` header mode).
    """

    api_key: str | None = Field(default=None)
    base_url: str | None = Field(default=None)
    auth_mode: Literal["api_key", "x-api-key", "bearer"] = Field(default="api_key")


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
    #: Task I.6 — schema used by the plugin registry to validate
    #: ``self.config`` at ``register_provider`` time.
    config_schema = AnthropicProviderConfig

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

        # Pre-validate mode with a clear RuntimeError before the pydantic
        # schema turns it into a less-helpful ValidationError. Keeps the
        # existing error message contract that callers rely on.
        if mode not in ("x-api-key", "api_key", "bearer"):
            raise RuntimeError(
                f"Unknown ANTHROPIC_AUTH_MODE: {mode!r} "
                f"(expected 'x-api-key', 'api_key', or 'bearer')"
            )

        # Task I.6: store a validated config snapshot so the plugin
        # registry can re-check it against ``config_schema`` at
        # ``register_provider`` time. The schema is permissive about
        # auth_mode spelling (accepts "x-api-key" and "api_key"), so
        # pass the effective value through.
        self.config = AnthropicProviderConfig(
            api_key=key,
            base_url=base,
            auth_mode=mode,  # type: ignore[arg-type]
        )

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
        # Otherwise mode is "x-api-key" or "api_key" → default SDK behavior
        # uses x-api-key; both spellings are equivalent here.
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
    ) -> AsyncIterator[StreamEvent]:
        """Stream response events via Anthropic's `messages.stream()` context.

        Yields text_delta events as tokens arrive, then a single "done" event
        with the final ProviderResponse (including tool calls if any).
        """
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

        async with self.client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if text:
                    yield StreamEvent(kind="text_delta", text=text)
            final = await stream.get_final_message()

        yield StreamEvent(kind="done", response=self._parse_response(final))


__all__ = ["AnthropicProvider", "AnthropicProviderConfig"]
