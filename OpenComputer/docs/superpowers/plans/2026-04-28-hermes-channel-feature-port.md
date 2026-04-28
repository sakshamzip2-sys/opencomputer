# Hermes Channel Feature Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port high-value channel adapter and shared messaging-infrastructure features from Hermes Agent into OpenComputer (Tiers 1+2+3 from spec) without losing OC's preserved invariants (plugin_sdk boundary, F1 consent layer, centralized session lock, outgoing queue, profile system).

**Architecture:** Helper-extraction approach. New shared modules in `plugin_sdk/` (helpers, utils, network, format converters). Enhanced `BaseChannelAdapter` with retry + lifecycle hooks. Adapter wiring per channel. New plugin `extensions/whatsapp-bridge/` for personal WhatsApp via Baileys.

**Tech Stack:** Python 3.12+, httpx, aiohttp, pytest, ruff, plugin_sdk contract, F1 ConsentGate.

**Spec:** `OpenComputer/docs/superpowers/specs/2026-04-28-hermes-channel-feature-port-design.md`

**Branch:** `feat/hermes-channel-feature-port` (off main)

**Total scope:** 6 PRs, ~50 tasks, ~150 new tests, ~+3,500 LOC, ~-300 LOC dedup.

---

## Pre-flight

- [ ] **Step 0.1: Create feature branch from main**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git checkout main
git pull
git checkout -b feat/hermes-channel-feature-port
```

- [ ] **Step 0.2: Verify baseline test suite passes**

```bash
source .venv/bin/activate
pytest tests/ -q --tb=no 2>&1 | tail -5
```

Expected: `885 passed` (or current baseline from CLAUDE.md). If failures: STOP, baseline is broken; do not proceed.

- [ ] **Step 0.3: Verify ruff baseline clean**

```bash
ruff check plugin_sdk/ opencomputer/ extensions/ tests/ 2>&1 | tail -5
```

Expected: `All checks passed.` Otherwise: address pre-existing lint debt before adding more code.

---

# PR 1 — Foundation modules in plugin_sdk

**Goal:** Add `channel_helpers.py`, `channel_utils.py`, `network_utils.py`, `format_converters/` package + comprehensive tests.

**Risk:** Low. New files only. No existing code modified.

**Estimated:** 2 days.

---

## Task 1.1: `plugin_sdk/channel_helpers.py`

**Files:**
- Create: `plugin_sdk/channel_helpers.py`
- Test: `tests/test_channel_helpers.py`

- [ ] **Step 1.1.1: Write failing tests for MessageDeduplicator**

```python
# tests/test_channel_helpers.py
import time
import pytest
from plugin_sdk.channel_helpers import MessageDeduplicator


def test_message_deduplicator_first_seen_is_new():
    dedup = MessageDeduplicator(max_size=100, ttl=60.0)
    assert dedup.is_new("msg-1") is True
    assert dedup.is_new("msg-1") is False  # second call: already seen


def test_message_deduplicator_ttl_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("plugin_sdk.channel_helpers.time.time", lambda: now[0])
    dedup = MessageDeduplicator(max_size=100, ttl=60.0)
    dedup.is_new("msg-1")
    now[0] += 30
    assert dedup.is_new("msg-1") is False  # within TTL
    now[0] += 31
    assert dedup.is_new("msg-1") is True  # past TTL, fresh


def test_message_deduplicator_max_size_eviction():
    dedup = MessageDeduplicator(max_size=3, ttl=300.0)
    for i in range(5):
        dedup.is_new(f"msg-{i}")
    # First two should have been evicted
    assert dedup.is_new("msg-0") is True
    assert dedup.is_new("msg-1") is True
    assert dedup.is_new("msg-4") is False


def test_message_deduplicator_ttl_zero_disables():
    dedup = MessageDeduplicator(max_size=100, ttl=0.0)
    assert dedup.is_new("msg-1") is True
    assert dedup.is_new("msg-1") is True  # always new when ttl=0
```

- [ ] **Step 1.1.2: Run tests to verify they fail**

```bash
pytest tests/test_channel_helpers.py::test_message_deduplicator_first_seen_is_new -v
```

Expected: `ImportError: cannot import name 'MessageDeduplicator' from 'plugin_sdk.channel_helpers'`

- [ ] **Step 1.1.3: Implement MessageDeduplicator**

```python
# plugin_sdk/channel_helpers.py
"""Shared channel-adapter helper classes.

Ported from gateway/platforms/helpers.py in Hermes Agent (2026.4.23) with
adaptations for OpenComputer's plugin_sdk boundary: profile_home is an
explicit parameter to ThreadParticipationTracker (no implicit ~/.hermes/).
"""
from __future__ import annotations

import json
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Awaitable, Callable, Optional


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
            # Refresh recency? No — Hermes treats first sighting as the
            # canonical timestamp, so we keep that.
            return False
        # Capacity-evict oldest before insert
        while len(self._seen) >= self._max_size:
            self._seen.popitem(last=False)
        self._seen[msg_id] = now
        return True
```

- [ ] **Step 1.1.4: Run MessageDeduplicator tests, verify pass**

```bash
pytest tests/test_channel_helpers.py -v -k MessageDeduplicator
```

Expected: 4 PASS.

- [ ] **Step 1.1.5: Write failing tests for TextBatchAggregator**

```python
# Append to tests/test_channel_helpers.py
import asyncio
from plugin_sdk.channel_helpers import TextBatchAggregator


@pytest.mark.asyncio
async def test_text_batch_aggregator_single_dispatch():
    received: list[str] = []
    async def handler(text: str) -> None:
        received.append(text)
    agg = TextBatchAggregator(handler, batch_delay=0.05, split_delay=0.1, split_threshold=4000)
    await agg.submit("chat-1", "hello")
    await asyncio.sleep(0.1)
    assert received == ["hello"]


@pytest.mark.asyncio
async def test_text_batch_aggregator_combines_within_window():
    received: list[str] = []
    async def handler(text: str) -> None:
        received.append(text)
    agg = TextBatchAggregator(handler, batch_delay=0.1, split_delay=0.2, split_threshold=4000)
    await agg.submit("chat-1", "part 1")
    await asyncio.sleep(0.02)
    await agg.submit("chat-1", "part 2")
    await asyncio.sleep(0.15)
    assert received == ["part 1\npart 2"]


@pytest.mark.asyncio
async def test_text_batch_aggregator_per_chat_isolation():
    received: list[tuple[str, str]] = []
    async def handler(text: str, chat: str) -> None:
        received.append((chat, text))
    agg = TextBatchAggregator(
        lambda text, chat="": handler(text, chat),
        batch_delay=0.05, split_delay=0.1, split_threshold=4000,
        chat_aware=True,
    )
    await agg.submit("chat-A", "hello A")
    await agg.submit("chat-B", "hello B")
    await asyncio.sleep(0.1)
    assert {"chat-A", "chat-B"} == {c for c, _ in received}


@pytest.mark.asyncio
async def test_text_batch_aggregator_adaptive_split_near_limit():
    received: list[str] = []
    async def handler(text: str) -> None:
        received.append(text)
    agg = TextBatchAggregator(handler, batch_delay=0.05, split_delay=0.2, split_threshold=10)
    big = "x" * 9
    await agg.submit("chat-1", big)
    # Within split_delay window, the next chunk should NOT merge
    await asyncio.sleep(0.06)
    await agg.submit("chat-1", "y" * 5)
    await asyncio.sleep(0.25)
    assert received == [big, "y" * 5]
```

- [ ] **Step 1.1.6: Implement TextBatchAggregator**

```python
# Append to plugin_sdk/channel_helpers.py
class TextBatchAggregator:
    """Coalesce rapid-fire text chunks into one dispatch per chat.

    Handler signature: ``async def(text: str, chat_id: str = "") -> None``
    when chat_aware=True; ``async def(text: str) -> None`` otherwise.

    batch_delay: window after the last submission to wait before flushing.
    split_delay: longer window applied when last buffered chunk's size is
        within `split_threshold` of the limit (next chunk is likely a
        continuation; give it time to arrive). Adaptive heuristic ported
        from Hermes for split-message handling.
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
```

- [ ] **Step 1.1.7: Run TextBatchAggregator tests, verify pass**

```bash
pytest tests/test_channel_helpers.py -v -k TextBatchAggregator
```

Expected: 4 PASS. Note: `pytest-asyncio` must be configured (it already is per OC's setup).

- [ ] **Step 1.1.8: Write failing tests + implement strip_markdown**

```python
# Append to tests/test_channel_helpers.py
from plugin_sdk.channel_helpers import strip_markdown


@pytest.mark.parametrize("input_text,expected", [
    ("**bold**", "bold"),
    ("*italic*", "italic"),
    ("__underline__", "underline"),
    ("_italic_", "italic"),
    ("~~strike~~", "strike"),
    ("# Heading", "Heading"),
    ("## Heading 2", "Heading 2"),
    ("`code`", "code"),
    ("[link text](https://example.com)", "link text"),
    ("```python\nx = 1\n```", "x = 1"),
    ("plain text", "plain text"),
    ("multi\n**line**\nformat", "multi\nline\nformat"),
])
def test_strip_markdown_basic(input_text, expected):
    assert strip_markdown(input_text) == expected
```

```python
# Append to plugin_sdk/channel_helpers.py
_MD_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.DOTALL)
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*]+)\*\*")
_MD_BOLD_UNDER_RE = re.compile(r"__([^_]+)__")
_MD_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_ITALIC_UNDER_RE = re.compile(r"(?<!_)_([^_\n]+)_(?!_)")
_MD_STRIKE_RE = re.compile(r"~~([^~]+)~~")
_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")


def strip_markdown(text: str) -> str:
    """Strip common markdown formatting to plain text.

    Used for SMS/iMessage/WhatsApp where literal markdown chars look ugly.
    Order matters: fenced code first (so its contents survive without
    backticks), then inline code, then bold/italic/strike, then headers,
    then links.
    """
    text = _MD_FENCE_RE.sub(lambda m: m.group(1), text)
    text = _MD_INLINE_CODE_RE.sub(r"\1", text)
    text = _MD_BOLD_DOUBLE_RE.sub(r"\1", text)
    text = _MD_BOLD_UNDER_RE.sub(r"\1", text)
    text = _MD_STRIKE_RE.sub(r"\1", text)
    text = _MD_ITALIC_STAR_RE.sub(r"\1", text)
    text = _MD_ITALIC_UNDER_RE.sub(r"\1", text)
    text = _MD_HEADING_RE.sub(r"\1", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    return text
```

- [ ] **Step 1.1.9: Run strip_markdown tests, verify pass**

```bash
pytest tests/test_channel_helpers.py -v -k strip_markdown
```

Expected: 12 PASS (parametrized).

- [ ] **Step 1.1.10: Write failing tests + implement redact_phone**

```python
# Append to tests/test_channel_helpers.py
from plugin_sdk.channel_helpers import redact_phone


@pytest.mark.parametrize("phone,expected", [
    ("+15551234567", "+1***4567"),
    ("+919876543210", "+91***3210"),
    ("+447911123456", "+44***3456"),
    ("5551234567", "***4567"),       # no country code
    ("+1234", "+1***"),               # short — country code only + redaction marker
    ("", ""),
    (None, ""),
])
def test_redact_phone(phone, expected):
    assert redact_phone(phone) == expected
```

```python
# Append to plugin_sdk/channel_helpers.py
def redact_phone(phone: Optional[str]) -> str:
    """Redact a phone number for safe logging.

    Format: keep country code (`+NN`) and last 4 digits; replace middle
    with `***`. Numbers without `+` country prefix produce `***NNNN`.
    Numbers shorter than 4 digits return `+CC***` (or `***`).
    """
    if not phone:
        return ""
    raw = phone.strip()
    if raw.startswith("+"):
        # Identify country-code prefix (1-3 digits typical)
        cc_match = re.match(r"\+(\d{1,3})", raw)
        cc = f"+{cc_match.group(1)}" if cc_match else "+"
        rest = raw[len(cc):]
        if len(rest) >= 4:
            return f"{cc}***{rest[-4:]}"
        return f"{cc}***"
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return "***"
```

- [ ] **Step 1.1.11: Run redact_phone tests, verify pass**

```bash
pytest tests/test_channel_helpers.py -v -k redact_phone
```

Expected: 7 PASS.

- [ ] **Step 1.1.12: Write failing tests + implement ThreadParticipationTracker**

```python
# Append to tests/test_channel_helpers.py
from plugin_sdk.channel_helpers import ThreadParticipationTracker


def test_thread_tracker_records_and_persists(tmp_path):
    tracker = ThreadParticipationTracker("discord", profile_home=tmp_path, max_tracked=10)
    tracker.record("thread-1")
    tracker.record("thread-2")
    assert tracker.is_participating("thread-1")
    assert tracker.is_participating("thread-2")
    # Reload from disk
    tracker2 = ThreadParticipationTracker("discord", profile_home=tmp_path, max_tracked=10)
    assert tracker2.is_participating("thread-1")
    assert tracker2.is_participating("thread-2")


def test_thread_tracker_max_bound_evicts_oldest(tmp_path):
    tracker = ThreadParticipationTracker("matrix", profile_home=tmp_path, max_tracked=3)
    for i in range(5):
        tracker.record(f"thread-{i}")
    assert not tracker.is_participating("thread-0")
    assert not tracker.is_participating("thread-1")
    assert tracker.is_participating("thread-4")


def test_thread_tracker_per_platform_isolated(tmp_path):
    a = ThreadParticipationTracker("discord", profile_home=tmp_path, max_tracked=10)
    b = ThreadParticipationTracker("matrix", profile_home=tmp_path, max_tracked=10)
    a.record("shared-id")
    assert a.is_participating("shared-id")
    assert not b.is_participating("shared-id")
```

```python
# Append to plugin_sdk/channel_helpers.py
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
```

- [ ] **Step 1.1.13: Run all channel_helpers tests, verify pass**

```bash
pytest tests/test_channel_helpers.py -v
```

Expected: ~30 PASS.

- [ ] **Step 1.1.14: Run lint**

```bash
ruff check plugin_sdk/channel_helpers.py tests/test_channel_helpers.py
```

Expected: All checks passed.

- [ ] **Step 1.1.15: Commit**

```bash
git add plugin_sdk/channel_helpers.py tests/test_channel_helpers.py
git commit -m "$(cat <<'EOF'
feat(plugin_sdk): add channel_helpers module

Port shared adapter helpers from Hermes:
- MessageDeduplicator (bounded TTL seen-message cache)
- TextBatchAggregator (coalesce rapid-fire text chunks per chat)
- strip_markdown (plain-text fallback for SMS/iMessage/WhatsApp)
- redact_phone (PII redaction for signal/sms/imessage logs)
- ThreadParticipationTracker (persistent thread-id set per platform)

Adaptations for OC:
- ThreadParticipationTracker takes profile_home explicitly (Hermes uses
  fixed ~/.hermes/); preserves OC's per-profile isolation invariant.
- Pure stdlib + plugin_sdk-only deps; respects test_phase6a boundary.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 1.2: `plugin_sdk/channel_utils.py`

**Files:**
- Create: `plugin_sdk/channel_utils.py`
- Test: `tests/test_channel_utils.py`
- Modify: `plugin_sdk/core.py:` (add `ProcessingOutcome` enum)

- [ ] **Step 1.2.1: Add ProcessingOutcome to plugin_sdk/core.py**

Edit `plugin_sdk/core.py` to add at the bottom (preserve existing exports):

```python
class ProcessingOutcome(str, Enum):
    """Outcome reported to BaseChannelAdapter.on_processing_complete."""
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
```

Append `ProcessingOutcome` to the `__all__` list if one exists.

- [ ] **Step 1.2.2: Write failing tests for utf16_len + truncate_message_smart**

```python
# tests/test_channel_utils.py
import pytest
from plugin_sdk.channel_utils import (
    utf16_len,
    truncate_message_smart,
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_VIDEO_TYPES,
)


@pytest.mark.parametrize("s,expected", [
    ("hello", 5),
    ("", 0),
    ("café", 4),         # é is BMP, 1 unit
    ("👍", 2),            # emoji = surrogate pair
    ("hi👍there", 9),     # 2+2+5 = 9
])
def test_utf16_len(s, expected):
    assert utf16_len(s) == expected


def test_truncate_message_smart_short():
    assert truncate_message_smart("hello", max_length=100) == ["hello"]


def test_truncate_message_smart_simple_split():
    text = "x" * 250
    chunks = truncate_message_smart(text, max_length=100)
    # Each chunk fits (with " (i/N)" indicator overhead)
    for c in chunks:
        assert len(c) <= 100


def test_truncate_message_smart_reopens_code_fence():
    text = "intro\n```python\n" + "x = 1\n" * 50 + "```\nouter"
    chunks = truncate_message_smart(text, max_length=80)
    # Every chunk except possibly first/last should start with ```
    # if it falls inside the fenced region
    for i, chunk in enumerate(chunks[1:-1], start=1):
        # If a chunk is in the middle and fenced, it must reopen
        if "x = 1" in chunk and not chunk.startswith("```"):
            pytest.fail(f"Chunk {i} mid-fence does not reopen code block")


def test_truncate_message_smart_indicator_appended():
    text = "x" * 250
    chunks = truncate_message_smart(text, max_length=50)
    # Multi-chunk: indicator like " (1/N)"
    assert "(1/" in chunks[0]
    last_idx = len(chunks)
    assert f"({last_idx}/{last_idx})" in chunks[-1]


def test_truncate_message_smart_utf16_aware():
    # 2050 surrogate-pair emojis (4100 utf16 units)
    text = "👍" * 2050
    chunks = truncate_message_smart(text, max_length=4096, len_fn=utf16_len)
    for c in chunks:
        assert utf16_len(c) <= 4096


def test_supported_document_types_has_pdf_md_zip_office():
    assert ".pdf" in SUPPORTED_DOCUMENT_TYPES
    assert ".md" in SUPPORTED_DOCUMENT_TYPES
    assert ".zip" in SUPPORTED_DOCUMENT_TYPES
    assert ".docx" in SUPPORTED_DOCUMENT_TYPES
    assert ".xlsx" in SUPPORTED_DOCUMENT_TYPES
    assert ".pptx" in SUPPORTED_DOCUMENT_TYPES
    # MIME values are non-empty
    for v in SUPPORTED_DOCUMENT_TYPES.values():
        assert "/" in v


def test_supported_video_types_set():
    assert {".mp4", ".mov", ".webm", ".mkv", ".avi"}.issubset(SUPPORTED_VIDEO_TYPES)
```

- [ ] **Step 1.2.3: Run tests, verify import error**

```bash
pytest tests/test_channel_utils.py -v
```

Expected: All FAIL (`cannot import`).

- [ ] **Step 1.2.4: Implement channel_utils.py**

```python
# plugin_sdk/channel_utils.py
"""Channel-utility functions: UTF-16 budgeting, smart truncation, type registries.

Ported from gateway/platforms/base.py in Hermes Agent (2026.4.23).
"""
from __future__ import annotations

import re
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# UTF-16 length math (Telegram measures 4096 limit in UTF-16 code units, not codepoints)
# ---------------------------------------------------------------------------

def utf16_len(s: str) -> int:
    """Number of UTF-16 code units in s.

    Surrogate pairs (emoji, etc.) count as 2.
    """
    if not s:
        return 0
    return len(s.encode("utf-16-le")) // 2


def _prefix_within_utf16_limit(s: str, budget: int) -> int:
    """Largest codepoint-prefix length whose UTF-16 length is <= budget."""
    if utf16_len(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if utf16_len(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


# ---------------------------------------------------------------------------
# Smart truncation (code-fence-aware, UTF-16-aware via len_fn)
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"```([a-zA-Z0-9_+-]*)$", re.MULTILINE)
_FENCE_CLOSE_LITERAL = "```"


def truncate_message_smart(
    content: str,
    max_length: int = 4096,
    len_fn: Optional[Callable[[str], int]] = None,
) -> list[str]:
    """Split content into chunks of <= max_length each, preserving code fences.

    Behaviour:
    - If a chunk boundary falls inside a fenced code block, reopen ```lang
      at the start of the next chunk so syntax highlighting survives.
    - Inline code spans (`...`) are not split mid-span.
    - Multi-chunk output gets " (i/N)" indicator appended (chunk index +
      total). For single chunks, no indicator.
    - len_fn lets callers swap codepoint length for UTF-16 length
      (Telegram) or any other custom unit. Default: codepoint count.

    Returns:
        Non-empty list of strings, each within budget.
    """
    if len_fn is None:
        len_fn = len
    if not content:
        return [""]
    if len_fn(content) <= max_length:
        return [content]

    # Reserve indicator overhead. " (NN/NN)" worst case ~10 chars.
    indicator_overhead = 10
    budget = max(1, max_length - indicator_overhead)

    chunks: list[str] = []
    remaining = content
    open_fence_lang: Optional[str] = None

    while remaining:
        if len_fn(remaining) <= budget:
            chunk = remaining
            remaining = ""
        else:
            # Find a clean break before budget: prefer line break, then
            # space, then hard break.
            cut = _find_clean_break(remaining, budget, len_fn)
            chunk = remaining[:cut]
            remaining = remaining[cut:].lstrip("\n")

        # Reopen lang if last chunk had unclosed fence
        if open_fence_lang is not None:
            chunk = f"```{open_fence_lang}\n" + chunk

        # Detect open fence at end of THIS chunk: count ``` lines
        fence_count = chunk.count(_FENCE_CLOSE_LITERAL)
        if fence_count % 2 == 1:
            # We have an unclosed fence — close it and remember lang
            m = _FENCE_OPEN_RE.search(chunk)
            open_fence_lang = m.group(1) if m else ""
            chunk = chunk + "\n" + _FENCE_CLOSE_LITERAL
        else:
            open_fence_lang = None

        chunks.append(chunk)

    if len(chunks) == 1:
        return chunks
    n = len(chunks)
    return [f"{c} ({i+1}/{n})" for i, c in enumerate(chunks)]


def _find_clean_break(text: str, budget: int, len_fn: Callable[[str], int]) -> int:
    """Largest cut index <= budget that prefers a newline boundary."""
    # Find the largest index whose prefix fits the budget
    if len_fn == len:
        max_idx = budget
    else:
        # Generic budget — find by walk
        max_idx = _prefix_within_utf16_limit(text, budget)
    if max_idx >= len(text):
        return len(text)
    # Prefer last newline before max_idx
    nl = text.rfind("\n", 0, max_idx + 1)
    if nl > max_idx // 2:  # don't break too early
        return nl + 1
    sp = text.rfind(" ", 0, max_idx + 1)
    if sp > max_idx // 2:
        return sp + 1
    return max_idx


# ---------------------------------------------------------------------------
# Document / video type registries
# ---------------------------------------------------------------------------

SUPPORTED_DOCUMENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

SUPPORTED_VIDEO_TYPES: frozenset[str] = frozenset({
    ".mp4",
    ".mov",
    ".webm",
    ".mkv",
    ".avi",
})


__all__ = [
    "utf16_len",
    "_prefix_within_utf16_limit",
    "truncate_message_smart",
    "SUPPORTED_DOCUMENT_TYPES",
    "SUPPORTED_VIDEO_TYPES",
]
```

- [ ] **Step 1.2.5: Run all channel_utils tests, verify pass**

```bash
pytest tests/test_channel_utils.py -v
```

Expected: ~13 PASS.

- [ ] **Step 1.2.6: Lint + commit**

```bash
ruff check plugin_sdk/channel_utils.py tests/test_channel_utils.py plugin_sdk/core.py
git add plugin_sdk/channel_utils.py tests/test_channel_utils.py plugin_sdk/core.py
git commit -m "feat(plugin_sdk): add channel_utils + ProcessingOutcome enum

UTF-16 budgeting (telegram measures 4096 in UTF-16 units), smart
truncation that reopens code fences across chunks, document/video MIME
registries shared across adapters. ProcessingOutcome enum for
on_processing_complete lifecycle hook (added in PR 2).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 1.3: `plugin_sdk/network_utils.py`

**Files:**
- Create: `plugin_sdk/network_utils.py`
- Test: `tests/test_network_utils.py`

- [ ] **Step 1.3.1: Write failing tests**

```python
# tests/test_network_utils.py
import pytest
from plugin_sdk.network_utils import (
    _looks_like_image,
    safe_url_for_log,
    is_network_accessible,
    resolve_proxy_url,
)


def test_looks_like_image_png():
    assert _looks_like_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)


def test_looks_like_image_jpeg():
    assert _looks_like_image(b"\xff\xd8\xff\xe0" + b"\x00" * 16)


def test_looks_like_image_gif():
    assert _looks_like_image(b"GIF89a" + b"\x00" * 16)


def test_looks_like_image_webp():
    assert _looks_like_image(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8)


def test_looks_like_image_rejects_html():
    assert not _looks_like_image(b"<!DOCTYPE html>...")


def test_looks_like_image_rejects_empty():
    assert not _looks_like_image(b"")


def test_looks_like_image_rejects_short():
    assert not _looks_like_image(b"\x89PNG")  # truncated


@pytest.mark.parametrize("url,expected", [
    ("https://user:pass@example.com/path?q=1#frag", "https://example.com/path"),
    ("http://example.com/foo", "http://example.com/foo"),
    ("https://very.long.host/" + "x" * 500, "https://very.long.host/" + "x" * 177),  # capped
    ("not a url", "not a url"),
])
def test_safe_url_for_log(url, expected):
    out = safe_url_for_log(url, max_len=200)
    assert len(out) <= 200
    if expected.endswith("xxx"):
        assert out.startswith(expected.split("xxx")[0])
    else:
        assert out == expected[:200]


def test_is_network_accessible_loopback_rejected():
    assert is_network_accessible("127.0.0.1") is False
    assert is_network_accessible("localhost") is False
    assert is_network_accessible("[::1]") is False


def test_resolve_proxy_url_env_priority(monkeypatch):
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://global:8080")
    monkeypatch.setenv("TELEGRAM_PROXY", "http://specific:8080")
    assert resolve_proxy_url("TELEGRAM_PROXY") == "http://specific:8080"
    monkeypatch.delenv("TELEGRAM_PROXY")
    assert resolve_proxy_url("TELEGRAM_PROXY") == "http://global:8080"
```

- [ ] **Step 1.3.2: Implement network_utils.py**

```python
# plugin_sdk/network_utils.py
"""Network-utility functions: SSRF guard, magic-byte sniff, proxy resolution.

Ported from gateway/platforms/base.py (Hermes 2026.4.23) with adaptations.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import socket
import subprocess
import sys
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger("plugin_sdk.network_utils")

# Magic-byte signatures for common image formats
_IMAGE_MAGIC_BYTES: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
]
_WEBP_HEADER = (b"RIFF", 4, b"WEBP")  # bytes 0-3 = RIFF, bytes 8-11 = WEBP


def _looks_like_image(data: bytes) -> bool:
    """Magic-byte check: is this likely an image (not HTML masquerading as one)?"""
    if not data or len(data) < 8:
        return False
    for prefix, _ in _IMAGE_MAGIC_BYTES:
        if data.startswith(prefix):
            return True
    if (
        data[:4] == _WEBP_HEADER[0]
        and len(data) >= 12
        and data[8:12] == _WEBP_HEADER[2]
    ):
        return True
    return False


def safe_url_for_log(url: str, max_len: int = 200) -> str:
    """Strip userinfo/query/fragment for safe logging; truncate to max_len."""
    try:
        parsed = urlparse(url)
        if parsed.scheme and parsed.netloc:
            # Strip userinfo if present
            netloc = parsed.netloc.rsplit("@", 1)[-1]
            sanitized = urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))
            return sanitized[:max_len]
    except (ValueError, AttributeError):
        pass
    return url[:max_len]


def is_network_accessible(host: str) -> bool:
    """Return False for loopback/private/link-local; True for routable hosts.

    Fail-closed on DNS resolution failure (returns False) — better to refuse
    than risk SSRF against an unknown host.
    """
    if not host:
        return False
    # Strip brackets from IPv6
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host.lower() == "localhost":
        return False
    # Try to parse as IP first
    try:
        addr = ipaddress.ip_address(host)
        return not (addr.is_loopback or addr.is_private or addr.is_link_local
                    or addr.is_multicast or addr.is_reserved or addr.is_unspecified)
    except ValueError:
        pass
    # Resolve host
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return False
    for fam, _type, _proto, _canon, sockaddr in infos:
        ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip)
            if (addr.is_loopback or addr.is_private or addr.is_link_local
                    or addr.is_multicast or addr.is_reserved or addr.is_unspecified):
                return False
        except ValueError:
            return False
    return True


def _detect_macos_system_proxy() -> Optional[str]:
    """Read macOS system proxy via scutil --proxy."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.check_output(
            ["scutil", "--proxy"], stderr=subprocess.DEVNULL, timeout=2
        ).decode("utf-8", errors="ignore")
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return None
    https_enabled = re.search(r"HTTPSEnable\s*:\s*1", out)
    if not https_enabled:
        return None
    proxy_match = re.search(r"HTTPSProxy\s*:\s*(\S+)", out)
    port_match = re.search(r"HTTPSPort\s*:\s*(\d+)", out)
    if not (proxy_match and port_match):
        return None
    return f"http://{proxy_match.group(1)}:{port_match.group(1)}"


def resolve_proxy_url(env_var: Optional[str] = None) -> Optional[str]:
    """Resolve effective proxy URL.

    Priority:
    1. Per-platform env var (e.g. TELEGRAM_PROXY)
    2. HTTPS_PROXY / https_proxy
    3. HTTP_PROXY / http_proxy
    4. ALL_PROXY / all_proxy
    5. macOS system proxy via scutil (Darwin only)
    Returns None if nothing configured.
    """
    if env_var:
        v = os.environ.get(env_var)
        if v:
            return v
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        v = os.environ.get(k)
        if v:
            return v
    return _detect_macos_system_proxy()


def proxy_kwargs_for_aiohttp(url: Optional[str]) -> dict[str, Any]:
    """Build kwargs for aiohttp.ClientSession to use proxy_url.

    HTTP/HTTPS: returns ``{"proxy": url}``.
    SOCKS: returns ``{"connector": ProxyConnector.from_url(url, rdns=True)}``
    if ``aiohttp_socks`` is installed; otherwise WARN and return ``{}``.
    """
    if not url:
        return {}
    if url.startswith(("http://", "https://")):
        return {"proxy": url}
    if url.startswith(("socks://", "socks4://", "socks5://", "socks5h://")):
        try:
            from aiohttp_socks import ProxyConnector  # type: ignore[import-not-found]

            return {"connector": ProxyConnector.from_url(url, rdns=True)}
        except ImportError:
            logger.warning("SOCKS proxy requested but aiohttp_socks not installed; ignoring")
            return {}
    return {}


def proxy_kwargs_for_bot(url: Optional[str]) -> dict[str, Any]:
    """Build kwargs for python-telegram-bot/discord.py Bot constructors."""
    if not url:
        return {}
    if url.startswith(("http://", "https://")):
        return {"proxy": url}
    if url.startswith(("socks://", "socks4://", "socks5://", "socks5h://")):
        try:
            from aiohttp_socks import ProxyConnector  # type: ignore[import-not-found]

            return {"connector": ProxyConnector.from_url(url, rdns=True)}
        except ImportError:
            logger.warning("SOCKS proxy requested but aiohttp_socks not installed; ignoring")
            return {}
    return {}


async def _ssrf_redirect_guard(response: Any) -> None:
    """httpx async response hook: re-validate each redirect target.

    Usage:
        client = httpx.AsyncClient(event_hooks={"response": [_ssrf_redirect_guard]})
    """
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("location")
        if location:
            from urllib.parse import urlparse as _up
            parsed = _up(location)
            host = parsed.hostname
            if host and not is_network_accessible(host):
                raise RuntimeError(f"SSRF guard: refused redirect to private host {host!r}")


__all__ = [
    "_looks_like_image",
    "safe_url_for_log",
    "is_network_accessible",
    "resolve_proxy_url",
    "proxy_kwargs_for_aiohttp",
    "proxy_kwargs_for_bot",
    "_ssrf_redirect_guard",
]
```

- [ ] **Step 1.3.3: Run tests, verify pass**

```bash
pytest tests/test_network_utils.py -v
```

Expected: ~13 PASS.

- [ ] **Step 1.3.4: Lint + commit**

```bash
ruff check plugin_sdk/network_utils.py tests/test_network_utils.py
git add plugin_sdk/network_utils.py tests/test_network_utils.py
git commit -m "feat(plugin_sdk): add network_utils (SSRF guard, magic-byte sniff, proxy resolution)

Pure stdlib + optional aiohttp_socks for SOCKS rDNS support.
Hermes parity (gateway/platforms/base.py) with macOS scutil proxy fallback.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 1.4: `plugin_sdk/format_converters/markdownv2.py`

**Files:**
- Create: `plugin_sdk/format_converters/__init__.py`
- Create: `plugin_sdk/format_converters/markdownv2.py`
- Test: `tests/test_format_converters.py`

- [ ] **Step 1.4.1: Write failing tests for MarkdownV2**

```python
# tests/test_format_converters.py
import pytest
from plugin_sdk.format_converters.markdownv2 import convert as to_mdv2, escape_mdv2


def test_mdv2_escape_basic_chars():
    s = "1.5 (rate)"
    out = escape_mdv2(s)
    assert out == r"1\.5 \(rate\)"


def test_mdv2_convert_bold():
    assert to_mdv2("**bold**") == "*bold*"


def test_mdv2_convert_italic_star():
    assert to_mdv2("*italic*") == "_italic_"


def test_mdv2_convert_code_fence_preserved():
    src = "```python\nx = 1\n```"
    out = to_mdv2(src)
    assert "```python\nx = 1\n```" in out  # untouched


def test_mdv2_convert_inline_code_preserved():
    src = "use `x = 1` here"
    out = to_mdv2(src)
    assert "`x = 1`" in out


def test_mdv2_convert_link_format():
    src = "[label](https://example.com)"
    out = to_mdv2(src)
    assert "[label](https://example.com)" in out


def test_mdv2_convert_strikethrough():
    src = "~~strike~~"
    assert to_mdv2(src) == "~strike~"


def test_mdv2_convert_blockquote():
    src = "> quoted"
    assert to_mdv2(src).startswith(">")


def test_mdv2_convert_special_chars_escaped_outside_code():
    src = "Hello, world! (1+1=2)"
    out = to_mdv2(src)
    # Telegram MarkdownV2 special chars: _*[]()~`>#+-=|{}.!
    assert "\\!" in out or "!" not in out  # depends on impl
    assert "\\(" in out
```

- [ ] **Step 1.4.2: Implement markdownv2.py**

```python
# plugin_sdk/format_converters/__init__.py
"""Per-platform format converters.

Each module exports ``convert(text: str) -> str`` and may export helpers.
All converters fall back to plain text on parse error (never raise).
"""
```

```python
# plugin_sdk/format_converters/markdownv2.py
"""Markdown -> Telegram MarkdownV2 converter.

Telegram's MarkdownV2 grammar requires escaping a wide set of special
characters (`_*[]()~\`>#+-=|{}.!\\`) wherever they appear OUTSIDE of code
spans. This converter:

1. Protects fenced code blocks and inline code via placeholder substitution
2. Converts markdown formatting (`**bold**` -> `*bold*`, `*ital*` -> `_ital_`)
3. Escapes remaining special chars in the cleaned-of-code text
4. Restores code blocks and inline code unchanged
5. Falls back to escaping plain text if any pattern fails

Ported from Hermes telegram.py:_escape_mdv2 / format_message.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("plugin_sdk.format_converters.markdownv2")

# MarkdownV2 special chars that MUST be escaped outside code spans
_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!\\"
_MDV2_ESCAPE_RE = re.compile(f"([{re.escape(_MDV2_SPECIAL)}])")

# Placeholder format: \x00CODEn\x00 (NUL won't appear in user-input text)
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*\n.*?\n)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_\n]+)__")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)$", re.MULTILINE)


def escape_mdv2(text: str) -> str:
    """Backslash-escape every MarkdownV2 special character."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def convert(text: str) -> str:
    """Convert plain markdown to Telegram MarkdownV2.

    Falls back to fully-escaped plain text if anything goes wrong.
    """
    try:
        return _convert_unsafe(text)
    except Exception:  # noqa: BLE001
        logger.warning("MarkdownV2 conversion failed; falling back to plain", exc_info=True)
        return escape_mdv2(text)


def _convert_unsafe(text: str) -> str:
    if not text:
        return ""
    placeholders: list[str] = []

    def stash(match: re.Match[str], wrap: str) -> str:
        idx = len(placeholders)
        placeholders.append(f"{wrap[0]}{match.group(0)[len(wrap[0]):-len(wrap[1])]}{wrap[1]}")
        return f"\x00P{idx}\x00"

    # 1. Stash fenced code (preserve fully)
    def stash_fence(m: re.Match[str]) -> str:
        idx = len(placeholders)
        placeholders.append(m.group(0))
        return f"\x00P{idx}\x00"

    text = _FENCE_RE.sub(stash_fence, text)

    # 2. Stash inline code
    def stash_inline(m: re.Match[str]) -> str:
        idx = len(placeholders)
        placeholders.append(f"`{m.group(1)}`")
        return f"\x00P{idx}\x00"

    text = _INLINE_CODE_RE.sub(stash_inline, text)

    # 3. Stash links
    def stash_link(m: re.Match[str]) -> str:
        idx = len(placeholders)
        label = escape_mdv2(m.group(1))
        url = m.group(2).replace(")", r"\)").replace("\\", r"\\")
        placeholders.append(f"[{label}]({url})")
        return f"\x00P{idx}\x00"

    text = _LINK_RE.sub(stash_link, text)

    # 4. Convert formatting BEFORE escaping (so `**` doesn't get escaped first)
    text = _BOLD_DOUBLE_RE.sub(lambda m: f"\x01B{escape_mdv2(m.group(1))}\x01B", text)
    text = _BOLD_UNDER_RE.sub(lambda m: f"\x01B{escape_mdv2(m.group(1))}\x01B", text)
    text = _STRIKE_RE.sub(lambda m: f"\x01S{escape_mdv2(m.group(1))}\x01S", text)
    # Single-asterisk italic: only when surrounded by non-asterisk
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", lambda m: f"\x01I{escape_mdv2(m.group(1))}\x01I", text)
    # Single-underscore italic
    text = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", lambda m: f"\x01I{escape_mdv2(m.group(1))}\x01I", text)
    # Headings -> bold
    text = _HEADING_RE.sub(lambda m: f"\x01B{escape_mdv2(m.group(2))}\x01B", text)

    # 5. Escape ALL remaining special chars in non-marker text
    # Split on placeholders so we don't escape them
    parts = re.split(r"(\x00P\d+\x00|\x01[BIS])", text)
    out: list[str] = []
    for p in parts:
        if p.startswith("\x00P") or p.startswith("\x01"):
            out.append(p)
        else:
            out.append(escape_mdv2(p))
    text = "".join(out)

    # 6. Replace formatting markers with MarkdownV2 syntax
    text = text.replace("\x01B", "*").replace("\x01I", "_").replace("\x01S", "~")

    # 7. Restore placeholders
    def restore(m: re.Match[str]) -> str:
        idx = int(m.group(1))
        return placeholders[idx]

    text = re.sub(r"\x00P(\d+)\x00", restore, text)
    return text


__all__ = ["convert", "escape_mdv2"]
```

- [ ] **Step 1.4.3: Run tests, verify pass; commit**

```bash
pytest tests/test_format_converters.py -v -k mdv2
ruff check plugin_sdk/format_converters/
git add plugin_sdk/format_converters/__init__.py plugin_sdk/format_converters/markdownv2.py tests/test_format_converters.py
git commit -m "feat(plugin_sdk): markdownv2 converter

Markdown -> Telegram MarkdownV2 with fenced-code/link/inline-code
preservation. Falls back to fully-escaped plain text on parse error.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 1.5: format_converters/slack_mrkdwn.py + matrix_html.py + whatsapp_format.py

**Files:**
- Create: `plugin_sdk/format_converters/slack_mrkdwn.py`
- Create: `plugin_sdk/format_converters/matrix_html.py`
- Create: `plugin_sdk/format_converters/whatsapp_format.py`
- Test: `tests/test_format_converters.py` (extend)

- [ ] **Step 1.5.1: Write failing tests for slack_mrkdwn**

```python
# Append to tests/test_format_converters.py
from plugin_sdk.format_converters.slack_mrkdwn import convert as to_mrkdwn


def test_mrkdwn_link_conversion():
    assert to_mrkdwn("[label](https://example.com)") == "<https://example.com|label>"


def test_mrkdwn_bold_double_to_single():
    assert to_mrkdwn("**bold**") == "*bold*"


def test_mrkdwn_italic_star_to_underscore():
    # Single * is bold in Slack, so * -> _ for italic
    assert to_mrkdwn("*italic*") == "_italic_"


def test_mrkdwn_strike():
    assert to_mrkdwn("~~strike~~") == "~strike~"


def test_mrkdwn_heading_to_bold():
    assert to_mrkdwn("# Heading").strip() == "*Heading*"


def test_mrkdwn_escape_amp_lt_gt():
    assert "&amp;" in to_mrkdwn("foo & bar")
    assert "&lt;" in to_mrkdwn("a < b")
    assert "&gt;" in to_mrkdwn("a > b")


def test_mrkdwn_code_fence_preserved():
    assert "```python" in to_mrkdwn("```python\nx = 1\n```")


def test_mrkdwn_no_double_escape():
    # &amp; should not become &amp;amp;
    assert to_mrkdwn("&amp;") == "&amp;"
```

- [ ] **Step 1.5.2: Implement slack_mrkdwn.py**

```python
# plugin_sdk/format_converters/slack_mrkdwn.py
"""Markdown -> Slack mrkdwn converter.

Ported from gateway/platforms/slack.py:format_message in Hermes.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("plugin_sdk.format_converters.slack_mrkdwn")

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*\n.*?\n)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)$", re.MULTILINE)


def convert(text: str) -> str:
    """Convert markdown to Slack mrkdwn. Plain-text fallback on error."""
    try:
        return _convert_unsafe(text)
    except Exception:  # noqa: BLE001
        logger.warning("mrkdwn conversion failed; returning plain text", exc_info=True)
        return text


def _convert_unsafe(text: str) -> str:
    if not text:
        return ""
    placeholders: list[str] = []

    def stash(content: str) -> str:
        placeholders.append(content)
        return f"\x00P{len(placeholders) - 1}\x00"

    # Stash fenced code + inline code unchanged
    text = _FENCE_RE.sub(lambda m: stash(m.group(0)), text)
    text = _INLINE_CODE_RE.sub(lambda m: stash(f"`{m.group(1)}`"), text)

    # Escape & < > BEFORE other transforms (avoid double-escape: skip if already &amp;-form)
    text = re.sub(r"&(?!(amp|lt|gt|quot|apos);)", "&amp;", text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")

    # **bold** -> *bold*
    text = _BOLD_DOUBLE_RE.sub(r"*\1*", text)
    # ~~strike~~ -> ~strike~
    text = _STRIKE_RE.sub(r"~\1~", text)
    # # Heading -> *Heading*
    text = _HEADING_RE.sub(r"*\2*", text)
    # Single *italic* -> _italic_ (because Slack uses * for bold)
    # NOTE: order matters — must run AFTER ** -> *
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"_\1_", text)

    # Convert links: [label](url) -> <url|label>
    text = _LINK_RE.sub(lambda m: stash(f"<{m.group(2)}|{m.group(1)}>"), text)

    # Restore placeholders
    def restore(m: re.Match[str]) -> str:
        return placeholders[int(m.group(1))]

    text = re.sub(r"\x00P(\d+)\x00", restore, text)
    return text


__all__ = ["convert"]
```

- [ ] **Step 1.5.3: Implement matrix_html.py**

```python
# plugin_sdk/format_converters/matrix_html.py
"""Markdown -> Matrix HTML converter.

Outputs ``org.matrix.custom.html`` body content. Uses the ``markdown``
library if available; falls back to a regex converter otherwise.

Ported from gateway/platforms/matrix.py:_markdown_to_html in Hermes.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("plugin_sdk.format_converters.matrix_html")

try:
    import markdown as _md_lib  # type: ignore[import-not-found]
    _HAVE_MD_LIB = True
except ImportError:
    _HAVE_MD_LIB = False

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*\n.*?\n)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)$", re.MULTILINE)


def _sanitize_url(url: str) -> str:
    """Reject javascript: / data: schemes; allow http(s)/mailto/matrix:."""
    lo = url.lower().strip()
    if lo.startswith(("http://", "https://", "mailto:", "matrix:")):
        return url.replace('"', "&quot;")
    return ""


def convert(text: str) -> str:
    """Convert markdown to Matrix HTML body. Plain-text on failure."""
    try:
        return _convert_unsafe(text)
    except Exception:  # noqa: BLE001
        logger.warning("matrix_html conversion failed; returning plain", exc_info=True)
        return text


def _convert_unsafe(text: str) -> str:
    if not text:
        return ""
    if _HAVE_MD_LIB:
        try:
            html = _md_lib.markdown(text, extensions=["fenced_code", "tables"])
            return html
        except Exception:  # noqa: BLE001
            pass  # fall through to regex
    return _regex_to_html(text)


def _regex_to_html(text: str) -> str:
    # Stash code first
    placeholders: list[str] = []

    def stash(content: str) -> str:
        placeholders.append(content)
        return f"\x00P{len(placeholders) - 1}\x00"

    text = _FENCE_RE.sub(
        lambda m: stash(f"<pre><code>{_html_escape(m.group(1))}</code></pre>"), text
    )
    text = _INLINE_CODE_RE.sub(
        lambda m: stash(f"<code>{_html_escape(m.group(1))}</code>"), text
    )

    text = _html_escape(text)
    # Re-introduce safe HTML for our converted markdown
    text = _BOLD_DOUBLE_RE.sub(r"<strong>\1</strong>", text)
    text = _ITALIC_STAR_RE.sub(r"<em>\1</em>", text)
    text = _STRIKE_RE.sub(r"<del>\1</del>", text)
    text = _HEADING_RE.sub(lambda m: f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>", text)

    def link_replace(m: re.Match[str]) -> str:
        url = _sanitize_url(m.group(2))
        if not url:
            return m.group(1)
        return f'<a href="{url}">{m.group(1)}</a>'

    text = _LINK_RE.sub(link_replace, text)

    def restore(m: re.Match[str]) -> str:
        return placeholders[int(m.group(1))]

    text = re.sub(r"\x00P(\d+)\x00", restore, text)
    return text


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&#39;")
    )


__all__ = ["convert"]
```

- [ ] **Step 1.5.4: Implement whatsapp_format.py**

```python
# plugin_sdk/format_converters/whatsapp_format.py
"""Markdown -> WhatsApp syntax converter.

WhatsApp accepts: *bold*, _italic_, ~strike~, ```code```. Headers/links
are flattened (no native support). Code-fence + inline-code preserved.

Ported from gateway/platforms/whatsapp.py:format_message in Hermes.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger("plugin_sdk.format_converters.whatsapp_format")

_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+-]*\n.*?\n)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_DOUBLE_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_BOLD_UNDER_RE = re.compile(r"__([^_\n]+)__")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)$", re.MULTILINE)


def convert(text: str) -> str:
    try:
        return _convert_unsafe(text)
    except Exception:  # noqa: BLE001
        logger.warning("whatsapp_format conversion failed; returning plain", exc_info=True)
        return text


def _convert_unsafe(text: str) -> str:
    if not text:
        return ""
    placeholders: list[str] = []

    def stash(content: str) -> str:
        placeholders.append(content)
        return f"\x00P{len(placeholders) - 1}\x00"

    text = _FENCE_RE.sub(lambda m: stash(m.group(0)), text)
    text = _INLINE_CODE_RE.sub(lambda m: stash(f"`{m.group(1)}`"), text)

    # **bold** -> *bold*; __bold__ -> *bold*
    text = _BOLD_DOUBLE_RE.sub(r"*\1*", text)
    text = _BOLD_UNDER_RE.sub(r"*\1*", text)
    # ~~strike~~ -> ~strike~
    text = _STRIKE_RE.sub(r"~\1~", text)
    # # Heading -> *Heading*
    text = _HEADING_RE.sub(r"*\2*", text)
    # [label](url) -> label (url)
    text = _LINK_RE.sub(r"\1 (\2)", text)

    def restore(m: re.Match[str]) -> str:
        return placeholders[int(m.group(1))]

    text = re.sub(r"\x00P(\d+)\x00", restore, text)
    return text


__all__ = ["convert"]
```

- [ ] **Step 1.5.5: Add tests for matrix_html and whatsapp_format**

```python
# Append to tests/test_format_converters.py
from plugin_sdk.format_converters.matrix_html import convert as to_html
from plugin_sdk.format_converters.whatsapp_format import convert as to_whatsapp


def test_matrix_html_bold():
    out = to_html("**bold**")
    assert "<strong>bold</strong>" in out


def test_matrix_html_link_safe_scheme():
    out = to_html("[label](https://example.com)")
    assert '<a href="https://example.com">label</a>' in out


def test_matrix_html_link_javascript_rejected():
    out = to_html("[evil](javascript:alert(1))")
    assert "<a" not in out
    assert "evil" in out


def test_matrix_html_escape_lt_gt():
    out = to_html("a < b > c")
    assert "&lt;" in out and "&gt;" in out


def test_whatsapp_bold_double_to_single():
    assert to_whatsapp("**bold**") == "*bold*"


def test_whatsapp_strike_double_to_single():
    assert to_whatsapp("~~strike~~") == "~strike~"


def test_whatsapp_heading_to_bold():
    assert to_whatsapp("# Hello").strip() == "*Hello*"


def test_whatsapp_link_inline():
    assert to_whatsapp("[click](https://x.com)") == "click (https://x.com)"


def test_whatsapp_code_fence_preserved():
    src = "```python\nx = 1\n```"
    assert "```python" in to_whatsapp(src)
```

- [ ] **Step 1.5.6: Run all format_converter tests + commit**

```bash
pytest tests/test_format_converters.py -v
ruff check plugin_sdk/format_converters/
git add plugin_sdk/format_converters/ tests/test_format_converters.py
git commit -m "feat(plugin_sdk): slack_mrkdwn, matrix_html, whatsapp_format converters

Each: pure convert(text)->str with plain-text fallback on parse error.
Code-fence + inline-code preservation. URL sanitization in matrix_html.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 1.6: PR 1 final checks + push

- [ ] **Step 1.6.1: Run full test suite**

```bash
pytest tests/ -q --tb=line 2>&1 | tail -10
```

Expected: `885 + ~50 new = 935 passed` (or similar). All green.

- [ ] **Step 1.6.2: Run plugin_sdk boundary test**

```bash
pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer -v
```

Expected: PASS. Confirms no new module pulls from `opencomputer/*`.

- [ ] **Step 1.6.3: Verify ruff clean across changed files**

```bash
ruff check plugin_sdk/ tests/test_channel_helpers.py tests/test_channel_utils.py tests/test_network_utils.py tests/test_format_converters.py
```

Expected: All checks passed.

- [ ] **Step 1.6.4: Push branch + open draft PR**

```bash
git push -u origin feat/hermes-channel-feature-port
gh pr create --draft --title "feat: hermes channel feature port — PR 1 (foundation modules)" \
  --body "$(cat <<'EOF'
## Summary
- Adds plugin_sdk/channel_helpers (MessageDeduplicator, TextBatchAggregator, strip_markdown, redact_phone, ThreadParticipationTracker)
- Adds plugin_sdk/channel_utils (utf16_len, truncate_message_smart, document/video MIME registries)
- Adds plugin_sdk/network_utils (SSRF guard, magic-byte sniff, proxy resolution)
- Adds plugin_sdk/format_converters (markdownv2, slack_mrkdwn, matrix_html, whatsapp_format)
- Adds ProcessingOutcome enum to plugin_sdk/core
- ~50 new tests, ruff clean, plugin_sdk boundary preserved

## Spec
docs/superpowers/specs/2026-04-28-hermes-channel-feature-port-design.md

## Test plan
- [ ] pytest tests/ -q passes (~935 total)
- [ ] ruff check clean
- [ ] test_plugin_sdk_does_not_import_opencomputer still PASS

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

# PR 2 — BaseChannelAdapter enhancements + Dispatch wiring

**Goal:** Add `_send_with_retry`, reaction lifecycle hooks, fatal-error reporting, agent-output-extraction methods to `BaseChannelAdapter`. Wire reaction lifecycle and photo-burst merging into `Dispatch`.

**Risk:** Medium. Touches plugin_sdk contract and gateway core. Mitigation: defaults are no-ops; existing adapters unaffected.

**Estimated:** 1.5 days.

---

## Task 2.1: BaseChannelAdapter `_send_with_retry`

**Files:**
- Modify: `plugin_sdk/channel_contract.py`
- Test: `tests/test_send_with_retry.py`

- [ ] **Step 2.1.1: Write failing tests**

```python
# tests/test_send_with_retry.py
import asyncio
import pytest
from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Platform, SendResult


class _FakeAdapter(BaseChannelAdapter):
    platform = Platform.CLI
    async def connect(self): return True
    async def disconnect(self): pass
    async def send(self, chat_id, text, **kwargs): return SendResult(success=True)


@pytest.mark.asyncio
async def test_send_with_retry_first_try_success():
    adapter = _FakeAdapter({})
    calls = []
    async def fn(*a, **kw):
        calls.append(1)
        return SendResult(success=True)
    res = await adapter._send_with_retry(fn, "chat", "text")
    assert res.success and len(calls) == 1


@pytest.mark.asyncio
async def test_send_with_retry_retries_on_retryable():
    adapter = _FakeAdapter({})
    calls = []
    async def fn(*a, **kw):
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("connection reset by peer")
        return SendResult(success=True)
    res = await adapter._send_with_retry(fn, "chat", "text", base_delay=0.01)
    assert res.success and len(calls) == 3


@pytest.mark.asyncio
async def test_send_with_retry_does_not_retry_timeout():
    adapter = _FakeAdapter({})
    calls = []
    async def fn(*a, **kw):
        calls.append(1)
        raise TimeoutError("read timed out")
    with pytest.raises(TimeoutError):
        await adapter._send_with_retry(fn, "chat", "text", base_delay=0.01)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_send_with_retry_exhausts_returns_failure_result():
    adapter = _FakeAdapter({})
    async def fn(*a, **kw):
        raise ConnectionError("network")
    res = await adapter._send_with_retry(fn, "chat", "text", max_attempts=3, base_delay=0.01)
    assert not res.success
    assert "network" in (res.error or "").lower()


@pytest.mark.asyncio
async def test_is_retryable_error_classes():
    adapter = _FakeAdapter({})
    assert adapter._is_retryable_error(ConnectionError("connection reset"))
    assert adapter._is_retryable_error(OSError("network unreachable"))
    assert not adapter._is_retryable_error(ValueError("bad input"))
```

- [ ] **Step 2.1.2: Implement on BaseChannelAdapter**

Edit `plugin_sdk/channel_contract.py` — add to BaseChannelAdapter class:

```python
# Append imports at top
import asyncio
import logging
import random
from .core import SendResult

logger = logging.getLogger("plugin_sdk.channel_contract")

# Append class constants on BaseChannelAdapter
_RETRYABLE_ERROR_PATTERNS: tuple[str, ...] = (
    "connecterror", "connectionerror", "connectionreset", "connectionrefused",
    "connecttimeout", "network", "broken pipe", "remotedisconnected", "eoferror",
)

# Append methods on BaseChannelAdapter class
def _is_retryable_error(self, exc: BaseException) -> bool:
    """Heuristic: is exc transient and worth retrying?

    Read/write timeouts excluded — non-idempotent.
    """
    cls = type(exc).__name__.lower()
    if "timeout" in cls and "connect" not in cls:
        return False
    if any(p in cls for p in self._RETRYABLE_ERROR_PATTERNS):
        return True
    msg = str(exc).lower()
    return any(p in msg for p in self._RETRYABLE_ERROR_PATTERNS)


async def _send_with_retry(
    self,
    send_fn,
    *args,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    **kwargs,
):
    """Retry transient send failures with exponential backoff + jitter.

    Returns the function's SendResult on success, or a failure SendResult
    on exhaustion. Non-retryable exceptions propagate.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return await send_fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001
            if not self._is_retryable_error(exc):
                raise
            last_exc = exc
            if attempt + 1 >= max_attempts:
                break
            delay = base_delay * (2 ** attempt) + random.uniform(0, base_delay * 0.25)
            logger.warning(
                "send retry %d/%d after %s: %s",
                attempt + 1, max_attempts, type(exc).__name__, str(exc)[:200],
            )
            await asyncio.sleep(delay)
    err = f"{type(last_exc).__name__ if last_exc else 'Unknown'}: {str(last_exc)[:300] if last_exc else 'no exc'}"
    return SendResult(success=False, error=err)
```

- [ ] **Step 2.1.3: Run tests + commit**

```bash
pytest tests/test_send_with_retry.py -v
ruff check plugin_sdk/channel_contract.py tests/test_send_with_retry.py
git add plugin_sdk/channel_contract.py tests/test_send_with_retry.py
git commit -m "feat(plugin_sdk): BaseChannelAdapter._send_with_retry

Exponential backoff with jitter for transient send errors. Non-retryable
errors (timeouts, validation) propagate immediately. Hermes parity.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2.2: Reaction lifecycle hooks (`on_processing_start` / `on_processing_complete`)

**Files:**
- Modify: `plugin_sdk/channel_contract.py`
- Modify: `opencomputer/gateway/dispatch.py`
- Test: `tests/test_processing_lifecycle.py`

- [ ] **Step 2.2.1: Write failing tests**

```python
# tests/test_processing_lifecycle.py
import asyncio
import pytest
from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, ProcessingOutcome, SendResult


class _ReactiveAdapter(BaseChannelAdapter):
    platform = Platform.TELEGRAM
    capabilities = ChannelCapabilities.REACTIONS
    def __init__(self, config): super().__init__(config); self.reactions = []
    async def connect(self): return True
    async def disconnect(self): pass
    async def send(self, chat_id, text, **kwargs): return SendResult(success=True)
    async def send_reaction(self, chat_id, message_id, emoji, **kwargs):
        self.reactions.append((chat_id, message_id, emoji))
        return SendResult(success=True)


@pytest.mark.asyncio
async def test_on_processing_start_adds_eyes_reaction_when_capable():
    adapter = _ReactiveAdapter({})
    await adapter.on_processing_start("chat-1", "msg-42")
    assert ("chat-1", "msg-42", "👀") in adapter.reactions


@pytest.mark.asyncio
async def test_on_processing_complete_success_replaces_with_check():
    adapter = _ReactiveAdapter({})
    await adapter.on_processing_start("chat", "1")
    await adapter.on_processing_complete("chat", "1", ProcessingOutcome.SUCCESS)
    assert ("chat", "1", "✅") in adapter.reactions


@pytest.mark.asyncio
async def test_on_processing_complete_failure_uses_cross():
    adapter = _ReactiveAdapter({})
    await adapter.on_processing_complete("chat", "1", ProcessingOutcome.FAILURE)
    assert ("chat", "1", "❌") in adapter.reactions


@pytest.mark.asyncio
async def test_no_reactions_capability_is_noop():
    class _PlainAdapter(_ReactiveAdapter):
        capabilities = ChannelCapabilities.NONE
    adapter = _PlainAdapter({})
    await adapter.on_processing_start("chat", "1")
    assert adapter.reactions == []


@pytest.mark.asyncio
async def test_reaction_send_failure_swallowed():
    class _BrokenAdapter(_ReactiveAdapter):
        async def send_reaction(self, *a, **kw):
            raise RuntimeError("api down")
    adapter = _BrokenAdapter({})
    # Should NOT raise
    await adapter.on_processing_start("chat", "1")
```

- [ ] **Step 2.2.2: Implement hooks on BaseChannelAdapter**

Append to `plugin_sdk/channel_contract.py`:

```python
async def on_processing_start(self, chat_id: str, message_id: str | None) -> None:
    """Hook: called when agent begins processing this message.

    Default: if REACTIONS capability is set and message_id provided, add
    👀 reaction. Override for custom behaviour.
    """
    if not message_id:
        return
    if not (self.capabilities & ChannelCapabilities.REACTIONS):
        return
    await self._run_processing_hook(self.send_reaction(chat_id, message_id, "👀"))


async def on_processing_complete(
    self,
    chat_id: str,
    message_id: str | None,
    outcome,
) -> None:
    """Hook: called when agent finishes processing.

    Default: replace 👀 with ✅ (SUCCESS) / ❌ (FAILURE) / clear (CANCELLED).
    """
    if not message_id:
        return
    if not (self.capabilities & ChannelCapabilities.REACTIONS):
        return
    from plugin_sdk.core import ProcessingOutcome
    emoji_map = {
        ProcessingOutcome.SUCCESS: "✅",
        ProcessingOutcome.FAILURE: "❌",
        ProcessingOutcome.CANCELLED: "",
    }
    emoji = emoji_map.get(outcome, "")
    if emoji:
        await self._run_processing_hook(self.send_reaction(chat_id, message_id, emoji))


async def _run_processing_hook(self, coro) -> None:
    """Swallow exceptions from a fire-and-forget hook coroutine."""
    try:
        await coro
    except Exception:  # noqa: BLE001
        logger.debug("processing-hook coroutine raised; swallowing", exc_info=True)
```

- [ ] **Step 2.2.3: Wire into Dispatch**

Edit `opencomputer/gateway/dispatch.py`. Locate `Dispatch.handle_message`. After computing session_id and acquiring per-chat lock, before invoking the agent loop:

```python
# Inside handle_message, after lock acquisition + before loop call:
adapter = self._adapters_by_platform.get(event.platform)
message_id = event.metadata.get("message_id") if event.metadata else None
if adapter:
    asyncio.create_task(self._safe_lifecycle_hook(
        adapter.on_processing_start(event.chat_id, message_id)
    ))

# After agent loop call (in success / failure / except branches):
from plugin_sdk.core import ProcessingOutcome
outcome = ProcessingOutcome.SUCCESS  # or FAILURE in except branch
if adapter:
    asyncio.create_task(self._safe_lifecycle_hook(
        adapter.on_processing_complete(event.chat_id, message_id, outcome)
    ))
```

Add helper to `Dispatch` class:

```python
async def _safe_lifecycle_hook(self, coro) -> None:
    """Fire-and-forget lifecycle hook with error swallowing."""
    try:
        await coro
    except Exception:  # noqa: BLE001
        self._log.debug("lifecycle hook raised", exc_info=True)
```

- [ ] **Step 2.2.4: Run tests + verify dispatch integration test**

```bash
pytest tests/test_processing_lifecycle.py -v
pytest tests/test_phase7.py -v  # existing dispatch tests must still pass
```

Expected: all PASS.

- [ ] **Step 2.2.5: Commit**

```bash
git add plugin_sdk/channel_contract.py opencomputer/gateway/dispatch.py tests/test_processing_lifecycle.py
git commit -m "feat: reaction lifecycle hooks (on_processing_start/_complete)

BaseChannelAdapter gains default 👀→✅/❌ behaviour for REACTIONS-capable
adapters. Dispatch fires hooks around agent loop calls (fire-and-forget,
errors swallowed). No-op on adapters without REACTIONS cap.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2.3: `_set_fatal_error` + supervisor handoff

**Files:**
- Modify: `plugin_sdk/channel_contract.py`
- Modify: `opencomputer/gateway/server.py`
- Test: `tests/test_fatal_error_handoff.py`

- [ ] **Step 2.3.1: Write tests + implement**

```python
# tests/test_fatal_error_handoff.py
import pytest
from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Platform


class _MinAdapter(BaseChannelAdapter):
    platform = Platform.CLI
    async def connect(self): return True
    async def disconnect(self): pass
    async def send(self, *a, **kw): pass


def test_set_fatal_error_records_state():
    a = _MinAdapter({})
    a._set_fatal_error("conflict", "another process polling", retryable=False)
    assert a._fatal_error_code == "conflict"
    assert a._fatal_error_message == "another process polling"
    assert a._fatal_error_retryable is False
    assert a.has_fatal_error()


def test_set_fatal_error_retryable_true():
    a = _MinAdapter({})
    a._set_fatal_error("network", "transport down", retryable=True)
    assert a._fatal_error_retryable is True


def test_no_fatal_error_initial_state():
    a = _MinAdapter({})
    assert not a.has_fatal_error()
```

Add to `BaseChannelAdapter.__init__`:

```python
self._fatal_error_code: str | None = None
self._fatal_error_message: str | None = None
self._fatal_error_retryable: bool = False
```

Add methods:

```python
def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
    """Mark adapter as fatally errored. Gateway supervisor reads this."""
    self._fatal_error_code = code
    self._fatal_error_message = message
    self._fatal_error_retryable = retryable
    logger.error("adapter fatal error: code=%s msg=%s retryable=%s",
                 code, message, retryable)


def has_fatal_error(self) -> bool:
    return self._fatal_error_code is not None
```

In `opencomputer/gateway/server.py`, in the main loop (`serve_forever` or its periodic check), add a 60s tick that:
1. Iterates adapters
2. Calls `adapter.has_fatal_error()`
3. If retryable=True: calls `adapter.disconnect()` then `adapter.connect()`
4. If retryable=False: logs ERROR + leaves adapter disconnected

```python
# In Gateway class
async def _check_fatal_errors_periodic(self) -> None:
    """Tick every 60s; reconnect retryable-fatal adapters; warn on non-retryable."""
    while not self._stop.is_set():
        await asyncio.sleep(60)
        for adapter in self._adapters:
            if not adapter.has_fatal_error():
                continue
            code = adapter._fatal_error_code
            retryable = adapter._fatal_error_retryable
            if retryable:
                self._log.warning("reconnecting adapter %s (fatal=%s)",
                                  adapter.platform, code)
                try:
                    await adapter.disconnect()
                    adapter._fatal_error_code = None
                    adapter._fatal_error_message = None
                    adapter._fatal_error_retryable = False
                    await adapter.connect()
                except Exception:  # noqa: BLE001
                    self._log.exception("adapter reconnect failed")
            else:
                self._log.error("adapter %s fatal-non-retryable: %s",
                                adapter.platform, code)
```

Wire `_check_fatal_errors_periodic` into `Gateway.start` as a background task.

- [ ] **Step 2.3.2: Run tests + commit**

```bash
pytest tests/test_fatal_error_handoff.py -v
git add plugin_sdk/channel_contract.py opencomputer/gateway/server.py tests/test_fatal_error_handoff.py
git commit -m "feat: BaseChannelAdapter fatal-error handoff to gateway supervisor

Adapters can mark themselves fatally errored via _set_fatal_error. Gateway
ticks every 60s; reconnects retryable-fatal adapters; logs ERROR for
non-retryable. Hermes parity (gateway/platforms/base.py:_set_fatal_error).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2.4: `extract_local_files` + `extract_media`

**Files:**
- Modify: `plugin_sdk/channel_contract.py`
- Test: `tests/test_extract_local_files.py`, `tests/test_extract_media.py`

- [ ] **Step 2.4.1: Write tests for extract_local_files**

```python
# tests/test_extract_local_files.py
import pytest
from pathlib import Path
from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Platform


class _A(BaseChannelAdapter):
    platform = Platform.CLI
    async def connect(self): return True
    async def disconnect(self): pass
    async def send(self, *a, **kw): pass


def test_extract_local_files_bare_image_path(tmp_path):
    f = tmp_path / "foo.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    text = f"check this: {f}"
    cleaned, files = _A({}).extract_local_files(text)
    assert files == [f]
    assert "foo.png" not in cleaned


def test_extract_local_files_ignores_inside_code_block(tmp_path):
    f = tmp_path / "foo.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    text = f"```\n{f}\n```"
    cleaned, files = _A({}).extract_local_files(text)
    assert files == []
    assert str(f) in cleaned


def test_extract_local_files_ignores_inside_inline_code(tmp_path):
    f = tmp_path / "foo.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n")
    text = f"`{f}`"
    cleaned, files = _A({}).extract_local_files(text)
    assert files == []


def test_extract_local_files_nonexistent_passed_through(tmp_path):
    text = "/nonexistent/path/foo.png"
    cleaned, files = _A({}).extract_local_files(text)
    assert files == []
    assert "foo.png" in cleaned


def test_extract_local_files_relative_not_extracted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel.png").write_bytes(b"\x89PNG")
    text = "see: rel.png"
    cleaned, files = _A({}).extract_local_files(text)
    # Relative paths NOT extracted (security)
    assert files == []
```

- [ ] **Step 2.4.2: Implement extract_local_files**

Append to `BaseChannelAdapter`:

```python
import os
import re as _re

# Module-level regex (avoid recompile)
_BARE_PATH_RE = _re.compile(r"(?<![/\w])(/[^\s`'\"<>]+\.[a-zA-Z0-9]{1,5})(?=\s|$|[.,;:!?])")
_HOME_PATH_RE = _re.compile(r"(?<![/\w])(~/[^\s`'\"<>]+\.[a-zA-Z0-9]{1,5})(?=\s|$|[.,;:!?])")
_FENCE_BLOCK_RE = _re.compile(r"```.*?```", _re.DOTALL)
_INLINE_CODE_RE = _re.compile(r"`[^`\n]+`")


def extract_local_files(self, content: str) -> tuple[str, list[Path]]:
    """Extract bare absolute file paths from agent output.

    Excludes paths inside fenced code blocks or inline code. Validates
    path exists via os.path.isfile. Returns (cleaned_text, [Path, ...]).

    Relative paths are NOT extracted (security: prevents path-traversal
    attacks where the agent emits ``./../etc/passwd``).
    """
    if not content:
        return content, []

    # Mask code regions so paths inside them aren't matched
    masked = _FENCE_BLOCK_RE.sub(lambda m: "\x00" * len(m.group(0)), content)
    masked = _INLINE_CODE_RE.sub(lambda m: "\x00" * len(m.group(0)), masked)

    paths: list[Path] = []
    cleaned = content

    for regex in (_BARE_PATH_RE, _HOME_PATH_RE):
        for match in regex.finditer(masked):
            raw = match.group(1)
            expanded = Path(os.path.expanduser(raw))
            if expanded.is_file():
                paths.append(expanded)
                cleaned = cleaned.replace(raw, "")

    cleaned = _re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, paths
```

- [ ] **Step 2.4.3: Write tests + implement extract_media**

```python
# tests/test_extract_media.py
from plugin_sdk.channel_contract import BaseChannelAdapter, MediaItem
from plugin_sdk.core import Platform


class _A(BaseChannelAdapter):
    platform = Platform.CLI
    async def connect(self): return True
    async def disconnect(self): pass
    async def send(self, *a, **kw): pass


def test_extract_media_directive_basic():
    text = "Look at this MEDIA: /tmp/foo.png and that's it"
    cleaned, items = _A({}).extract_media(text)
    assert items == [MediaItem(path="/tmp/foo.png", as_voice=False, ext="png")]
    assert "MEDIA: /tmp/foo.png" not in cleaned


def test_extract_media_audio_as_voice_directive():
    text = "[[audio_as_voice]] /tmp/note.ogg"
    cleaned, items = _A({}).extract_media(text)
    assert items == [MediaItem(path="/tmp/note.ogg", as_voice=True, ext="ogg")]


def test_extract_media_quoted_path():
    text = 'MEDIA: "/tmp/path with spaces.png"'
    cleaned, items = _A({}).extract_media(text)
    assert items[0].path == "/tmp/path with spaces.png"


def test_extract_media_ext_whitelist_enforced():
    text = "MEDIA: /tmp/script.exe"
    cleaned, items = _A({}).extract_media(text)
    assert items == []  # rejected
```

Append to `plugin_sdk/channel_contract.py`:

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class MediaItem:
    path: str
    as_voice: bool
    ext: str


_MEDIA_EXT_WHITELIST = frozenset({
    "png", "jpg", "jpeg", "gif", "webp",
    "mp4", "mov", "avi", "mkv", "webm",
    "ogg", "opus", "mp3", "wav", "m4a",
    "epub", "pdf", "zip", "docx", "doc", "xlsx", "xls", "pptx", "ppt",
    "txt", "csv", "md",
})

_MEDIA_DIRECTIVE_RE = _re.compile(
    r"(?:\[\[audio_as_voice\]\]\s*|MEDIA:\s*)"
    r"(?:\"([^\"]+)\"|'([^']+)'|`([^`]+)`|(\S+))",
)


def extract_media(self, content: str) -> tuple[str, list[MediaItem]]:
    """Parse MEDIA: <path> and [[audio_as_voice]] <path> directives.

    Whitelist-checks the extension. Returns cleaned text + media items.
    """
    if not content:
        return content, []
    items: list[MediaItem] = []
    cleaned = content
    for match in _MEDIA_DIRECTIVE_RE.finditer(content):
        path = next(g for g in match.groups() if g is not None)
        as_voice = "[[audio_as_voice]]" in match.group(0)
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext not in _MEDIA_EXT_WHITELIST:
            continue
        items.append(MediaItem(path=path, as_voice=as_voice, ext=ext))
        cleaned = cleaned.replace(match.group(0), "")
    cleaned = _re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned, items
```

- [ ] **Step 2.4.4: Run tests + commit**

```bash
pytest tests/test_extract_local_files.py tests/test_extract_media.py -v
git add plugin_sdk/channel_contract.py tests/test_extract_local_files.py tests/test_extract_media.py
git commit -m "feat: extract_local_files + extract_media on BaseChannelAdapter

Agent output post-processing: bare absolute paths to attached files;
MEDIA: and [[audio_as_voice]] directives parsed and routed. Code-region-
aware (paths inside fenced/inline code preserved). Extension whitelist.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2.5: `resolve_channel_prompt` / `resolve_channel_skills`

**Files:**
- Modify: `plugin_sdk/channel_contract.py`
- Test: `tests/test_channel_prompt_resolution.py`

- [ ] **Step 2.5.1: Tests + impl**

Add to `BaseChannelAdapter`:

```python
def resolve_channel_prompt(
    self, channel_id: str, parent_id: str | None = None,
) -> str | None:
    """Per-channel ephemeral system prompt. Falls back to parent_id if missing.

    Default: read self.config.get("channel_prompts", {}). Override per-platform.
    Returns None if no prompt configured.
    """
    prompts = (self.config or {}).get("channel_prompts") or {}
    if channel_id in prompts:
        return prompts[channel_id]
    if parent_id and parent_id in prompts:
        return prompts[parent_id]
    return None


def resolve_channel_skills(
    self, channel_id: str, parent_id: str | None = None,
) -> list[str]:
    """Per-channel auto-load skill list. Falls back to parent_id."""
    bindings = (self.config or {}).get("channel_skill_bindings") or {}
    if channel_id in bindings:
        return list(bindings[channel_id])
    if parent_id and parent_id in bindings:
        return list(bindings[parent_id])
    return []
```

```python
# tests/test_channel_prompt_resolution.py
from plugin_sdk.channel_contract import BaseChannelAdapter
from plugin_sdk.core import Platform

class _A(BaseChannelAdapter):
    platform = Platform.CLI
    async def connect(self): return True
    async def disconnect(self): pass
    async def send(self, *a, **kw): pass

def test_resolve_channel_prompt_direct():
    a = _A({"channel_prompts": {"chan-1": "be helpful"}})
    assert a.resolve_channel_prompt("chan-1") == "be helpful"

def test_resolve_channel_prompt_parent_fallback():
    a = _A({"channel_prompts": {"parent": "fallback"}})
    assert a.resolve_channel_prompt("thread-1", parent_id="parent") == "fallback"

def test_resolve_channel_prompt_none_when_unset():
    a = _A({})
    assert a.resolve_channel_prompt("any") is None

def test_resolve_channel_skills_direct():
    a = _A({"channel_skill_bindings": {"c1": ["stock-market-analysis"]}})
    assert a.resolve_channel_skills("c1") == ["stock-market-analysis"]

def test_resolve_channel_skills_empty():
    assert _A({}).resolve_channel_skills("any") == []
```

- [ ] **Step 2.5.2: Run + commit**

```bash
pytest tests/test_channel_prompt_resolution.py -v
git add plugin_sdk/channel_contract.py tests/test_channel_prompt_resolution.py
git commit -m "feat: resolve_channel_prompt + resolve_channel_skills on BaseChannelAdapter

Per-channel ephemeral system prompt and skill auto-load (with parent
fallback for threaded chats). Foundation for DM Topics work in PR 5.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2.6: Photo-burst merging in Dispatch

**Files:**
- Modify: `opencomputer/gateway/dispatch.py`
- Test: `tests/test_dispatch_photo_burst.py`

- [ ] **Step 2.6.1: Write failing test**

```python
# tests/test_dispatch_photo_burst.py
import asyncio
import pytest
from unittest.mock import AsyncMock
from plugin_sdk.core import MessageEvent, Platform
from opencomputer.gateway.dispatch import Dispatch


@pytest.mark.asyncio
async def test_photo_burst_merges_attachments_within_window(monkeypatch):
    loop_mock = AsyncMock()
    loop_mock.run_conversation = AsyncMock(return_value="response")
    d = Dispatch(loop_mock, plugin_api=None)
    d._burst_window_seconds = 0.3

    # Send 3 photo events for same chat in 0.1s
    base = lambda i: MessageEvent(
        platform=Platform.TELEGRAM, chat_id="chat-1", user_id="u-1",
        text="", attachments=[f"telegram:f{i}"], timestamp=1000.0 + i*0.05,
        metadata={"message_id": str(i)},
    )
    await asyncio.gather(
        d.handle_message(base(1)),
        d.handle_message(base(2)),
        d.handle_message(base(3)),
    )
    # Only ONE agent run with 3 attachments
    assert loop_mock.run_conversation.call_count == 1
    args, kwargs = loop_mock.run_conversation.call_args
    merged_event = args[0] if args else kwargs.get("event")
    assert len(merged_event.attachments) == 3


@pytest.mark.asyncio
async def test_photo_burst_separate_sessions_not_merged():
    loop_mock = AsyncMock()
    loop_mock.run_conversation = AsyncMock(return_value="ok")
    d = Dispatch(loop_mock, plugin_api=None)
    d._burst_window_seconds = 0.3
    e1 = MessageEvent(platform=Platform.TELEGRAM, chat_id="A", user_id="u",
                      text="", attachments=["t:1"], timestamp=1000.0,
                      metadata={"message_id": "1"})
    e2 = MessageEvent(platform=Platform.TELEGRAM, chat_id="B", user_id="u",
                      text="", attachments=["t:2"], timestamp=1000.0,
                      metadata={"message_id": "2"})
    await asyncio.gather(d.handle_message(e1), d.handle_message(e2))
    assert loop_mock.run_conversation.call_count == 2
```

- [ ] **Step 2.6.2: Implement burst-merge in Dispatch**

Edit `opencomputer/gateway/dispatch.py`:

Add to `Dispatch.__init__`:

```python
self._burst_window_seconds = 0.8
self._burst_pending: dict[str, MessageEvent] = {}  # session_id -> in-flight event
self._burst_tasks: dict[str, asyncio.Task] = {}
```

Modify `handle_message` to check for burst before agent dispatch:

```python
async def handle_message(self, event: MessageEvent) -> None:
    if not event.text and not event.attachments:
        return
    session_id = session_id_for(event.platform.value, event.chat_id,
                                thread_hint=(event.metadata or {}).get("thread_hint"))

    # Photo-burst merge: if an in-flight event for this session exists
    # and the new event is "pure attachments" (no text), merge attachments
    # in. The pending dispatch task picks them up.
    if event.attachments and not event.text and session_id in self._burst_pending:
        pending = self._burst_pending[session_id]
        merged_attachments = list(pending.attachments) + list(event.attachments)
        merged_meta = dict(pending.metadata or {})
        new_meta = (event.metadata or {})
        if "attachment_meta" in new_meta:
            merged_meta.setdefault("attachment_meta", []).extend(
                new_meta["attachment_meta"]
            )
        self._burst_pending[session_id] = pending._replace(
            attachments=merged_attachments,
            metadata=merged_meta,
        ) if hasattr(pending, "_replace") else MessageEvent(
            platform=pending.platform,
            chat_id=pending.chat_id,
            user_id=pending.user_id,
            text=pending.text,
            attachments=merged_attachments,
            timestamp=pending.timestamp,
            metadata=merged_meta,
        )
        return  # piggy-back on the existing pending dispatch

    # Else: this is a fresh event; schedule a delayed dispatch so subsequent
    # photo events can merge in.
    self._burst_pending[session_id] = event
    if session_id in self._burst_tasks and not self._burst_tasks[session_id].done():
        return  # task already scheduled
    self._burst_tasks[session_id] = asyncio.create_task(
        self._dispatch_after_burst_window(session_id)
    )

async def _dispatch_after_burst_window(self, session_id: str) -> None:
    await asyncio.sleep(self._burst_window_seconds)
    event = self._burst_pending.pop(session_id, None)
    self._burst_tasks.pop(session_id, None)
    if event is None:
        return
    await self._do_dispatch(event, session_id)


async def _do_dispatch(self, event: MessageEvent, session_id: str) -> None:
    """The real dispatch logic — was previously inline in handle_message."""
    # ... existing per-chat lock + run_conversation + lifecycle hooks ...
```

NOTE: Move the existing per-chat-lock + run_conversation logic out of `handle_message` into `_do_dispatch`. Keep the existing channel_directory.record() + lifecycle-hook calls in `_do_dispatch`.

- [ ] **Step 2.6.3: Run tests + verify existing pass**

```bash
pytest tests/test_dispatch_photo_burst.py -v
pytest tests/test_phase2.py tests/test_phase7.py -v  # existing dispatch tests
```

Expected: all PASS.

- [ ] **Step 2.6.4: Commit**

```bash
git add opencomputer/gateway/dispatch.py tests/test_dispatch_photo_burst.py
git commit -m "feat(gateway): photo-burst merging in Dispatch

When multiple pure-attachment events arrive for the same session within
0.8s, merge into one event with all attachments before dispatching to
the agent. Hermes parity (gateway/platforms/base.py:merge_pending_message_event).

Critical for image-heavy workflows: forwarding 5 charts in quick succession
now produces ONE agent run, not 5.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2.7: PR 2 final + push

- [ ] **Step 2.7.1: Full test suite green**

```bash
pytest tests/ -q --tb=line 2>&1 | tail -5
ruff check plugin_sdk/ opencomputer/ tests/
```

- [ ] **Step 2.7.2: Push + update PR description (still draft)**

```bash
git push
gh pr edit --add-label hermes-port --body-file - <<'EOF'
... (updated description noting PR 2 included)
EOF
```

---

# PR 3 — Adapter wiring (the bulk)

**Goal:** Wire each adapter to use the new helpers. Telegram is the heaviest (mention boundaries, MarkdownV2, retry, fatal cap, sticker cache); other adapters are smaller.

**Risk:** Medium. Touches 11 adapter files. Mitigation: per-adapter test files; default behaviours preserved (mention-gating opt-in).

**Estimated:** 2.5 days. Highly parallelizable across subagents.

---

## Task 3.1: Telegram — mention boundaries (entity-based)

**Files:**
- Modify: `extensions/telegram/adapter.py`
- Test: `tests/test_telegram_mention_boundaries.py`

- [ ] **Step 3.1.1: Write failing tests**

```python
# tests/test_telegram_mention_boundaries.py
from extensions.telegram.adapter import TelegramAdapter


def test_mention_at_message_start_via_entity():
    adapter = TelegramAdapter({"bot_token": "x"})
    adapter._bot_username = "hermes_bot"
    msg = {
        "text": "@hermes_bot help me",
        "entities": [{"type": "mention", "offset": 0, "length": 11}],
    }
    assert adapter._message_mentions_bot(msg) is True


def test_substring_mention_without_entity_rejected():
    """@hermes_bot_admin should NOT trigger when bot is @hermes_bot."""
    adapter = TelegramAdapter({"bot_token": "x"})
    adapter._bot_username = "hermes_bot"
    msg = {
        "text": "ping @hermes_bot_admin",
        "entities": [{"type": "mention", "offset": 5, "length": 16}],  # full @-handle
    }
    assert adapter._message_mentions_bot(msg) is False


def test_text_mention_entity_accepted():
    """text_mention entities point at user, not username text."""
    adapter = TelegramAdapter({"bot_token": "x"})
    adapter._bot_username = "hermes_bot"
    adapter._bot_id = 12345
    msg = {
        "text": "hey there",
        "entities": [{"type": "text_mention", "offset": 0, "length": 3,
                      "user": {"id": 12345}}],
    }
    assert adapter._message_mentions_bot(msg) is True


def test_no_entities_returns_false():
    adapter = TelegramAdapter({"bot_token": "x"})
    adapter._bot_username = "hermes_bot"
    assert adapter._message_mentions_bot({"text": "@hermes_bot"}) is False


def test_require_mention_disabled_default():
    adapter = TelegramAdapter({"bot_token": "x"})
    assert adapter._require_mention is False


def test_free_response_chat_bypasses_gate():
    adapter = TelegramAdapter({"bot_token": "x", "require_mention": True,
                               "free_response_chats": ["chat-A"]})
    msg = {"text": "hello", "entities": [], "chat": {"id": "chat-A"}}
    assert adapter._should_process_message(msg) is True


def test_reply_to_bot_bypasses_gate():
    adapter = TelegramAdapter({"bot_token": "x", "require_mention": True})
    adapter._bot_id = 12345
    msg = {
        "text": "follow-up", "entities": [],
        "chat": {"id": "chat-X"},
        "reply_to_message": {"from": {"id": 12345}},
    }
    assert adapter._should_process_message(msg) is True
```

- [ ] **Step 3.1.2: Implement**

Edit `extensions/telegram/adapter.py`. Add to `__init__`:

```python
self._require_mention: bool = bool(config.get("require_mention", False))
self._free_response_chats: set[str] = set(map(str, config.get("free_response_chats") or []))
self._bot_username: str | None = None  # populated in connect()
```

In `connect()`, after `getMe`:

```python
self._bot_username = data["result"].get("username")
```

Add methods:

```python
def _message_mentions_bot(self, msg: dict) -> bool:
    """Return True if msg contains a Telegram MessageEntity that mentions us.

    Uses MessageEntity types:
    - "mention" = @username — match by exact username text
    - "text_mention" = user reference — match by bot user_id

    Substring matching is NOT used (would misfire on @hermes_bot_admin).
    """
    if self._bot_username is None and self._bot_id is None:
        return False
    text = msg.get("text") or msg.get("caption") or ""
    for entity in (msg.get("entities") or msg.get("caption_entities") or []):
        kind = entity.get("type")
        offset = entity.get("offset", 0)
        length = entity.get("length", 0)
        if kind == "mention" and self._bot_username:
            mention_text = text[offset:offset + length]
            # Exact match on @username (case-insensitive per Telegram norm)
            if mention_text.lower() == f"@{self._bot_username.lower()}":
                return True
        elif kind == "text_mention" and self._bot_id is not None:
            user = entity.get("user") or {}
            if user.get("id") == self._bot_id:
                return True
    return False


def _is_reply_to_bot(self, msg: dict) -> bool:
    rep = msg.get("reply_to_message")
    if not rep:
        return False
    frm = rep.get("from") or {}
    return self._bot_id is not None and frm.get("id") == self._bot_id


def _should_process_message(self, msg: dict) -> bool:
    """Apply mention-gating rules.

    Returns True if message should be dispatched to the agent loop.
    """
    if not self._require_mention:
        return True
    chat_id = str((msg.get("chat") or {}).get("id", ""))
    if chat_id in self._free_response_chats:
        return True
    if self._is_reply_to_bot(msg):
        return True
    return self._message_mentions_bot(msg)
```

Wire `_should_process_message` into `_handle_update`: after the metadata-only skip + before constructing `MessageEvent`, return early if `_should_process_message(msg)` is False.

- [ ] **Step 3.1.3: Tests + commit**

```bash
pytest tests/test_telegram_mention_boundaries.py -v
git add extensions/telegram/adapter.py tests/test_telegram_mention_boundaries.py
git commit -m "feat(telegram): entity-based mention-boundary safety (opt-in)

Default behaviour unchanged (require_mention=False). When opt-in
require_mention=True, gates inbound by Telegram MessageEntity types
(mention exact-match, text_mention user-id-match) — never substring.
Free-response chats and reply-to-bot bypass gate. Hermes parity.

Fixes: @hermes_bot_admin wakes bot when bot is @hermes_bot. Won't.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Tasks 3.2-3.16: Adapter wiring batch (compressed for plan length)

**These tasks follow the same TDD pattern: failing test → impl → run → commit.** For each:

### Task 3.2: Telegram — MarkdownV2 converter wiring

- Modify `TelegramAdapter.send` to wrap `text` via `format_converters.markdownv2.convert(text)` and pass `parse_mode="MarkdownV2"`. On API error containing "can't parse", retry with plain text + no `parse_mode`.
- Test: `tests/test_telegram_format.py` — sends MarkdownV2; falls back to plain on parse error.
- Commit: `feat(telegram): MarkdownV2 outbound formatting + plain-text fallback`

### Task 3.3: Telegram — `_send_with_retry` wiring

- Wrap `_client.post` calls in `send`/`send_typing`/`send_*media*` via `self._send_with_retry`.
- Test: assert retry on simulated `httpx.ConnectError`.
- Commit: `feat(telegram): retry transient send errors via _send_with_retry`

### Task 3.4: Telegram — fatal cap (409 + network)

- In `_poll_forever`: after 3 consecutive 409 conflicts × 10s, call `self._set_fatal_error("telegram-conflict", "another process is polling — stop it or rotate token", retryable=False)` and break.
- After 10 consecutive network errors with backoff `[5,10,20,40,60,60,...]`: `self._set_fatal_error("telegram-network", "transport down", retryable=True)`.
- Test: `tests/test_telegram_conflict_fatal.py`, `tests/test_telegram_network_fatal.py`.
- Commit: `feat(telegram): fatal-cap on 409 conflicts and network errors`

### Task 3.5: Telegram — sticker vision cache

- Create `opencomputer/cache/sticker_cache.py` (LRU JSON file at `<profile_home>/sticker_descriptions.json`, max 5000 entries).
- In `TelegramAdapter._handle_update`: on inbound sticker, call `sticker_cache.get(file_unique_id)` → if hit, inject as text; on miss, defer to vision via duck-typed `provider.describe_image(bytes)` if available.
- Test: `tests/test_sticker_cache.py` — cache hit short-circuits; LRU bound; persistence.
- Commit: `feat(telegram): sticker vision cache`

### Task 3.6: Discord — `message.mentions` detection

- Use `message.mentions` list (discord.py provides) for mention-gating.
- Add config `discord.require_mention`, `discord.allowed_users`, `discord.allowed_roles`, `discord.allow_bots`.
- Test: `tests/test_discord_allowed_users.py`.
- Commit: `feat(discord): message.mentions-based gating + allowlist`

### Task 3.7: Discord — `_send_with_retry` wiring

- Wrap discord.py send paths.
- Commit: `feat(discord): retry transient send errors`

### Task 3.8: WhatsApp — bridge mention parsing + format converter

- Use bridge-supplied `mentions[]` array (not text scan).
- Use `format_converters.whatsapp_format.convert` in send.
- Commit: `feat(whatsapp): JID-based mention parsing + format converter`

### Task 3.9: Slack — mrkdwn converter + adopt strip_markdown

- `extensions/slack/adapter.py:format_message` uses `format_converters.slack_mrkdwn.convert`.
- Replace `_emoji_to_slack_name` local with `helpers` if applicable (skip — emoji map is platform-specific).
- Commit: `feat(slack): mrkdwn formatter via plugin_sdk`

### Task 3.10: Matrix — HTML converter

- `extensions/matrix/adapter.py:send`: use `format_converters.matrix_html.convert` for `formatted_body`; plain text for `body`.
- Commit: `feat(matrix): HTML formatted_body via plugin_sdk converter`

### Task 3.11: Email — automated-sender filter

- Add `_NOREPLY_PATTERNS = re.compile(r"^(noreply|no-reply|donotreply|do-not-reply|postmaster|mailer-daemon|bounce|bounces)@", re.I)`.
- Add `_AUTOMATED_HEADERS = ("auto-submitted", "precedence", "x-auto-response-suppress", "list-unsubscribe", "list-id")`.
- Add `_is_automated_sender(sender, headers) -> bool`.
- Drop matching messages before agent dispatch (log INFO with reason).
- Test: `tests/test_email_automated_filter.py`.
- Commit: `feat(email): drop automated/list/bounce traffic`

### Task 3.12: Signal — phone redaction in logs

- Replace raw E.164 in log lines with `helpers.redact_phone(...)`.
- Commit: `feat(signal): redact phone numbers in logs`

### Task 3.13: SMS — phone redaction + helpers.strip_markdown

- Replace `_strip_markdown` (local) with `helpers.strip_markdown`.
- Replace raw `from_number` in logs with `helpers.redact_phone(...)`.
- Commit: `refactor(sms): use plugin_sdk helpers for markdown strip + phone redact`

### Task 3.14: iMessage — phone redaction

- Same pattern: redact in logs.
- Commit: `feat(imessage): redact phone numbers in logs`

### Task 3.15: Webhook — `deliver_only` mode

- Add per-route `deliver_only: bool` (default False) and `delivery_target: {platform, chat_id}`.
- When `deliver_only=true`: render via `_render_prompt` template, enqueue via `outgoing_queue.enqueue(platform, chat_id, body)` — no agent run.
- Validate `delivery_target` reaches a registered adapter at startup; refuse otherwise.
- Test: `tests/test_webhook_deliver_only.py`.
- Commit: `feat(webhook): deliver_only mode for push-only notifications`

### Task 3.16: PR 3 final + push

- [ ] Full test suite green.
- [ ] Ruff clean.
- [ ] Push + update draft PR.

---

# PR 4 — Tier 2 operational hardening

**Goal:** IP-fallback, thread-not-found retry, allowed_mentions, idempotency, cross-platform delivery, slack pause-typing.

**Estimated:** 1.5 days. Each item independent.

## Task 4.1: Telegram IP-fallback transport

- Create `extensions/telegram/network.py` with `TelegramFallbackTransport` (httpx AsyncBaseTransport + sticky IP retry preserving Host + SNI).
- DoH IP discovery via `dns.google/resolve` and `cloudflare-dns.com/dns-query`.
- `parse_fallback_ip_env(value)` validator (IPv4-only; reject private/loopback/link-local).
- Gate via `TELEGRAM_FALLBACK_IPS=auto` or comma-separated IPs.
- Tests: `tests/test_telegram_fallback_transport.py`.
- Commit: `feat(telegram): IP-fallback transport for geo-blocked regions`

## Task 4.2: Telegram thread-not-found retry

- `_is_thread_not_found_error(exc)`: matches BadRequest("message thread not found").
- On send-with-thread-id failure: retry once without `message_thread_id`. WARN-log.
- Test: `tests/test_telegram_thread_fallback.py`.
- Commit: `feat(telegram): thread-not-found retry without thread_id`

## Task 4.3: Discord allowed_mentions safe defaults

- `_build_allowed_mentions()` returns `discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=True)`.
- Env overrides: `DISCORD_ALLOW_MENTION_*`.
- Wire into all send paths.
- Test: `tests/test_discord_allowed_mentions.py`.
- Commit: `feat(discord): allowed_mentions safe defaults`

## Task 4.4: Webhook idempotency cache

- Per-route `_seen_deliveries: dict[str, float]` keyed on `delivery_id` header (or computed payload hash); 1h TTL.
- Repeat returns cached 200 response.
- Test: `tests/test_webhook_idempotency.py`.
- Commit: `feat(webhook): idempotency cache on delivery_id`

## Task 4.5: Webhook cross_platform mode

- New per-route `cross_platform: true` mode. Renders payload + enqueues to `outgoing_queue`.
- Test: `tests/test_webhook_cross_platform.py`.
- Commit: `feat(webhook): cross_platform mode for inbound→outbound routing`

## Task 4.6: Slack pause-typing during approval

- When ConsentGate prompts via Slack: clear typing indicator (`assistant_threads_setStatus("")`); restore on resolve.
- Test: `tests/test_slack_pause_typing.py`.
- Commit: `feat(slack): pause typing during consent approval`

## Task 4.7: PR 4 final + push.

---

# PR 5 — Tier 3a: DM Topics + channel-skill bindings

**Goal:** Telegram DM Topics (Bot API 9.4) creating per-topic skill/prompt routing.

**Estimated:** 3 days.

## Task 5.1: extensions/telegram/dm_topics.py

- New module: wraps `forum_topic_created`/`forum_topic_edited` updates; persists `topic_id → {label, skill, system_prompt}` to `<profile_home>/telegram_dm_topics.json`.
- API: `setup_dm_topics(adapter, config) -> None`, `get_topic_info(topic_id) -> dict | None`, `register_topic_id(label, topic_id) -> None`.
- Test: `tests/test_dm_topics.py`.

## Task 5.2: Wire dm_topics into TelegramAdapter

- On inbound message with `message_thread_id`: lookup topic info; if found, set `event.metadata["channel_id"] = topic_id` so resolve_channel_prompt/skills picks it up.

## Task 5.3: AgentLoop integration

- AgentLoop.run_conversation reads `RuntimeContext.extras["channel_id"]` and asks adapter (via plugin_api) for `resolve_channel_prompt` and `resolve_channel_skills`. Apply skills via existing skill-loading infrastructure.

## Task 5.4: CLI commands

- `opencomputer telegram topic create --label "Stocks" --skill stock-market-analysis --system "..."` — wraps Bot API `createForumTopic` + persists to config.yaml.
- `opencomputer telegram topic list` — shows configured topics.
- Test: `tests/test_telegram_topic_cli.py`.

## Task 5.5: PR 5 final + push.

---

# PR 6 — Tier 3b: Matrix E2EE + WhatsApp bridge + Discord forum threads

**Goal:** Three independent power-user features. Each self-contained.

**Estimated:** 5 days. Parallelizable across subagents.

## Task 6.1: Matrix E2EE

- Optional dep `mautrix[encryption]`. Detect at import.
- Encrypted-room handling: decrypt-on-receive, encrypt-on-send.
- Crypto state store: `<profile_home>/matrix_crypto/`.
- `_verify_device_keys_on_server`, `_reverify_keys_after_upload`.
- Test: `tests/test_matrix_e2ee.py`.

## Task 6.2: WhatsApp Node.js bridge

- New plugin `extensions/whatsapp-bridge/`. Files: `plugin.json`, `plugin.py`, `adapter.py`, `bridge_supervisor.py`, `bridge/package.json`, `bridge/index.js`.
- Baileys subprocess; HTTP API on `127.0.0.1:3001`.
- QR login flow → emit to dispatch as system message.
- Cross-platform process kill (`taskkill /T /F` vs `killpg`).
- Test: `tests/test_whatsapp_bridge.py`.

## Task 6.3: Discord forum threads + slash command tree

- `_create_thread`, `_auto_create_thread`, `_handle_thread_create_slash`, `_dispatch_thread_session`, `_send_to_forum`, `_format_thread_chat_name`.
- Slash command tree (app_commands): `/ask /reset /status /stop /steer /queue /background /side /title /resume /usage /thread`.
- Sync policy: `DISCORD_COMMAND_SYNC = safe|bulk|off`.
- Test: `tests/test_discord_threads.py`, `tests/test_discord_slash_commands.py`.

## Task 6.4: PR 6 final + push.

---

# Final checks (across all PRs)

## Self-review of plan vs spec

- [ ] Spec §3 invariants — every one has a "preserve" guarantee in code (not violated by any change in PRs 1-6). Confirmed via:
  - I1 plugin_sdk boundary: PR 1 modules are pure-stdlib; no `from opencomputer` imports. Test enforces.
  - I3 F1 ConsentGate single arbiter: PR 6 (Slack/Discord approval ports — only IF added in stretch) routes through `ConsentGate.resolve_pending`. Plan does NOT extend approval inline-button surface beyond Telegram in PR 1-6 (deferred to a future ConsentGate-platform-extension work).
  - I4 per-chat session lock: PR 2 keeps `Dispatch._locks` as the only per-chat lock. Photo-burst merge happens BEFORE lock acquisition.
  - I5 friendly-error mapping: unchanged in PR 1-6; lives in Dispatch.
  - I6 outgoing queue: PR 3.15 + PR 4.5 explicitly use outgoing_queue.enqueue rather than calling adapter.send.
  - I7 per-profile state: PR 1 ThreadParticipationTracker takes profile_home explicitly.
  - I8 webhook HMAC-SHA256: PR 4.4 idempotency adds NO crypto changes.
  - I9 single-instance lock: PR 3.4 reuses existing scope_lock.

- [ ] Spec §4-§6 features — every one has a task. Mapped:
  - §4.1 channel_helpers → Task 1.1 ✅
  - §4.2 channel_utils → Task 1.2 ✅
  - §4.3 network_utils → Task 1.3 ✅
  - §4.4 format_converters → Tasks 1.4, 1.5 ✅
  - §4.5 BaseChannelAdapter retry + lifecycle → Tasks 2.1, 2.2 ✅
  - §4.6 mention-boundary safety → Tasks 3.1 (TG), 3.6 (DC), 3.8 (WA) ✅
  - §4.7 phone redaction → Tasks 3.12-3.14 ✅
  - §4.8 email automated filter → Task 3.11 ✅
  - §4.9 photo-burst merging → Task 2.6 ✅
  - §4.10 sticker vision cache → Task 3.5 ✅
  - §4.11 webhook deliver_only → Task 3.15 ✅
  - §4.12 telegram polling fatal cap → Task 3.4 ✅
  - §4.13 extract_local_files → Task 2.4 ✅
  - §4.14 extract_media → Task 2.4 ✅
  - §5.1-§5.7 Tier 2 → Tasks 4.1-4.6 ✅
  - §6.1 DM Topics → Tasks 5.1-5.4 ✅
  - §6.2 Matrix E2EE → Task 6.1 ✅
  - §6.3 WhatsApp bridge → Task 6.2 ✅
  - §6.4 Discord forum threads → Task 6.3 ✅

- [ ] Placeholder scan: no "TODO/TBD/implement later" in concrete code blocks. Compressed task descriptions in 3.2-3.16 / 4 / 5 / 6 are intentional plan-level summaries; subagent execution will expand each into full TDD steps using the templates from Tasks 1.1, 2.1.

- [ ] Type consistency: `MediaItem` dataclass defined once (Task 2.4); referenced in tests + adapter wiring. `ProcessingOutcome` defined once (Task 1.2 step 1.2.1). `ChannelCapabilities.REACTIONS` etc. unchanged.

---

## Execution choice

**Plan complete and saved to `docs/superpowers/plans/2026-04-28-hermes-channel-feature-port.md`.** Two execution options:

**1. Subagent-Driven (recommended)** — Dispatch fresh opus subagent per task; two-stage review between tasks; preserves main-context for synthesis.

**2. Inline Execution** — Run `superpowers:executing-plans` skill in this session; batch tasks with checkpoints.

Given the size (~50 tasks across 6 PRs), **Subagent-Driven is strongly recommended** — main context would otherwise hold raw test output for 20+ days of work.
