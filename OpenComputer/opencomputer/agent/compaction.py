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
from dataclasses import dataclass

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
    "claude-opus-4-7": 200_000,
    "claude-opus-4-6": 200_000,
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
    ) -> None:
        self.provider = provider
        self.model = model
        self.config = config or CompactionConfig()
        self.disabled = disabled
        #: PR-6 T2.2 — optional MemoryBridge for on_pre_compress key-fact extraction.
        self._memory_bridge = memory_bridge
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
        window = context_window_for(self.model)
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
            return resp.message.content or "[compaction returned empty]"
        finally:
            self._in_progress = False

    def _truncate_fallback(
        self, messages: list[Message], split_idx: int
    ) -> CompactionResult:
        """Degraded path: drop N oldest non-system messages."""
        drop = min(self.config.fallback_drop_count, split_idx)
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


__all__ = [
    "CompactionEngine",
    "CompactionConfig",
    "CompactionResult",
    "context_window_for",
    "DEFAULT_CONTEXT_WINDOWS",
]
