"""Shared channel-adapter helper classes.

Ported from gateway/platforms/helpers.py in Hermes Agent (2026.4.23) with
adaptations for OpenComputer's plugin_sdk boundary: profile_home is an
explicit parameter to ThreadParticipationTracker (no implicit ~/.hermes/).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path


class MessageDeduplicator:
    """Bounded TTL-based seen-message cache.

    Replaces ad-hoc _seen_messages dicts in adapter implementations.
    Thread-safe-ish (single-threaded asyncio assumption — no lock).
    """

    def __init__(self, max_size: int = 2000, ttl: float = 300.0) -> None:
        self._max_size = max_size
        self._ttl = ttl
        self._seen: OrderedDict[str, float] = OrderedDict()

    def is_new(self, msg_id: str) -> bool:
        """Return True if msg_id has not been seen within TTL.

        Records the message id with current timestamp on first sight.
        TTL=0 effectively disables deduplication (always returns True).
        """
        if self._ttl <= 0:
            return True
        now = time.time()
        # Lazy expiry of stale entries
        cutoff = now - self._ttl
        while self._seen and next(iter(self._seen.values())) < cutoff:
            self._seen.popitem(last=False)
        if msg_id in self._seen:
            # Hermes treats first sighting as the canonical timestamp,
            # so we keep that — do not refresh recency on re-hit.
            return False
        # Capacity-evict oldest before insert
        while len(self._seen) >= self._max_size:
            self._seen.popitem(last=False)
        self._seen[msg_id] = now
        return True


class TextBatchAggregator:
    """Coalesce rapid-fire text chunks into one dispatch per chat.

    Handler signature: ``async def(text: str, chat_id: str = "") -> None``
    when chat_aware=True; ``async def(text: str) -> None`` otherwise.

    batch_delay: window after the last submission to wait before flushing.
    split_delay: longer window applied when last buffered chunk's size is
        greater than `split_threshold` (next chunk is likely a continuation;
        give it time to arrive). Adaptive heuristic ported from Hermes for
        split-message handling.
    split_threshold: trigger adaptive delay when len(last_chunk) > threshold.
    """

    def __init__(
        self,
        handler: Callable[..., Awaitable[None]],
        batch_delay: float = 0.6,
        split_delay: float = 2.0,
        split_threshold: int = 4000,
        chat_aware: bool = False,
    ) -> None:
        self._handler = handler
        self._batch_delay = batch_delay
        self._split_delay = split_delay
        self._split_threshold = split_threshold
        self._chat_aware = chat_aware
        self._buffers: dict[str, list[str]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def submit(self, chat_id: str, text: str) -> None:
        # Cancel any in-flight flush; we extend the window each time.
        existing = self._tasks.get(chat_id)
        if existing and not existing.done():
            existing.cancel()
        self._buffers.setdefault(chat_id, []).append(text)
        delay = self._select_delay(chat_id)
        self._tasks[chat_id] = asyncio.create_task(self._flush_after(chat_id, delay))

    def _select_delay(self, chat_id: str) -> float:
        last = self._buffers.get(chat_id, [])
        if last and len(last[-1]) > self._split_threshold:
            return self._split_delay
        return self._batch_delay

    async def _flush_after(self, chat_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        chunks = self._buffers.pop(chat_id, [])
        self._tasks.pop(chat_id, None)
        if not chunks:
            return
        text = "\n".join(chunks)
        if self._chat_aware:
            await self._handler(text, chat_id)
        else:
            await self._handler(text)


_MD_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*]+)\*\*")
_MD_BOLD_UNDER_RE = re.compile(r"__([^_]+)__")
_MD_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_ITALIC_UNDER_RE = re.compile(r"(?<!_)_([^_\n]+)_(?!_)")
_MD_STRIKE_RE = re.compile(r"~~([^~]+)~~")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def strip_markdown(text: str) -> str:
    """Strip common markdown formatting to plain text.

    Used for SMS/iMessage/WhatsApp where literal markdown chars look ugly.
    Order matters: fenced code first (so its contents survive without
    backticks), then inline code, then bold/italic/strike, then headers,
    then links.
    """
    # Strip fenced code: keep contents but drop fence markers and trailing newline
    def _strip_fence(m: re.Match[str]) -> str:
        body = m.group(1)
        return body.rstrip("\n")

    text = _MD_FENCE_RE.sub(_strip_fence, text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_BOLD_DOUBLE_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDER_RE.sub(r"\1", text)
    text = _MD_STRIKE_RE.sub(r"\1", text)
    text = _MD_ITALIC_STAR_RE.sub(r"\1", text)
    text = _MD_ITALIC_UNDER_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    return text


def redact_phone(phone: str | None) -> str:
    """Redact a phone number for safe logging.

    Format: keep country code (`+NN`) and last 4 digits; replace middle
    with `***`. The country-code parse uses the heuristic "country code
    is whatever digits come before the last 10 (typical NSN length)".
    For numbers with fewer than 10 trailing digits, fall back to the
    first 1-3 digits as the country code.

    Numbers without `+` country prefix produce `***NNNN`.
    Numbers shorter than 4 digits return `+CC***` (or `***`).
    """
    if not phone:
        return ""
    raw = phone.strip()
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw[1:])
        # Heuristic: NSN is typically 10 digits → CC = digits before last 10.
        if len(digits) > 10:
            cc_digits = digits[:-10]
            rest_digits = digits[-10:]
        else:
            # Short / unusual format — assume first digit is country code.
            cc_digits = digits[:1] if digits else ""
            rest_digits = digits[1:]
        cc = f"+{cc_digits}" if cc_digits else "+"
        if len(rest_digits) >= 4:
            return f"{cc}***{rest_digits[-4:]}"
        return f"{cc}***"
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return "***"


class ThreadParticipationTracker:
    """Persistent set of thread IDs the agent has participated in.

    File-backed at ``<profile_home>/<platform>_threads.json``. Bounded
    by ``max_tracked``; oldest evicted on overflow. Atomic write.
    """

    def __init__(
        self,
        platform_name: str,
        profile_home: Path,
        max_tracked: int = 500,
    ) -> None:
        self._path = Path(profile_home) / f"{platform_name}_threads.json"
        self._max_tracked = max_tracked
        self._threads: list[str] = self._load()

    def _load(self) -> list[str]:
        try:
            data = json.loads(self._path.read_text())
            if isinstance(data, list):
                return [str(x) for x in data][-self._max_tracked:]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return []

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._threads))
            tmp.replace(self._path)
        except OSError:
            pass

    def record(self, thread_id: str) -> None:
        thread_id = str(thread_id)
        if thread_id in self._threads:
            return
        self._threads.append(thread_id)
        if len(self._threads) > self._max_tracked:
            self._threads = self._threads[-self._max_tracked:]
        self._save()

    def is_participating(self, thread_id: str) -> bool:
        return str(thread_id) in self._threads


__all__ = [
    "MessageDeduplicator",
    "TextBatchAggregator",
    "ThreadParticipationTracker",
    "redact_phone",
    "strip_markdown",
]
