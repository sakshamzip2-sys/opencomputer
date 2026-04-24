"""
Provider contract — what plugin authors implement to add an LLM provider.

Providers wrap model APIs (Anthropic, OpenAI, OpenRouter, etc.) behind a
single interface the agent loop depends on. The agent never imports
anthropic/openai SDKs directly — it only uses BaseProvider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

from plugin_sdk.core import Message
from plugin_sdk.tool_contract import ToolSchema


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts from a single LLM call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """The result of calling `provider.complete(...)`.

    Reasoning fields (default ``None``) let reasoning-capable providers
    (OpenAI o1 / o3, Anthropic extended thinking, Nous, OpenRouter)
    surface the reasoning chain alongside the assistant message so the
    agent loop can persist it into SessionDB. Providers that don't
    expose reasoning (standard Opus/Sonnet completions, stock OpenAI
    chat completions) leave these ``None`` — no behaviour change.

    * ``reasoning``             — reasoning TEXT.
    * ``reasoning_details``     — structured OpenRouter / Nous array.
    * ``codex_reasoning_items`` — OpenAI o1/o3 reasoning items for
                                  verbatim replay.
    """

    message: Message  # the assistant message, possibly containing tool_calls
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens" | ...
    usage: Usage
    reasoning: str | None = None
    reasoning_details: Any = None  # list[dict[str, Any]] | None
    codex_reasoning_items: Any = None  # list[dict[str, Any]] | None


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One event emitted by `provider.stream_complete()`.

    Types:
      - "text_delta": incremental text chunk (`text` field)
      - "tool_call": full tool call has been assembled (`tool_call` field)
      - "done": streaming finished (`response` field carries the final ProviderResponse)
    """

    kind: Literal["text_delta", "tool_call", "done"]
    text: str = ""
    response: ProviderResponse | None = None


class BaseProvider(ABC):
    """Base class for an LLM provider plugin."""

    name: str = ""
    default_model: str = ""

    @abstractmethod
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
        """Send messages to the provider, return a single ProviderResponse."""
        ...

    @abstractmethod
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
        """Stream the response.

        Yields StreamEvent objects in order. Final event has kind="done"
        and carries the complete ProviderResponse (including aggregated text
        and any tool calls). Text chunks arrive as kind="text_delta".
        """
        ...


__all__ = ["BaseProvider", "ProviderResponse", "Usage", "StreamEvent"]
