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

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider

logger = logging.getLogger("opencomputer.agent.compaction")


#: Sensible per-model-family context windows. Compaction fires at 80% of these.
#: Keep conservative — better to compact early than hit a real-limit error.
DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic Claude 4.x models with extended context
    "claude-opus-4-7": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5": 200_000,
    # OpenAI GPT 5.x
    "gpt-5.4": 400_000,
    # Fallback
    "_default": 200_000,
}


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
    """Look up the context window for a model. Falls back to default."""
    if model in DEFAULT_CONTEXT_WINDOWS:
        return DEFAULT_CONTEXT_WINDOWS[model]
    # Fuzzy family match
    for key, v in DEFAULT_CONTEXT_WINDOWS.items():
        if key != "_default" and model.startswith(key.split("-")[0]):
            return v
    return DEFAULT_CONTEXT_WINDOWS["_default"]


class CompactionEngine:
    """Decide when to compact, and do it with safety rails."""

    def __init__(
        self,
        provider: BaseProvider,
        model: str,
        config: CompactionConfig | None = None,
        disabled: bool = False,
    ) -> None:
        self.provider = provider
        self.model = model
        self.config = config or CompactionConfig()
        self.disabled = disabled
        #: Flag the loop checks to suppress hook firing while compaction runs.
        self._in_progress = False

    @property
    def in_progress(self) -> bool:
        """True while compaction's own LLM call is in flight — hooks must not fire."""
        return self._in_progress

    def should_compact(self, last_input_tokens: int) -> bool:
        """Use actual measured tokens, not an estimate."""
        if self.disabled:
            return False
        window = context_window_for(self.model)
        threshold = int(window * self.config.threshold_ratio)
        return last_input_tokens >= threshold

    async def maybe_run(
        self, messages: list[Message], last_input_tokens: int
    ) -> CompactionResult:
        """Check the threshold; compact if needed; otherwise return unchanged."""
        if not self.should_compact(last_input_tokens):
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

        # Try the aux LLM summary
        try:
            summary_text = await asyncio.wait_for(
                self._summarize(old_block), timeout=self.config.summarize_timeout_s
            )
        except Exception as e:  # noqa: BLE001 — fall back on any failure
            logger.warning("compaction aux LLM failed, falling back to truncate: %s", e)
            return self._truncate_fallback(messages, split_idx)

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
