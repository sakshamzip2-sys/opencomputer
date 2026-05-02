"""
Provider contract — what plugin authors implement to add an LLM provider.

Providers wrap model APIs (Anthropic, OpenAI, OpenRouter, etc.) behind a
single interface the agent loop depends on. The agent never imports
anthropic/openai SDKs directly — it only uses BaseProvider.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from plugin_sdk.core import Message
from plugin_sdk.tool_contract import ToolSchema


def _heuristic_token_count(
    messages: list[Message],
    system: str = "",
    tools: list[ToolSchema] | None = None,
) -> int:
    """Provider-agnostic input-token count.

    Tries ``tiktoken`` (cl100k_base — what GPT-4/Claude/Llama tokenizers
    closely approximate) when available. Falls back to a ~4-chars-per-
    token heuristic when tiktoken isn't installed.

    Used by :meth:`BaseProvider.count_tokens` when the concrete provider
    doesn't override. Providers with native endpoints (Anthropic
    ``messages.count_tokens``) or local tokenizers (Llama-cpp, Ollama
    ``/api/tokenize``) should still override for highest accuracy —
    this fallback is just a smarter default than character counting.
    """
    # Try tiktoken's cl100k_base first — close approximation for most
    # modern tokenizers (GPT-4, Claude, Llama, Mistral all within ~10%).
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        total = len(enc.encode(system or ""))
        for m in messages:
            if m.content:
                total += len(enc.encode(m.content))
            for tc in (m.tool_calls or []):
                total += len(
                    enc.encode(tc.name + json.dumps(tc.arguments or {}))
                )
        if tools:
            for t in tools:
                total += len(enc.encode(json.dumps(t.to_openai_format())))
        return max(1, total)
    except (ImportError, KeyError, Exception):  # noqa: BLE001
        # Fall through to char-based heuristic when tiktoken unavailable
        # or its encoder fails (rare — defensive).
        pass

    # Char-based fallback — ~4 chars per token. Conservative.
    total = len(system or "")
    for m in messages:
        total += len(m.content or "")
        for tc in (m.tool_calls or []):
            total += len(tc.name) + len(json.dumps(tc.arguments or {}))
    if tools:
        for t in tools:
            total += len(json.dumps(t.to_openai_format()))
    return max(1, total // 4)

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


class BatchUnsupportedError(NotImplementedError):
    """Raised when a provider doesn't support batch processing.

    Subsystem E (2026-05-02). Most providers don't natively support
    batch APIs — Anthropic does (50% cost discount, ~1hr turnaround),
    OpenAI does with a different async-file-based shape, others don't
    at all. Callers should catch this and fall back to serial calls
    if they want graceful degradation.
    """


class VisionUnsupportedError(NotImplementedError):
    """Raised when a provider doesn't support vision (multimodal image input).

    Mirrors :class:`BatchUnsupportedError` for the vision capability.
    Most providers don't accept image content blocks (Ollama default
    text models, Groq's text-only Llama variants, the OpenAI-compat
    shims that pass-through plain chat). Anthropic, OpenAI (via
    gpt-4o / gpt-5.4), and OpenRouter (when routed to a vision model)
    do. Callers should catch this and surface a clean "vision not
    supported on <provider>" message rather than crashing with a
    cryptic HTTP 400 from the LLM API.
    """


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """One entry in a batch job — the input to ``submit_batch``.

    Provider-agnostic shape — providers translate to their native batch
    request format. Composes with Subsystems B (``runtime_extras``) and
    C (``response_schema``): each batched request can carry its own
    effort tier and schema independently.
    """

    custom_id: str
    """Caller-supplied id for matching results to requests. Anthropic
    requires alphanumeric + ``_-``, 1-64 chars."""
    messages: list[Message]
    model: str
    system: str = ""
    max_tokens: int = 1024
    runtime_extras: dict | None = None
    response_schema: dict | None = None


@dataclass(frozen=True, slots=True)
class BatchResult:
    """One entry in batch results — output from ``get_batch_results``."""

    custom_id: str
    status: Literal["succeeded", "errored", "expired", "canceled", "processing"]
    response: ProviderResponse | None = None
    """Populated when ``status == "succeeded"``."""
    error: str = ""
    """Populated when ``status == "errored"``."""


class JsonSchemaSpec(TypedDict, total=False):
    """Provider-agnostic structured-outputs schema spec.

    Lives in plugin_sdk because every BaseProvider sees this kwarg.
    Providers translate the spec to their native shape:

    * Anthropic: ``output_config.format = {"type": "json_schema", "schema": <schema>}``
    * OpenAI: ``response_format = {"type": "json_schema", "json_schema":
      {"name": <name>, "schema": <schema>, "strict": True}}``
    * Providers without native support: pass through as no-op (the
      caller is expected to add JSON instructions to the prompt as
      a backup).

    Fields:
      * ``schema`` — the JSON Schema (subset Anthropic + OpenAI accept).
        Must include ``type: "object"`` at the top level for both.
      * ``name`` — short identifier used by OpenAI's ``json_schema.name``
        field. Anthropic ignores this. Default ``"response"``.
      * ``description`` — optional one-liner. Some providers surface it
        in the schema metadata.
    """

    schema: dict
    name: str
    description: str


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
        response_schema: JsonSchemaSpec | None = None,
        site: str = "agent_loop",
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

        ``response_schema`` enables structured outputs (Subsystem C,
        2026-05-02). When set, providers translate to their native
        schema-enforcement shape (Anthropic ``output_config.format``,
        OpenAI ``response_format`` with ``strict: true``). Providers
        without native schema enforcement should accept the kwarg as a
        no-op — callers should add JSON instructions in the prompt as a
        backup. Default ``None`` = no schema enforcement, free-form
        text response (existing behavior).

        The eval harness's ``opencomputer.inference.parse_safely``
        wrapper provides typed-fallback parsing for callers on providers
        without native schema enforcement.

        ``site`` is a free-form attribution string emitted by
        ``record_llm_call`` into ``LLMCallEvent.site`` (Phase 4 of the
        quality-foundation work, 2026-05-02). Default ``"agent_loop"``
        covers the agent loop's untreated calls. The eval harness's
        ``ProviderShim`` passes ``"eval_grader"``. Channel adapters or
        skill code can pass their own identifier for per-site cost /
        latency attribution in ``oc insights llm``.
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
        response_schema: JsonSchemaSpec | None = None,
        site: str = "agent_loop",
    ) -> AsyncIterator[StreamEvent]:
        """Stream the response.

        Yields StreamEvent objects in order. Final event has kind="done"
        and carries the complete ProviderResponse (including aggregated text
        and any tool calls). Text chunks arrive as kind="text_delta".

        ``response_schema`` — see :meth:`complete` for semantics.
        Streaming with structured outputs is supported by Anthropic;
        OpenAI partial-JSON streaming has more nuance (initial
        implementation may aggregate before yielding ``done``).

        ``site`` — see :meth:`complete` for semantics.
        """
        ...

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Declares what this provider supports for the agent loop's
        context-economy decisions. Override in concrete providers that
        opt in to reasoning resend, cache-token extraction, etc. The
        default returns the safe-baseline (everything off), so existing
        providers behave exactly as today until they explicitly opt in.
        """
        return ProviderCapabilities()

    async def complete_vision(
        self,
        *,
        model: str,
        image_base64: str,
        mime_type: str,
        prompt: str,
        max_tokens: int = 1024,
    ) -> str:
        """Run a vision completion (text + image), return assistant text.

        Default raises :class:`VisionUnsupportedError` — providers opt in
        by overriding. The expected implementation is to call ``complete()``
        with the multimodal-content array shape:
        ``[{"type": "image", "source": {"type": "base64", "media_type": ...,
        "data": ...}}, {"type": "text", "text": prompt}]`` wrapped in a
        single user :class:`Message`, then return ``response.message.content``.

        Mirrors :meth:`submit_batch` — capability is a method override
        rather than a separate registry lookup. Callers (e.g. the
        ``VisionAnalyzeTool``) catch :class:`VisionUnsupportedError` and
        surface a user-facing "vision not supported on <name>" message.
        """
        raise VisionUnsupportedError(
            f"{self.name} does not support vision (multimodal image input)"
        )

    async def submit_batch(self, requests: list[BatchRequest]) -> str:
        """Submit a batch job — returns a provider-specific batch_id.

        Subsystem E (2026-05-02). Anthropic supports natively (50% cost
        discount, ~1hr turnaround). Default raises BatchUnsupportedError
        — providers opt in by overriding.
        """
        raise BatchUnsupportedError(
            f"{self.name} does not support batch processing"
        )

    async def get_batch_results(self, batch_id: str) -> list[BatchResult]:
        """Get current results for a previously-submitted batch.

        Returns one BatchResult per request. Caller polls — this method
        does not block. Default raises BatchUnsupportedError.
        """
        raise BatchUnsupportedError(
            f"{self.name} does not support batch processing"
        )

    async def count_tokens(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
    ) -> int:
        """Count input tokens for a request — provider-agnostic interface.

        Concrete (non-abstract) default returns a heuristic estimate
        (~4 chars per token). Providers should override with their
        native endpoint (Anthropic ``messages.count_tokens``) or local
        tokenizer (OpenAI ``tiktoken``, llama-cpp, Ollama) for accuracy.

        Used by CompactionEngine, cost-guard pre-flight estimates, and
        any classifier / extractor wanting to budget input length.
        Returns ≥ 1 for any non-empty input.
        """
        return _heuristic_token_count(messages, system, tools)


__all__ = [
    "BaseProvider",
    "BatchRequest",
    "BatchResult",
    "BatchUnsupportedError",
    "CacheTokens",
    "JsonSchemaSpec",
    "ProviderCapabilities",
    "ProviderResponse",
    "RateLimitedError",
    "StreamEvent",
    "Usage",
    "VisionUnsupportedError",
]
