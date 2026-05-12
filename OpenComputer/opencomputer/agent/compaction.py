"""
CompactionEngine — auto-summarize old turns when the context fills up.

Design notes (per Phase 6a review):

1. Trigger uses the ACTUAL input_tokens from the last ProviderResponse.usage
   (not a character-count estimate). Different providers tokenize differently.
2. Preserves the last N messages (default 20) untouched.
3. Preserves assistant+tool_result message PAIRS atomically. Splitting a
   tool_use from its matching tool_result causes Anthropic's API to 400.
4. On aux-LLM failure or timeout, falls back to a deterministic
   "truncate-and-drop-oldest-N" strategy so the turn can still proceed.
5. Hooks and injection providers DO NOT fire inside the compaction LLM call
   (no recursion). Iteration budget is not charged.

Returns a NEW message list with the compacted range replaced by one synthetic
assistant message tagged `[compacted-summary]` so downstream tools (FTS5
search) can distinguish it from the model's own output.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from opencomputer.agent.context_engine import ContextEngine, ContextEngineResult
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider

logger = logging.getLogger("opencomputer.agent.compaction")


#: Per-model context windows. Compaction fires at ``threshold_ratio`` (80%)
#: of the listed window, so a value here is treated as the model's true
#: maximum input capacity.
#:
#: Asymmetric cost note: an UNDERSTATED window costs us a wasted aux-LLM
#: summarisation call (compact too early); an OVERSTATED window costs us
#: a failed conversation (compact too late, API rejects). When uncertain,
#: prefer smaller-than-reality.
#:
#: Sources for each value: official provider documentation as of 2026-05-02.
DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {
    # ─── Anthropic ─────────────────────────────────────────────────
    # 200k window across the Claude 3 / 4 / 5 line. Mythos preview
    # supports a 1M beta; we use the conservative 200k for it too —
    # callers needing the extended window can override via
    # CompactionConfig.
    # Wave 3 (2026-05-08) — Opus 4.6 / 4.7 ship a 1M context window
    # by default (no beta header required). Older Opus 4.x and the
    # Sonnet line still use 200k; update those individually as
    # Anthropic publishes new sizes. User can override per-model via
    # ``model_context_overrides`` in config.yaml regardless.
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-opus-4": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet-latest": 200_000,
    "claude-3-5-sonnet-20241022": 200_000,
    "claude-3-5-haiku-latest": 200_000,
    "claude-3-5-haiku-20241022": 200_000,
    "claude-3-opus-20240229": 200_000,
    "claude-mythos-preview": 200_000,
    # ─── OpenAI Chat Completions ───────────────────────────────────
    # GPT-5.x series — 400k.
    "gpt-5.4": 400_000,
    "gpt-5": 400_000,
    # GPT-4o family — 128k.
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4o-2024-08-06": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4-turbo-2024-04-09": 128_000,
    "gpt-4": 8_192,
    # GPT-3.5 — 16k (older variants 4k, but 16k is the latest).
    "gpt-3.5-turbo": 16_385,
    # OpenAI o-series reasoning — 200k.
    "o1": 200_000,
    "o1-preview": 128_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    # ─── Google Gemini ─────────────────────────────────────────────
    # Gemini 2.0 Pro — 2M.
    "gemini-2.0-pro": 2_000_000,
    "gemini-2.0-pro-exp": 2_000_000,
    # Gemini 2.0 Flash — 1M.
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.0-flash-exp": 1_000_000,
    "gemini-2.0-flash-thinking-exp": 1_000_000,
    # Gemini 1.5 — 1M (Pro), 1M (Flash).
    "gemini-1.5-pro": 1_000_000,
    "gemini-1.5-pro-latest": 1_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-1.5-flash-latest": 1_000_000,
    # ─── DeepSeek ─────────────────────────────────────────────────
    # 64k input window across DeepSeek Chat + Reasoner (R1).
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
    "deepseek-v3": 64_000,
    "deepseek-r1": 64_000,
    # ─── xAI Grok ─────────────────────────────────────────────────
    "grok-2": 131_072,
    "grok-2-mini": 131_072,
    "grok-beta": 131_072,
    # ─── Mistral ──────────────────────────────────────────────────
    "mistral-large-latest": 128_000,
    "mistral-medium": 32_000,
    "mistral-small": 32_000,
    # ─── Meta Llama (via Together / Groq / Ollama) ────────────────
    # Llama 3.1+ supports 128k; older 3.0 was 8k.
    "llama-3.1-405b": 128_000,
    "llama-3.1-70b": 128_000,
    "llama-3.1-8b": 128_000,
    "llama-3.2-90b": 128_000,
    "llama-3.2-3b": 128_000,
    "llama-3.3-70b": 128_000,
    # ─── Fallback ─────────────────────────────────────────────────
    # Conservative — most modern models clear 64k. If we don't know,
    # claim less than reality so compaction fires too early instead of
    # too late. A wasted aux-LLM call is cheaper than a failed turn.
    "_default": 64_000,
}


# Family-prefix rules: when we don't have an exact match, fall through
# to a family rule that's specific enough to be safe. Order matters —
# longer prefixes go first so ``claude-3-5-sonnet-`` wins over
# ``claude-3-`` for an unlisted variant.
_FAMILY_PREFIXES: tuple[tuple[str, int], ...] = (
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-haiku-4", 200_000),
    ("claude-3-5-sonnet", 200_000),
    ("claude-3-5-haiku", 200_000),
    ("claude-3-opus", 200_000),
    ("claude-3-sonnet", 200_000),
    ("claude-3-haiku", 200_000),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-3.5", 16_385),
    ("gemini-2.0-pro", 2_000_000),
    ("gemini-2.0-flash", 1_000_000),
    ("gemini-1.5-pro", 1_000_000),
    ("gemini-1.5-flash", 1_000_000),
    ("deepseek-", 64_000),
    ("o1-", 128_000),
    ("o3-", 200_000),
    ("llama-3.1", 128_000),
    ("llama-3.2", 128_000),
    ("llama-3.3", 128_000),
    ("grok-", 131_072),
    ("mistral-large", 128_000),
)


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    preserve_recent: int = 20
    threshold_ratio: float = 0.8
    summarize_max_tokens: int = 1024
    summarize_timeout_s: float = 30.0
    #: Number of messages to drop on aux-LLM failure fallback.
    fallback_drop_count: int = 10


@dataclass(slots=True)
class CompactionResult:
    messages: list[Message]
    did_compact: bool = False
    degraded: bool = False  # True when aux LLM failed and we truncated instead
    reason: str = ""


def context_window_with_overrides(
    model: str,
    custom_providers: tuple = (),
    model_context_overrides: dict | None = None,
    *,
    provider_hint: str = "",
    enable_probe: bool = True,
) -> int:
    """Resolve context length using the full multi-source chain.

    Resolution order (highest → lowest priority):

    1. ``model_context_overrides[<model>]`` — flat user-supplied
       per-model override that applies to *any* provider, including
       bundled ones (Anthropic, OpenAI, OpenRouter). Wins always so
       a documented vendor value can correct probe drift.
    2. ``custom_providers[].models[<model>].context_length`` — same
       intent but scoped to a named ``custom_providers`` entry.
    3. **Persistent cache + dynamic probe chain** (Wave 3 follow-up,
       2026-05-08) — :func:`context_window_probe.probe_context_window`
       hits OpenRouter's free catalog, a local Ollama server, the
       Anthropic API (when keyed), and the models.dev community
       registry, in that order. Results cached 24h on disk. Pass
       ``enable_probe=False`` to skip (used by hot-path renders that
       must stay synchronous).
    4. :func:`context_window_for` — the embedded static table +
       family-prefix rules + 64k conservative default.

    Both override layers are user-editable and survive across
    sessions; a user-overstated window is the user's risk, but if
    the override comes from concrete vendor docs it's accurate.
    """
    if model_context_overrides:
        explicit = model_context_overrides.get(model)
        if explicit is not None:
            return int(explicit)
    for cp in custom_providers:
        override = getattr(cp, "models", {}).get(model)
        if override is not None:
            ctx_len = getattr(override, "context_length", None)
            if ctx_len is not None:
                return int(ctx_len)
    # Wave 3 follow-up — dynamic probe chain (cache + OR + Ollama +
    # Anthropic + models.dev). Disabled on hot paths via the
    # ``enable_probe`` kwarg; the cache layer keeps subsequent calls
    # synchronous-fast.
    try:
        from opencomputer.agent.context_window_probe import cached_context_window

        cached = cached_context_window(model, provider_hint=provider_hint)
        if cached is not None:
            return int(cached)
    except Exception:  # noqa: BLE001
        pass
    try:
        from opencomputer.openrouter_catalog import context_length_for_model

        openrouter_ctx = context_length_for_model(model)
        if openrouter_ctx is not None:
            return int(openrouter_ctx)
    except Exception:  # noqa: BLE001
        pass
    if enable_probe:
        try:
            from opencomputer.agent.context_window_probe import probe_context_window

            probed = probe_context_window(model, provider_hint=provider_hint)
            if probed is not None:
                return probed
        except Exception:  # noqa: BLE001 — never let probe break resolution
            pass
    return context_window_for(model)


#: Conservative fallback for ``resolve_window_safe`` when the model is
#: empty / the resolution chain raises / the resolved value is non-
#: positive. 200k matches Anthropic's Claude 3.5+ default; lower
#: defaults (e.g. ``context_window_for``'s 64k) are too pessimistic for
#: "what's my agent's actual budget?" surfaces.
_SAFE_FALLBACK_WINDOW: int = 200_000


def resolve_window_safe(model: str) -> int:
    """Synchronous, never-raising context-window resolver for slash /
    CLI surfaces.

    Wraps :func:`context_window_with_overrides` with:

      - ``enable_probe=False`` — never blocks on a network round-trip.
        OK for slash commands and CLI renders; bad for any
        background-time accuracy path (use the un-suffixed variant).
      - ``try/except`` around the resolution chain — a corrupt config
        or unexpected import raises a debug log + falls back rather
        than crashing the user-facing surface.
      - Floor of :data:`_SAFE_FALLBACK_WINDOW` (200k) when the
        resolved value is empty or non-positive — keeps `%`-of-context
        math meaningful.

    Returns the resolved or fallback window as an int. Used by
    ``/context`` (slash), ``oc context show`` / ``list``, and any
    future read-only "show me the context budget" surface.
    """
    if not model:
        return _SAFE_FALLBACK_WINDOW
    try:
        resolved = context_window_with_overrides(model, enable_probe=False)
    except Exception:  # noqa: BLE001 — caller is read-only / must not crash
        import logging

        logging.getLogger(__name__).debug(
            "resolve_window_safe: resolution failed for model=%s — using fallback",
            model,
        )
        return _SAFE_FALLBACK_WINDOW
    if not resolved or int(resolved) <= 0:
        return _SAFE_FALLBACK_WINDOW
    return int(resolved)


def context_window_for(model: str) -> int:
    """Look up the context window for a model.

    Resolution order:
      1. Exact match in :data:`DEFAULT_CONTEXT_WINDOWS`.
      2. Most-specific family-prefix rule from
         :data:`_FAMILY_PREFIXES`.
      3. Conservative default (``_default`` entry; 64k today).

    The previous implementation used ``model.startswith(key.split("-")[0])``
    which produced silent false positives — e.g. ``gpt-4o`` matched
    ``gpt-5.4``'s entry and inherited a 400k window despite its real
    128k limit. Removed entirely; this function now never widens a
    model's window beyond what we have evidence for.
    """
    if model in DEFAULT_CONTEXT_WINDOWS:
        return DEFAULT_CONTEXT_WINDOWS[model]
    # Family prefixes — first match wins. ``_FAMILY_PREFIXES`` is
    # ordered most-specific first so ``claude-3-5-sonnet-...`` wins
    # over a hypothetical broader rule.
    for prefix, window in _FAMILY_PREFIXES:
        if model.startswith(prefix):
            return window
    return DEFAULT_CONTEXT_WINDOWS["_default"]


class CompactionEngine(ContextEngine):
    """Decide when to compact, and do it with safety rails.

    Implements the :class:`ContextEngine` ABC — the agent loop binds
    this engine via the ``"compressor"`` name in the registry by
    default. Subclasses or plugin-provided alternatives can replace
    it via ``LoopConfig.context_engine``.
    """

    name: str = "compressor"

    def __init__(
        self,
        provider: BaseProvider,
        model: str,
        config: CompactionConfig | None = None,
        disabled: bool = False,
        memory_bridge: object | None = None,
        usage_recorder: Callable[[Any], None] | None = None,
        custom_providers: tuple = (),
    ) -> None:
        self.provider = provider
        self.model = model
        self.config = config or CompactionConfig()
        self.disabled = disabled
        #: PR-6 T2.2 — optional MemoryBridge for on_pre_compress key-fact extraction.
        self._memory_bridge = memory_bridge
        #: Wave 3 (2026-05-08) — pass-through of Config.custom_providers
        #: so should_compact can honor per-model context_length overrides
        #: declared under ``custom_providers[].models[<id>].context_length``.
        self._custom_providers = custom_providers
        #: Hermes B4 follow-up — optional callback fired with the
        #: ``ProviderResponse.usage`` after each compaction LLM call.
        #: Caller (typically AgentLoop) supplies this to route compaction
        #: cost into ``llm_calls`` so insights reflects the *full*
        #: conversation cost, not just the user-visible reply.
        self._usage_recorder = usage_recorder
        #: Flag the loop checks to suppress hook firing while compaction runs.
        self._in_progress = False

    @property
    def in_progress(self) -> bool:  # type: ignore[override]
        """True while compaction's own LLM call is in flight — hooks must not fire."""
        return self._in_progress

    # ─── ContextEngine ABC implementation ──────────────────────────

    def should_compress(self, *, last_input_tokens: int) -> bool:
        """ABC entry point. Delegates to the existing ``should_compact``."""
        return self.should_compact(last_input_tokens)

    async def compress(
        self, *, messages: list[Message], last_input_tokens: int
    ) -> ContextEngineResult:
        """ABC entry point. Wraps ``maybe_run`` and converts to the ABC's
        result shape.
        """
        legacy = await self.maybe_run(messages, last_input_tokens)
        return ContextEngineResult(
            messages=legacy.messages,
            did_compress=legacy.did_compact,
            degraded=legacy.degraded,
            reason=legacy.reason,
        )

    # ─── Existing API kept verbatim for callers ─────────────────────

    def should_compact(self, last_input_tokens: int) -> bool:
        """Use actual measured tokens, not an estimate."""
        if self.disabled:
            return False
        window = context_window_with_overrides(self.model, self._custom_providers)
        threshold = int(window * self.config.threshold_ratio)
        return last_input_tokens >= threshold

    async def should_compact_now(
        self,
        messages: list[Message],
        *,
        system: str = "",
    ) -> bool:
        """Subsystem D follow-up (2026-05-02) — pre-flight compaction check
        using the provider's ``count_tokens``.

        Useful when the caller doesn't have a recent ``last_input_tokens``
        figure (e.g., out-of-band ``/compress`` invocation, fresh session
        resume, cost-guard pre-flight). Asks the provider to count the
        current messages and applies the same threshold check as
        :meth:`should_compact`.

        Provider-agnostic — every BaseProvider's ``count_tokens`` falls
        back to a heuristic if no native tokenizer is available, so this
        method always returns a usable answer.
        """
        if self.disabled:
            return False
        try:
            tokens = await self.provider.count_tokens(
                model=self.model,
                messages=messages,
                system=system,
            )
        except Exception:  # noqa: BLE001 — pre-flight is best-effort
            # Fall back to "don't compact" rather than blocking the caller.
            # CompactionEngine.maybe_run still has its own count-aware path.
            return False
        return self.should_compact(tokens)

    async def maybe_run(
        self, messages: list[Message], last_input_tokens: int,
        *, force: bool = False,
    ) -> CompactionResult:
        """Check the threshold; compact if needed; otherwise return unchanged.

        When ``force=True`` the threshold check is skipped — used by the
        ``/compress`` slash command so users can request manual
        compaction below the auto-trigger threshold (2026-04-30).
        """
        if not force and not self.should_compact(last_input_tokens):
            return CompactionResult(messages=messages, did_compact=False)

        # Decide which messages to compact. Preserve:
        #  - System messages at the start
        #  - The last N messages untouched
        recent_count = self.config.preserve_recent
        if len(messages) <= recent_count + 1:
            # Not enough old messages to bother — no-op
            return CompactionResult(messages=messages, did_compact=False)

        # Split at a SAFE boundary — must not split tool_use from tool_result.
        split_idx = self._safe_split_index(messages, recent_count)
        if split_idx <= 0:
            return CompactionResult(messages=messages, did_compact=False)

        old_block = messages[:split_idx]
        recent_block = messages[split_idx:]

        # PR-6 T2.2 — extract key facts from providers BEFORE the aux LLM
        # summarises (so facts survive compaction). Failures are isolated;
        # compaction proceeds without facts if the bridge is unavailable.
        key_facts = ""
        if self._memory_bridge is not None:
            try:
                key_facts = await self._memory_bridge.collect_pre_compress(old_block)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "compaction: collect_pre_compress failed; proceeding without"
                )

        # Try the aux LLM summary
        try:
            summary_text = await asyncio.wait_for(
                self._summarize(old_block), timeout=self.config.summarize_timeout_s
            )
        except Exception as e:  # noqa: BLE001 — fall back on any failure
            logger.warning("compaction aux LLM failed, falling back to truncate: %s", e)
            return self._truncate_fallback(messages, split_idx)

        # Prepend provider key-facts so they survive the compaction summary.
        if key_facts:
            summary_text = (
                "<KEY-FACTS-DO-NOT-SUMMARIZE>\n"
                f"{key_facts}\n"
                "</KEY-FACTS-DO-NOT-SUMMARIZE>\n\n"
                + summary_text
            )

        # Success — replace old_block with one synthetic summary message
        synthetic = Message(
            role="assistant",
            content=f"[compacted-summary]\n\n{summary_text}",
        )
        new_msgs = [synthetic, *recent_block]
        return CompactionResult(messages=new_msgs, did_compact=True, reason="aux-summary")

    # ─── internals ────────────────────────────────────────────────

    def _safe_split_index(
        self, messages: list[Message], preserve_recent: int
    ) -> int:
        """
        Find a split point at `len(messages) - preserve_recent` that does NOT
        break a tool_use / tool_result pair.

        Walk backwards from the target index. If the candidate boundary has
        a tool_result right after a tool_use, move earlier until we're between
        a clean turn boundary.
        """
        if len(messages) <= preserve_recent:
            return 0
        target = len(messages) - preserve_recent

        # Scan backward: if messages[target] is a tool result, move back until
        # we land right BEFORE its originating assistant tool_use message
        # (ideally at a user message or a clean assistant reply).
        idx = target
        while idx > 0:
            msg = messages[idx]
            prev = messages[idx - 1] if idx > 0 else None
            # Unsafe: `idx` points to a tool result and prev is an assistant
            # message containing tool_use blocks — splitting would orphan them.
            prev_has_tool_use = (
                prev is not None
                and prev.role == "assistant"
                and bool(prev.tool_calls)
            )
            if msg.role == "tool" or prev_has_tool_use:
                idx -= 1
                continue
            break
        return idx

    async def _summarize(self, old_block: list[Message]) -> str:
        """Call the provider to summarize. Hooks/injection must NOT fire here."""
        self._in_progress = True
        try:
            # Keep the prompt simple. The provider returns a plain Message.
            prompt = Message(
                role="user",
                content=(
                    "Summarize the following conversation history tightly. "
                    "Keep facts, decisions, file paths, and any commands run. "
                    "Output plain prose, no markdown headers. Target ~300 words."
                ),
            )
            # Flatten history into text — providers need canonical messages.
            synth_history = _flatten_for_summary(old_block)
            resp = await self.provider.complete(
                model=self.model,
                messages=[Message(role="user", content=synth_history), prompt],
                max_tokens=self.config.summarize_max_tokens,
                temperature=0.3,
            )
            # Hermes B4 follow-up — emit usage so AgentLoop can record
            # the compaction call into ``llm_calls``. Best-effort:
            # telemetry must never wedge compaction.
            if self._usage_recorder is not None:
                try:
                    self._usage_recorder(resp.usage)
                except Exception:  # noqa: BLE001
                    logger.debug("compaction usage_recorder swallowed", exc_info=True)
            return resp.message.content or "[compaction returned empty]"
        finally:
            self._in_progress = False

    def _truncate_fallback(
        self, messages: list[Message], split_idx: int
    ) -> CompactionResult:
        """Degraded path: drop N oldest non-system messages.

        The naive ``messages[drop:]`` slice can orphan a ``tool_result``
        from its matching ``tool_use`` when the cut lands inside a tool
        cycle (e.g. ``messages[drop]`` is a ``tool`` whose owning
        assistant lives at ``messages[drop-1]``). That produces a 400
        from Anthropic on the next turn:

            messages.N.content.0: unexpected ``tool_use_id`` ... Each
            ``tool_result`` block must have a corresponding ``tool_use``
            block in the previous message.

        Mirror :meth:`_safe_split_index` here: walk the drop boundary
        forward (toward the recent end) until it lands on a clean turn
        boundary — i.e. the FIRST surviving message is not a ``tool``,
        and the message before it (which is being dropped) is not an
        assistant carrying ``tool_calls``. Walking forward (not
        backward) preserves the "drop at least N" intent of the
        fallback while never producing an orphan. If no clean boundary
        exists in the range, drop everything (degraded but coherent).
        """
        target = min(self.config.fallback_drop_count, split_idx)
        drop = self._safe_drop_index(messages, target)
        new_msgs = messages[drop:]
        synthetic = Message(
            role="assistant",
            content=f"[compacted-truncated] — {drop} oldest messages removed due to compaction failure",
        )
        return CompactionResult(
            messages=[synthetic, *new_msgs],
            did_compact=True,
            degraded=True,
            reason="aux-failed-truncated",
        )

    def _safe_drop_index(
        self, messages: list[Message], target: int
    ) -> int:
        """Forward-walk ``target`` to a tool-pair-safe boundary.

        Returns the smallest ``drop >= target`` such that
        ``messages[drop:]`` does not begin with a ``tool`` whose
        matching ``tool_use`` lies in ``messages[:drop]``, AND
        ``messages[drop-1]`` (the LAST dropped message) is not an
        assistant whose ``tool_calls`` reference a ``tool_call_id`` in
        the surviving slice.

        If walking past the end is required (no clean boundary exists),
        return ``len(messages)`` — the fallback then drops everything,
        which is degraded but never orphans a tool_use/tool_result
        pair on the wire.
        """
        n = len(messages)
        idx = max(0, min(target, n))
        while idx < n:
            head = messages[idx]
            prev = messages[idx - 1] if idx > 0 else None
            prev_has_tool_use = (
                prev is not None
                and prev.role == "assistant"
                and bool(prev.tool_calls)
            )
            # Unsafe if surviving slice starts with a ``tool`` (its
            # matching tool_use would be in the dropped prefix), OR if
            # the LAST dropped message is an assistant with tool_calls
            # (its matching tool_results would be in the surviving
            # slice). Walk forward in either case.
            if head.role == "tool" or prev_has_tool_use:
                idx += 1
                continue
            return idx
        return n


def _flatten_for_summary(messages: list[Message]) -> str:
    """Render a message list as plain text for the summarizer."""
    parts: list[str] = []
    for m in messages:
        role = m.role.upper()
        content = m.content or ""
        if m.tool_calls:
            tool_names = ", ".join(tc.name for tc in m.tool_calls)
            content = (content + f"\n[called tools: {tool_names}]").strip()
        parts.append(f"{role}: {content}")
    return "\n\n".join(parts)


# ─── runtime-state resolvers ─────────────────────────────────────────
# Shared between the TUI status-line bar (``opencomputer.cli_ui.status_line``)
# and the ``/context`` slash command (``opencomputer.agent.slash_commands_impl
# .context_cmd``). Centralising the resolution kills the drift the deep-dive
# caught: before this layer the bar summed ``session_tokens_in +
# session_tokens_out`` (a 10x inflation after ~10 turns) while ``/context``
# read ``last_input_tokens`` correctly; and ``/context`` displayed
# "compaction triggers at: 98%" while the engine fires at 80%.


def _coerce_pos_int_for_tokens(value: object) -> int:
    """Strict positive-int coercion used by :func:`resolve_current_input_tokens`.

    Rejects ``bool`` (an int-subclass that would otherwise pass), NaN,
    ``inf``, ``None``, list / dict, and negative values. Numeric
    strings are tolerated for YAML round-trip robustness.

    Returns ``0`` for anything that doesn't cleanly resolve to a
    positive integer — callers treat ``0`` as "fall through to next
    signal".
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        # NaN check (NaN != NaN per IEEE 754) AND inf check AND non-positive.
        if value != value or value == float("inf") or value <= 0:
            return 0
        try:
            return int(value)
        except (ValueError, OverflowError):
            return 0
    if isinstance(value, str):
        try:
            v = int(value)
            return v if v > 0 else 0
        except (TypeError, ValueError):
            return 0
    return 0


def resolve_current_input_tokens(custom: dict | None) -> int:
    """Return the current-turn input-token count for context-window meters.

    THE single resolver shared by the TUI status-line bar and the
    ``/context`` slash command. Centralising here is the fix for the
    bug the deep-dive caught — before this layer, two surfaces hand-
    typed their own logic and drifted.

    Resolution priority:

      1. ``last_input_tokens`` — the most recent LLM call's
         provider-reported ``input_tokens``. Written by the agent
         loop after each successful response. This is the actual
         current request size — what "% of context used right now"
         really means.
      2. ``session_tokens_in`` — cumulative input across the
         session. Used as a fallback for one-shot CLI mode
         (``cli._sync_runtime_token_tally``) where the loop never
         gets a chance to populate ``last_input_tokens``.

    Output tokens (``session_tokens_out``) are NEVER summed in: every
    output token re-enters the next turn's input and is already
    counted in ``last_input_tokens`` then. Summing both double-counts
    the same content.

    All inputs validated defensively — a buggy plugin that stomped a
    string / list / NaN onto either key must never crash the bar.

    Args:
        custom: the ``runtime.custom`` dict. ``None`` is tolerated for
            cold-start callers that haven't built a runtime yet.

    Returns:
        Non-negative ``int``. ``0`` only when neither signal carries
        a positive value (true cold start).
    """
    if not isinstance(custom, dict):
        return 0
    last_input = _coerce_pos_int_for_tokens(custom.get("last_input_tokens"))
    if last_input > 0:
        return last_input
    return _coerce_pos_int_for_tokens(custom.get("session_tokens_in"))


def resolve_effective_compaction_threshold_ratio(
    custom: dict | None,
) -> float:
    """Return the compaction trigger ratio for ``/context`` to display.

    Reads ``runtime.custom["compaction_threshold_ratio"]`` (populated
    each turn by the agent loop from the active compaction engine's
    ``config.threshold_ratio``) so a user who customises the ratio
    via ``config.yaml`` sees the customisation in ``/context``. Falls
    back to :class:`CompactionConfig` 's default when the key is
    missing — guaranteeing the displayed value matches whatever the
    engine would actually fire at on an unconfigured install.

    Validation rejects values outside ``(0.0, 1.0]``, ``None``,
    ``bool``, ``str``, ``NaN``, and ``inf``. The "out-of-range"
    branch protects the panel from a corrupt config rendering
    "compaction triggers at 9900%".

    Args:
        custom: the ``runtime.custom`` dict. ``None`` is tolerated.

    Returns:
        Float in ``(0.0, 1.0]`` — either the validated override or
        :attr:`CompactionConfig.threshold_ratio`.
    """
    default = CompactionConfig().threshold_ratio
    if not isinstance(custom, dict):
        return default
    raw = custom.get("compaction_threshold_ratio")
    # Explicit None / bool / non-numeric rejection. ``bool`` is an
    # ``int`` subclass — True would otherwise pass as ``1.0``.
    if raw is None or isinstance(raw, bool):
        return default
    if not isinstance(raw, (int, float)):
        return default
    if isinstance(raw, float) and (raw != raw or raw == float("inf")):
        # NaN or +inf.
        return default
    ratio = float(raw)
    if not (0.0 < ratio <= 1.0):
        return default
    return ratio


__all__ = [
    "CompactionEngine",
    "CompactionConfig",
    "CompactionResult",
    "context_window_for",
    "context_window_with_overrides",
    "resolve_current_input_tokens",
    "resolve_effective_compaction_threshold_ratio",
    "resolve_window_safe",
    "DEFAULT_CONTEXT_WINDOWS",
]
