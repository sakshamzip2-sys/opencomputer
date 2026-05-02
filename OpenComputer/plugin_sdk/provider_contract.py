"""
Provider contract — what plugin authors implement to add an LLM provider.

Providers wrap model APIs (Anthropic, OpenAI, OpenRouter, etc.) behind a
single interface the agent loop depends on. The agent never imports
anthropic/openai SDKs directly — it only uses BaseProvider.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
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
class CacheTokens:
    """Provider-agnostic cache token counts extracted from a usage payload."""

    read: int = 0
    write: int = 0


def _default_extract_cache_tokens(usage: Any) -> CacheTokens:  # noqa: ARG001
    """Conservative default — providers without cache visibility return zeros."""
    return CacheTokens()


def _default_min_cache_tokens(model: str) -> int:  # noqa: ARG001
    """No filtering by default."""
    return 0


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """What a provider supports for the agent loop's context-economy decisions.

    All fields default to conservative "off" values so a provider that does
    nothing inherits today's behaviour.

    * ``requires_reasoning_resend_in_tool_cycle`` — set True if the provider
      requires the assistant message that originally produced a tool_use to
      include the corresponding reasoning block (with signature) when the
      tool_result is sent back. Anthropic extended thinking requires this;
      OpenAI Chat Completions does not.
    * ``reasoning_block_kind`` — opaque tag the provider uses to distinguish
      its reasoning replay shape (e.g. ``"anthropic_thinking"``).
    * ``extracts_cache_tokens`` — callable that maps the provider's usage
      payload to ``CacheTokens``. Default returns zeros.
    * ``min_cache_tokens`` — minimum block size (in tokens) for which a
      cache_control marker is worth placing. Provider-aware; receives the
      model name. Default returns 0 (no filter).
    * ``supports_long_ttl`` — True if the provider exposes a 1-hour cache
      TTL knob (Anthropic only today).
    """

    requires_reasoning_resend_in_tool_cycle: bool = False
    reasoning_block_kind: Literal["anthropic_thinking", "openai_reasoning", None] = None
    extracts_cache_tokens: Callable[[Any], CacheTokens] = field(
        default=_default_extract_cache_tokens
    )
    min_cache_tokens: Callable[[str], int] = field(default=_default_min_cache_tokens)
    supports_long_ttl: bool = False


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
    reasoning_replay_blocks: Any = None  # list[dict[str, Any]] | None
    """Verbatim provider-side reasoning blocks that must be replayed on
    the next turn (Anthropic thinking blocks with signatures). The
    agent loop should propagate this onto the canonical Message it
    persists, so the next turn's _to_<provider>_messages can replay it
    on the wire. Other providers leave this ``None``.
    """


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
    "CacheTokens",
    "ProviderCapabilities",
    "ProviderResponse",
    "RateLimitedError",
    "StreamEvent",
    "Usage",
]
