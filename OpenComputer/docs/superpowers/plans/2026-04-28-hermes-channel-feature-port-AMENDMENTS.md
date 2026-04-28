# Hermes Channel Feature Port — Plan Amendments (post-audit)

**Source plan:** `2026-04-28-hermes-channel-feature-port.md` (3,201 lines, committed `93056f0b`)
**Audit:** `/tmp/hermes-port-plan-audit.md` (693 lines, opus independent critic, 2026-04-28)

This document amends the plan based on audit findings. Fixes are organized by severity. **All Critical (C1-C6) and High (H1-H8) fixes are mandatory** before execution starts. Two product-level scope-cut recommendations require explicit user decision.

---

## A. Critical fixes (mandatory pre-execution)

### A.1 — Photo-burst rewrite (replaces Task 2.6 fully)

**Problems** (audit C1, C2, H2):
- `MessageEvent` is `@dataclass(frozen=True, slots=True)`; `_replace` doesn't exist on it.
- Plan's refactor changes `Dispatch.handle_message` from `str | None` return to fire-and-forget None, silently breaking 7 adapters (slack/mattermost/email/signal/sms/imessage/webhook) whose default `BaseChannelAdapter.handle_message` (`channel_contract.py:108-114`) awaits the return and calls `self.send(chat_id, response)`.
- Plan's "skip merge for text" doesn't cancel the in-flight photo burst → two agent runs out of order.

**Replacement design:**

```python
# opencomputer/gateway/dispatch.py — REPLACE Task 2.6.2 implementation
import dataclasses

# Dispatch.__init__ additions
self._burst_window_seconds: float = float(self._config.get("photo_burst_window", 0.8))
self._burst_pending: dict[str, MessageEvent] = {}
self._burst_tasks: dict[str, asyncio.Task] = {}
self._burst_futures: dict[str, asyncio.Future[str | None]] = {}  # joiners await

async def handle_message(self, event: MessageEvent) -> str | None:
    """PRESERVES return contract: returns assistant text or None (unchanged)."""
    if not event.text and not event.attachments:
        return None
    session_id = session_id_for(
        event.platform.value, event.chat_id,
        thread_hint=(event.metadata or {}).get("thread_hint"),
    )

    pure_attachment = bool(event.attachments) and not event.text

    # Case 1: pure-attachment event arriving while a burst is pending — JOIN
    if pure_attachment and session_id in self._burst_pending:
        pending = self._burst_pending[session_id]
        merged_meta = dict(pending.metadata or {})
        new_meta = event.metadata or {}
        if "attachment_meta" in new_meta:
            merged_meta.setdefault("attachment_meta", []).extend(new_meta["attachment_meta"])
        self._burst_pending[session_id] = dataclasses.replace(
            pending,
            attachments=list(pending.attachments) + list(event.attachments),
            metadata=merged_meta,
        )
        # Joiner awaits the same future the original event will resolve
        future = self._burst_futures[session_id]
        return await future

    # Case 2: text (or text+attachment) event arriving while a burst is pending — CANCEL + MERGE-IN
    if event.text and session_id in self._burst_tasks:
        # Cancel pending dispatch; absorb its attachments into this event
        self._burst_tasks[session_id].cancel()
        pending = self._burst_pending.pop(session_id, None)
        future = self._burst_futures.pop(session_id, None)
        self._burst_tasks.pop(session_id, None)
        if pending:
            event = dataclasses.replace(
                event,
                attachments=list(pending.attachments) + list(event.attachments),
            )
        # Run inline (no burst wait — text is the user's "go" signal)
        result = await self._do_dispatch(event, session_id)
        if future and not future.done():
            future.set_result(result)
        return result

    # Case 3: pure-attachment event with NO pending burst — start the timer
    if pure_attachment:
        self._burst_pending[session_id] = event
        self._burst_futures[session_id] = asyncio.get_running_loop().create_future()
        future = self._burst_futures[session_id]
        self._burst_tasks[session_id] = asyncio.create_task(
            self._dispatch_after_burst_window(session_id)
        )
        return await future

    # Case 4: text-only event with no burst — direct dispatch
    return await self._do_dispatch(event, session_id)


async def _dispatch_after_burst_window(self, session_id: str) -> None:
    try:
        await asyncio.sleep(self._burst_window_seconds)
    except asyncio.CancelledError:
        return
    event = self._burst_pending.pop(session_id, None)
    future = self._burst_futures.pop(session_id, None)
    self._burst_tasks.pop(session_id, None)
    if event is None or future is None:
        return
    try:
        result = await self._do_dispatch(event, session_id)
        if not future.done():
            future.set_result(result)
    except Exception as exc:  # noqa: BLE001
        if not future.done():
            future.set_exception(exc)


async def _do_dispatch(self, event: MessageEvent, session_id: str) -> str | None:
    """The original handle_message body, extracted. Same return contract."""
    # ... existing code (per-chat lock, channel_directory.record, lifecycle hooks,
    # run_conversation, return text) — DO NOT change this method's behavior
```

**Required tests added to Task 2.6:**

```python
@pytest.mark.asyncio
async def test_handle_message_default_text_only_returns_assistant_text(monkeypatch):
    """Regression: simple text event preserves str-or-None return contract."""
    loop_mock = AsyncMock()
    loop_mock.run_conversation = AsyncMock(return_value="hello back")
    d = Dispatch(loop_mock, plugin_api=None)
    event = MessageEvent(platform=Platform.SMS, chat_id="+1", user_id="u",
                         text="hi", attachments=[], timestamp=1000.0, metadata={})
    result = await d.handle_message(event)
    assert result == "hello back"


@pytest.mark.asyncio
async def test_text_arrival_cancels_pending_burst_and_merges():
    """When text arrives mid-burst, cancel pending dispatch + merge attachments in."""
    loop_mock = AsyncMock()
    loop_mock.run_conversation = AsyncMock(return_value="combined response")
    d = Dispatch(loop_mock, plugin_api=None)
    d._burst_window_seconds = 0.5

    photo = MessageEvent(platform=Platform.TELEGRAM, chat_id="A", user_id="u",
                         text="", attachments=["t:1"], timestamp=1000.0,
                         metadata={"message_id": "1"})
    text = MessageEvent(platform=Platform.TELEGRAM, chat_id="A", user_id="u",
                        text="what's this?", attachments=[], timestamp=1001.0,
                        metadata={"message_id": "2"})
    photo_task = asyncio.create_task(d.handle_message(photo))
    await asyncio.sleep(0.1)  # photo is pending
    result = await d.handle_message(text)
    photo_result = await photo_task
    assert result == "combined response"
    assert photo_result == "combined response"  # joiner gets same answer
    # Single agent run with both
    assert loop_mock.run_conversation.call_count == 1
    args, _ = loop_mock.run_conversation.call_args
    merged_event = args[0]
    assert "t:1" in merged_event.attachments
    assert merged_event.text == "what's this?"
```

### A.2 — Sticker cache moves to `plugin_sdk/sticker_cache.py`

**Problem** (audit C6): adapters under `extensions/` cannot import from `opencomputer.*` (test-enforced via `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer`). Plan's path `opencomputer/cache/sticker_cache.py` violates this.

**Fix**: change Task 3.5 file path to `plugin_sdk/sticker_cache.py`. Telegram adapter does `from plugin_sdk.sticker_cache import StickerCache`. Constructor takes `profile_home: Path` (preserves per-profile invariant).

```python
# plugin_sdk/sticker_cache.py
"""Persistent file_unique_id → vision-description LRU cache.

OrderedDict with move_to_end on get for recency. Atomic JSON write.
Single-writer assumption (one bot per profile per process).
"""
from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

class StickerCache:
    def __init__(self, profile_home: Path, max_entries: int = 5000) -> None:
        self._path = Path(profile_home) / "sticker_descriptions.json"
        self._max = max_entries
        self._data: OrderedDict[str, str] = self._load()

    def _load(self) -> OrderedDict[str, str]:
        try:
            raw = json.loads(self._path.read_text())
            if isinstance(raw, list):
                return OrderedDict((str(k), str(v)) for k, v in raw[-self._max:])
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return OrderedDict()

    def _save(self) -> None:
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(list(self._data.items())))
            tmp.replace(self._path)
        except OSError:
            pass

    def get(self, file_unique_id: str) -> str | None:
        if file_unique_id in self._data:
            self._data.move_to_end(file_unique_id)
            return self._data[file_unique_id]
        return None

    def put(self, file_unique_id: str, description: str) -> None:
        self._data[file_unique_id] = description
        self._data.move_to_end(file_unique_id)
        while len(self._data) > self._max:
            self._data.popitem(last=False)
        self._save()
```

### A.3 — Webhook outgoing_queue access via PluginAPI

**Problem** (audit H3): plan's webhook adapter import `from opencomputer.gateway.outgoing_queue import OutgoingQueue` violates the same boundary as A.2.

**Fix**: extend `plugin_sdk/PluginAPI` with an `outgoing_queue` accessor that proxies to `opencomputer.gateway.outgoing_queue.OutgoingQueue`. Webhook adapter calls `self._plugin_api.outgoing_queue.enqueue(platform, chat_id, body)`.

Add a new task **Task 2.0 (pre-PR-3)** that does this in `plugin_sdk/plugin_api.py` (or wherever PluginAPI lives — confirm during execution).

### A.4 — Manifest schema awareness

**Problem** (audit C3): `PluginManifestSchema` uses `extra="forbid"` and has NO `capabilities` field. Four existing manifests (ambient-sensors, browser-control, skill-evolution, voice-mode) fail discovery silently. Plan adds `extensions/whatsapp-bridge/plugin.json` and could repeat the bug.

**Fix**: add Pre-flight Steps 0.4 + 0.5 to plan:

```markdown
- [ ] **Step 0.4: Document manifest schema constraint**

Read `opencomputer/plugins/manifest_validator.py:121-198`. Confirm:
- PluginManifestSchema uses extra="forbid"
- No `capabilities` field exists
- ALL new plugin.json files (whatsapp-bridge in PR 6, any future plugins) MUST omit `capabilities`
- Capability declaration for F1 ConsentGate happens at TOOL level via `BaseTool.capability_claims`, not at manifest level

- [ ] **Step 0.5: Verify pytest-asyncio + ruff config baselines**

```bash
grep -A1 "asyncio_mode\|asyncio" pyproject.toml
grep "BLE001\|select\|ignore" pyproject.toml
```

Required: `asyncio_mode = "auto"` (or every async test is decorated). Ruff must NOT have BLE001 in the selected set unless every `except Exception:` in plugin code carries `# noqa: BLE001`.
```

(Fixing the four broken manifests is out of scope for this port — separate cleanup PR. Plan §0.5 just documents.)

### A.5 — `_set_fatal_error` adds `clear_fatal_error()` method

**Problem** (audit M5): plan has gateway mutating `adapter._fatal_error_code = None` directly — leaks encapsulation.

**Fix**: in Task 2.3, add to BaseChannelAdapter:

```python
def clear_fatal_error(self) -> None:
    """Reset fatal-error state. Called by gateway supervisor after successful reconnect."""
    self._fatal_error_code = None
    self._fatal_error_message = None
    self._fatal_error_retryable = False
```

In `Gateway._check_fatal_errors_periodic`, replace direct mutation with `adapter.clear_fatal_error()`.

### A.6 — Mention-gating regression tests

**Problem** (audit C4): plan asserts default `require_mention=False` preserves existing behavior but no test guards this.

**Fix**: add to Task 3.1.1:

```python
def test_default_no_config_passes_through_normal_message():
    """Regression: default config (no require_mention key) MUST allow plain text in 1:1."""
    adapter = TelegramAdapter({"bot_token": "x"})
    msg = {"text": "hi there", "entities": [], "chat": {"id": "u1"}}
    assert adapter._should_process_message(msg) is True


def test_default_no_config_passes_group_message_too():
    """Regression: default behavior allows group messages to wake bot (existing behavior)."""
    adapter = TelegramAdapter({"bot_token": "x"})
    msg = {"text": "hello bot", "entities": [], "chat": {"id": -1001}, "from": {"id": 7}}
    assert adapter._should_process_message(msg) is True
```

### A.7 — F1 ConsentGate × lifecycle hook integration test

**Problem** (audit C5): plan's reaction-lifecycle hooks fire concurrently with F1 consent prompts; ordering is implicit but untested.

**Fix**: add to Task 2.2 (Step 2.2.5 or new step):

```python
@pytest.mark.asyncio
async def test_processing_lifecycle_during_consent_prompt():
    """Verify ordering: 👀 → consent prompt → user click → ✅ → reply."""
    # Use a fake adapter + mock ConsentGate that prompts mid-conversation
    # Assert call sequence: on_processing_start → send_approval_request →
    # ConsentGate.resolve_pending → on_processing_complete → send
    # Assert no parallel approval state created
```

Full test body deferred to subagent execution; the test name + assertion list is sufficient guidance.

### A.8 — Attachment allowlist for `extract_local_files`

**Problem** (audit M3): plan claims excluding relative paths is "security." Real risk: agent emits `/etc/passwd` (absolute, exists) → adapter attaches it → leaks via chat.

**Fix**: in Task 2.4, add allowlist:

```python
def extract_local_files(
    self, content: str,
    allowed_dirs: list[Path] | None = None,
) -> tuple[str, list[Path]]:
    """... existing logic ...

    Only paths inside `allowed_dirs` are returned. Default allowlist:
    [Path.home() / "Documents", Path("/tmp")]. Override via config
    `attachments.allowed_dirs`.
    """
    if allowed_dirs is None:
        allowed_dirs = [Path.home() / "Documents", Path("/tmp")]
    # ... after path validation, also check is_in_allowlist(p):
    if not any(self._is_subpath(expanded, d) for d in allowed_dirs):
        continue  # reject path outside allowlist
    paths.append(expanded)


def _is_subpath(self, path: Path, allowed_dir: Path) -> bool:
    try:
        path.resolve().relative_to(allowed_dir.resolve())
        return True
    except ValueError:
        return False
```

Add test: `test_extract_local_files_outside_allowlist_rejected` asserting `/etc/passwd` (if exists) is NOT extracted.

---

## B. High-priority fixes (during execution)

### B.1 — Split PR 3 into PR 3a / 3b / 3c

**Problem** (audit H6, H7): PR 3 has 16 sub-tasks compressed into "follow same TDD pattern" — too thin for subagent execution; whole PR is 4 days realistic, not 2.5.

**Fix:**

| Original | Replacement |
|---|---|
| PR 3 (all 16 tasks, 2.5 days) | **PR 3a — Telegram only** (5 tasks, ~1.5 days): mention boundaries, MarkdownV2, retry, fatal cap, sticker cache |
| | **PR 3b — Discord/Slack/Matrix/WhatsApp** (4 tasks, ~1 day): mention/format/retry wiring per channel |
| | **PR 3c — Email/Signal/SMS/iMessage/Webhook** (6 tasks, ~1 day): phone redaction, email filter, webhook deliver_only, helpers.strip_markdown adoption |

PR 3a/3b/3c are independent (no shared adapter); can run in parallel via subagents.

### B.2 — Expand Tasks 3.2-3.16 with concrete details

**Problem** (audit H6): compressed tasks miss method names, error strings, file:line references. Subagents will rework.

**Fix examples** (apply the pattern to all of 3.2-3.16):

**Task 3.2 (Telegram MarkdownV2 wiring) — expanded:**
- Methods to wrap: `send` (text), `send_photo.caption`, `send_document.caption`, `send_voice.caption`, `edit_message` (text), `send_approval_request.prompt_text`.
- Error string for plain-text fallback: `"Bad Request: can't parse entities"`.
- Tests must cover: each method's MarkdownV2 path; each method's plain-text fallback on parse error; edit_message's 48h window unchanged.
- Hermes reference: `gateway/platforms/telegram.py:format_message` + `_escape_mdv2`.

**Task 3.6 (Discord mention) — expanded:**
- Use `bot.user.mentioned_in(message)` (canonical) AND scan `message.mentions` list (catches role-mentions).
- Multi-bot disambiguation: when other bots mentioned but not us, return False (preserves existing single-bot ergonomics).
- Tests: ALL 6 paths from spec §4.6.

(Apply similar expansion in execution phase for 3.3, 3.4, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15. Subagent receives the per-task expansion as part of the prompt, not just plan §3.X.)

### B.3 — `delivery_id` semantics in webhook idempotency

**Fix**: Task 4.4 specifies:

```python
def _delivery_id(self, request: aiohttp.web.Request, body: bytes, token_id: str) -> str:
    """Idempotency key. Header preferred; computed sha256(body|token_id) fallback."""
    for header_name in ("X-Github-Delivery", "X-Delivery-ID", "X-Idempotency-Key", "Stripe-Signature"):
        v = request.headers.get(header_name)
        if v:
            return v
    return hashlib.sha256(body + token_id.encode()).hexdigest()
```

### B.4 — `flock` for `<profile_home>/telegram_dm_topics.json`

**Fix**: Task 5.1 — wrap JSON writes in `fcntl.flock(LOCK_EX)` on POSIX, `msvcrt.locking()` on Windows. Reuse the helper if any exists in OC; otherwise add `plugin_sdk/file_lock.py`.

### B.5 — Photo-burst window configurable

**Fix**: Task 2.6 — add to gateway config schema: `gateway.photo_burst_window: float = 0.8`. `Dispatch.__init__` reads from config.

### B.6 — Fuzz test for MarkdownV2

**Fix**: Task 1.4 — add `tests/test_format_converters.py::test_markdownv2_fuzz`:

```python
import random
@pytest.mark.parametrize("seed", range(50))
def test_markdownv2_fuzz_does_not_raise(seed):
    """Random markdown-ish input must not raise from convert()."""
    random.seed(seed)
    chars = "abc123 _*[]()~`>#+-=|{}.!\\\n"
    text = "".join(random.choice(chars) for _ in range(200))
    out = to_mdv2(text)  # MUST NOT raise
    assert isinstance(out, str)
```

---

## C. Medium / observation items (optional during execution)

- **M1** Format converters DRY-extraction (`plugin_sdk/format_converters/_common.py`) — defer; pattern-spotted refactor is fine post-merge.
- **M4** Lifecycle-hook observability gap (warn-then-debug for first failure per platform) — fine to add in PR 4.
- **M5** Adapter restart semantics — flag as latent debt; plan as written is acceptable v1.
- **L3** Windows path support in `extract_local_files` — defer; user is on macOS.
- **L7** `mention_patterns` regex feature — keep in spec but don't block on it; minimal test only.

---

## D. Schedule revision

| | Original | Revised |
|---|---|---|
| **PR 1** | 2 d | 2.25 d (+ fuzz test, +0.5 helper module shared placeholder) |
| **PR 2** | 1.5 d | 2.5 d (+1 d for photo-burst rework + integration tests + clear_fatal_error + allowlist) |
| **PR 3** | 2.5 d | 3.5 d (split into 3a/3b/3c with expanded task descriptions) |
| **PR 4** | 1.5 d | 1.75 d (+0.25 for delivery_id + flock parity in DM Topics) |
| **PR 5** | 3 d | 3.25 d (+ flock in DM Topics) |
| **PR 6** | 5 d | **DEPENDS — see scope-cut decision in §E** |
| **Pre-flight** | — | +0.25 d (Steps 0.4 + 0.5) |

If both Tier-3 scope cuts accepted (drop WhatsApp bridge + Matrix E2EE):
- **PR 6** = 2 d (Discord forum threads only)
- **Total focused work** = 15.5 d (≈ original target preserved)

If both Tier-3 items kept:
- **PR 6** = 8 d (Matrix E2EE 3d + WhatsApp bridge 4d + Discord forum threads 1d realistic, was 5d)
- **Total focused work** = ~22 d realistic (47% over original target)

---

## E. Scope-cut decision required from user

Audit recommendation: **drop WhatsApp bridge AND Matrix E2EE from this port; keep Discord forum threads.**

### E.1 — WhatsApp Node.js bridge (Task 6.2)

**Audit reasoning:**
- OC already has `extensions/whatsapp/` with Cloud API. Outbound works; inbound via webhook.
- Cloud API can use a "test/development" WhatsApp Business account with one phone, no formal verification.
- Bridge adds: Node.js install, Baileys lib supply chain, subprocess management, QR login flow piping into dispatch, cross-platform process kill (Windows taskkill vs POSIX killpg with mocks), ~600 LOC, ongoing version pinning.
- Bridge reward: same WhatsApp surface, just personal-account.
- Maintenance commitment is large for marginal personal-bot benefit.

**Recommendation: DROP** — defer to post-dogfood gate. Re-evaluate if Saksham actually wants WhatsApp as a primary OC channel.

**User decision needed**: keep or drop?

### E.2 — Matrix E2EE (Task 6.1)

**Audit reasoning:**
- Without E2EE, Matrix adapter is broken-by-default (most modern Matrix rooms encrypted).
- libolm + python-olm install on macOS needs `brew install libolm` + version-pinning headaches.
- Realistic: 3 days focused (libolm install troubleshooting + crypto state store + device verification + tests with synapse mock or real homeserver).
- Saksham's CLAUDE.md / memory shows no current Matrix usage.

**Recommendation: DROP** — Matrix adapter stays unencrypted (current state); document E2EE as a future feature behind `pip install opencomputer[matrix-e2ee]`. Zero new Matrix code in this port.

**User decision needed**: keep or drop?

### E.3 — Discord forum threads (Task 6.3) — KEEP regardless

Self-contained Discord work; no dependencies on Matrix/WhatsApp; modest scope (~2 days). Leave in plan unchanged.

---

## F. Action items before execution

1. **User decides** §E.1 + §E.2 (keep or drop).
2. **Plan revisions applied** per §A.1-A.8 + §B.1-B.6 (technical fixes are mandatory regardless of scope decision).
3. **Pre-flight Steps 0.4 + 0.5 added** to plan.
4. **PR 3 split into 3a/3b/3c** with expanded task descriptions for each adapter.
5. **Task 2.6 fully rewritten** per §A.1.
6. **Sticker cache moved** to `plugin_sdk/sticker_cache.py` per §A.2.
7. **PluginAPI.outgoing_queue accessor added** (new Task 2.0) per §A.3.
8. **Schedule expectations reset** per §D.

---

## G. Audit's brutal 5-bullet summary (verbatim)

1. **The plan's photo-burst implementation is broken and breaks the assistant-reply path for half the adapters.** Must rewrite Task 2.6.
2. **Sticker cache violates the plugin_sdk boundary** — extensions can't import from `opencomputer.*`. Move it.
3. **PR 6 is 5 days for two features that should probably be scope-cut** — Matrix E2EE 3 days realistic, WhatsApp bridge 5 days realistic. Drop both, ship a 2-day Discord-forum-threads-only PR 6, save ~6 days.
4. **PR 3 is too compressed for subagents.** Split 3 ways, expand task descriptions.
5. **Schedule is 30-50% light.** Tell the user 22-28 days realistic, not 15.5, and hold scope (don't fold ad-hoc tweaks in).

End amendments.
