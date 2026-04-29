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
from typing import TYPE_CHECKING, Any, Literal

from plugin_sdk.core import Message
from plugin_sdk.tool_contract import ToolSchema

if TYPE_CHECKING:
    from pydantic import BaseModel


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


class RateLimitedError(RuntimeError):  # noqa: N818 — public name is the load-bearing one
    """TS-T7 — provider is currently rate-limited (cross-session signal).

    Raised by a provider's ``complete`` / ``stream_complete`` when the
    cross-session rate-limit guard (``opencomputer.agent.rate_guard``)
    indicates a previous 429 hasn't reset yet. Carries the provider name
    plus a human-readable message so the caller can decide between
    waiting, falling back to another provider/model, or surfacing the
    error verbatim.

    Subclasses ``RuntimeError`` (not a custom hierarchy) so the existing
    transient-error string-matching in ``opencomputer.agent.fallback``
    can pick this up via the ``"rate limit"`` marker without needing an
    isinstance check; that keeps the fallback layer provider-agnostic.
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One event emitted by `provider.stream_complete()`.

    Types:
      - "text_delta":     incremental answer text chunk (`text` field)
      - "thinking_delta": incremental reasoning text chunk (`text` field) —
                          providers that surface reasoning (Anthropic
                          extended thinking, OpenAI o-series reasoning)
                          emit these alongside text_delta. Renderers that
                          don't care about thinking can ignore this kind.
      - "tool_call":      full tool call has been assembled (`tool_call` field)
      - "done":           streaming finished (`response` field carries the final
                          ProviderResponse)
    """

    kind: Literal["text_delta", "thinking_delta", "tool_call", "done"]
    text: str = ""
    response: ProviderResponse | None = None


class BaseProvider(ABC):
    """Base class for an LLM provider plugin.

    Providers may optionally declare a ``config_schema`` class attribute
    (a ``pydantic.BaseModel`` subclass) describing the shape of their
    construction kwargs / ``self.config`` object. When set, the plugin
    registry validates the provider's config against this schema at
    ``register_provider`` time, raising ``ValueError`` early instead of
    waiting for a confusing first-use failure.

    Providers that don't set ``config_schema`` (the default ``None``)
    skip registry-side validation — backwards compatible with every
    pre-I.6 provider.

    Matches the OpenClaw pattern in
    ``sources/openclaw/src/plugins/provider-validation.ts``
    (``normalizeRegisteredProvider``): validate shape at registration,
    not at first request.
    """

    name: str = ""
    default_model: str = ""
    #: Optional pydantic schema describing this provider's config shape.
    #: When non-None, the registry validates ``self.config`` against it
    #: at ``register_provider`` time.
    config_schema: type[BaseModel] | None = None

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
        runtime_extras: dict | None = None,
    ) -> ProviderResponse:
        """Send messages to the provider, return a single ProviderResponse.

        ``runtime_extras`` carries provider-agnostic runtime flags
        (currently ``reasoning_effort`` and ``service_tier``) that the
        agent loop reads from ``runtime.custom`` (set by ``/reasoning``
        and ``/fast`` slash commands). Concrete providers should merge
        these into their API request via the helpers in
        ``opencomputer.agent.runtime_flags``. ``None`` (the default)
        means no flags active — providers must treat this identically to
        an empty dict.
        """
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
        runtime_extras: dict | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the response.

        Yields StreamEvent objects in order. Final event has kind="done"
        and carries the complete ProviderResponse (including aggregated text
        and any tool calls). Text chunks arrive as kind="text_delta".
        """
        ...


__all__ = [
    "BaseProvider",
    "ProviderResponse",
    "RateLimitedError",
    "StreamEvent",
    "Usage",
]
