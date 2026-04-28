# Hermes Channel Feature Port — Plan Audit

**Auditor:** independent expert critic
**Date:** 2026-04-28
**Plan reviewed:** `/Users/saksham/Vscode/claude/OpenComputer/docs/superpowers/plans/2026-04-28-hermes-channel-feature-port.md` (3,201 lines)
**Spec reviewed:** `/Users/saksham/Vscode/claude/OpenComputer/docs/superpowers/specs/2026-04-28-hermes-channel-feature-port-design.md` (459 lines)
**OC source verified at:** `plugin_sdk/channel_contract.py`, `plugin_sdk/core.py`, `plugin_sdk/CLAUDE.md`, `opencomputer/gateway/dispatch.py`, `opencomputer/gateway/server.py`, `opencomputer/gateway/outgoing_drainer.py`, `opencomputer/plugins/manifest_validator.py`, `extensions/{telegram,webhook,whatsapp,discord,slack,matrix}/adapter.py`.

Verdict: the plan is well structured at the spec→PR boundary level (good PR phasing, sensible TDD cadence, the invariants table from §3 of the spec is genuinely respected by most tasks). But it has **enough concrete bugs in the embedded code that Tasks 1.1, 1.2, 1.5, 2.1, 2.2, 2.4, 2.6 will not work as written** — the next agent will hit failures within the first hour. Several major architectural assumptions are also mis-aligned with OC reality (frozen MessageEvent, PluginManifest validator, photo-burst placement, schedule). Schedule estimates are 30–50 % light.

Detailed findings below in Anthropic-style severity buckets.

---

## 1. Executive summary

- **Multiple plan-as-written code blocks will fail at runtime or test time.** The most damaging are: (a) `MessageEvent` is `@dataclass(frozen=True, slots=True)` so `pending._replace(...)` and reconstruction in Task 2.6 won't work; the `_replace`/branch is unreachable and the fallback path mutates a frozen attribute; (b) Task 1.1.5 imports `asyncio` at the top of the test file but the `import asyncio` line is buried inside an inline appended block that the plan never marks as needing to live at module scope; (c) Task 2.6's Dispatch refactor renames `handle_message` semantics from "returns the assistant text" to "fire-and-forget" without updating `BaseChannelAdapter.handle_message` (which awaits the return) — this silently breaks every adapter that relies on the legacy reply-from-handler path (telegram, webhook, etc.).
- **The plan's photo-burst design re-architects Dispatch in a way that breaks the existing return contract.** The current `Dispatch.handle_message` returns `str | None` (the assistant reply text), and `BaseChannelAdapter.handle_message` (the wrapper adapters call) `await response = await self._message_handler(event)` then `await self.send(...)`. Task 2.6's `_dispatch_after_burst_window` schedules dispatch as a side-effect task without piping the reply back. This is silently broken for every adapter that uses the simple "return-text" path (slack, mattermost, signal, sms, imessage, email, plus the test suite). Telegram and Discord have their own send paths so they accidentally survive — but the plan does not call this out.
- **Plan does NOT fix the four broken manifests.** OC's `manifest_validator.PluginManifestSchema` uses `extra="forbid"` (line 124) and does not declare a `capabilities` field. Confirmed: `extensions/ambient-sensors/plugin.json`, `browser-control/plugin.json`, `skill-evolution/plugin.json`, `voice-mode/plugin.json` all carry `"capabilities": [...]`. Discovery currently rejects these and logs `WARNING invalid manifest ... — Extra inputs are not permitted`. The hermes-port plan inherits this breakage and the new `extensions/whatsapp-bridge/plugin.json` (PR 6) is likely to repeat the pattern.
- **The mention-gating opt-in claim is correctly framed but tests miss the regression case.** Plan §R1 promises default `require_mention=False` preserves behavior. Subagents will only add new tests; nothing in the plan adds a regression test that asserts an existing 1:1 chat **without** the `require_mention` config still gets through after the change — the claim that "the existing test suite still cover the default path" is asserted but not verified.
- **Schedule is unrealistic.** 15.5 days for ~50 tasks plus 6 PRs is plausible for greenfield code but two items are clearly under-budgeted: (a) Matrix E2EE = "1 day" is 2-3 days realistic on macOS (libolm builds + device verification flow + crypto-store fixtures); (b) WhatsApp bridge = "2 days" is 4-5 days realistic (Node subprocess management, QR flow piping into dispatch, cross-platform kill semantics with mocks, supply-chain pinning, 24h dogfood). Realistic total: **22-28 days focused work**, not 15.5.

---

## 2. Critical issues (must-fix before execution)

### C1. Task 2.6 photo-burst code is wrong — `MessageEvent` is frozen.

Plan line ~2673 (Task 2.6.2 implementation):

```python
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
```

Problems:
1. `MessageEvent` is `@dataclass(frozen=True, slots=True)` per `plugin_sdk/core.py:103`. It does NOT have a `_replace` method (that's a `NamedTuple` API). `hasattr(pending, "_replace")` is False, so the `else` branch runs.
2. The `else` branch builds a new `MessageEvent` — that's fine in isolation.
3. But the conditional `_replace`/MessageEvent expression is pinned to the `_burst_pending[session_id] = ...` rebind. Subsequent `pending` references retain the old reference.
4. More importantly, attachments and metadata fields on a frozen+slotted dataclass are themselves a `list` and `dict` respectively — mutations to `pending.attachments` would work (lists are mutable even when frozen-hosted) **but the plan code rebuilds rather than mutates**, so the `merged_attachments` list is correctly substituted only on rebind. OK in this branch — but document that the rebind happens.

**Fix:** delete the `_replace` branch entirely; always rebuild via `dataclasses.replace(pending, attachments=merged_attachments, metadata=merged_meta)`. `dataclasses.replace()` works on frozen+slots dataclasses. The plan should use that.

```python
import dataclasses
self._burst_pending[session_id] = dataclasses.replace(
    pending,
    attachments=merged_attachments,
    metadata=merged_meta,
)
```

### C2. Task 2.6 silently breaks the simple-adapter reply path.

`BaseChannelAdapter.handle_message` (plugin_sdk/channel_contract.py:108-114):

```python
async def handle_message(self, event: MessageEvent) -> None:
    if self._message_handler is None:
        return
    response = await self._message_handler(event)
    if response:
        await self.send(event.chat_id, response)
```

The `_message_handler` is `Dispatch.handle_message`, which currently `return result.final_message.content or None` (dispatch.py:270). The plan refactor (Task 2.6.2) makes `handle_message` schedule a delayed dispatch task and return None **before** the agent runs:

```python
self._burst_tasks[session_id] = asyncio.create_task(
    self._dispatch_after_burst_window(session_id)
)
```

The function falls off the end with no return → returns `None` → adapter's wrapper sees `None` → adapter never calls `self.send(...)` → **the agent reply never ships**.

This silently breaks: webhook adapter (uses `await self.handle_message(event)` then returns the dispatch result to caller), email, signal, sms, mattermost, slack, matrix, imessage — every adapter that doesn't have its own send path inside the handler loop.

**Fix:** photo-burst merging must NOT be moved to be the handle_message return path. Two options:

(a) **Keep merging in Dispatch but as a side-channel `pre_dispatch_merge_attachments(event)` step that mutates the event in place (or returns an updated event) BEFORE the existing single-event flow runs.** Race condition cost: still need the per-session timer + cancellation. Reply path stays intact.

(b) **Move photo-burst merging up into BaseChannelAdapter (the Hermes location).** Pros: per-platform tuning; adapters know their attachment shape. Cons: code duplicated across adapters. The spec §4.9 chooses Dispatch; but the plan's implementation does not actually achieve the spec without breaking the reply path.

Recommend (a). Rewrite Task 2.6.2 so:
- `handle_message` first checks "is there a pending burst we can join?" If yes, await a future that the eventual dispatch completes; both join points return the same string; no in-flight work duplicated.
- Or use an explicit "deferred dispatch" model: the FIRST event for the session creates a Future, joining events register callbacks, dispatch resolves the future for everyone. Considerably more complex.

Simpler alternative: wait `_burst_window_seconds` BEFORE invoking `loop.run_conversation`, with cancellation if a follow-up "pure attachment" event for the same session arrives. The cancelled call's caller awaits the new dispatch's result (chained future). Still complex; document carefully.

The plan as written IS NOT viable. This must be redesigned before execution.

### C3. Plan ignores broken manifests and may add another.

OC currently has 4 plugin manifests carrying `"capabilities": [...]` (ambient-sensors, browser-control, skill-evolution, voice-mode). `manifest_validator.PluginManifestSchema` (line 124) sets `extra="forbid"` — every one of these silently fails discovery at load time. Confirmed by reading `manifest_validator.py:121-170` — there is NO `capabilities` field in the schema.

The plan's PR 6 adds `extensions/whatsapp-bridge/plugin.json`. There's no instruction telling the subagent "do not add a `capabilities` field." Expected outcome: subagent will add `capabilities: ["whatsapp.send", ...]` because that's the natural pattern, and the new plugin will fail discovery silently the same way.

**Fix:** add a Pre-flight task before PR 1:

> **Step 0.4: Verify and document plugin manifest schema constraints.**
> Read `opencomputer/plugins/manifest_validator.py:121-198`.
> The `PluginManifestSchema` uses `extra="forbid"` and does NOT accept a `capabilities` field. Any new manifest must NOT include `capabilities`. (Capability declaration for F1 ConsentGate happens at the **tool** level via `BaseTool.capability_claims`, not at the manifest level.)
>
> **Step 0.5: Decide whether to fix the four broken manifests** (ambient-sensors, browser-control, skill-evolution, voice-mode) — strip `capabilities` field — as part of PR 1 or as a separate cleanup PR. Default decision: separate cleanup PR (cheap latent debt fix; not in scope of hermes-port).

### C4. Mention-gating opt-in default is asserted but not regression-tested.

Plan Task 3.1.1 tests cover: substring rejected, entity accepted, free-response chats bypass, reply-to-bot bypass, default `require_mention=False`. Missing:

- **Default-config 1:1 chat regression test.** With NO `require_mention` config key, an inbound 1:1 message that has no entities and no `reply_to` — does `_should_process_message` return True? The new code path:

```python
def _should_process_message(self, msg: dict) -> bool:
    if not self._require_mention:
        return True   # <-- early exit; OK
```

is correct, but the test plan doesn't assert this. Risk: a future refactor flipping the default to True silently breaks every existing user's 1:1 chats. The CLAUDE.md notes "Always check skills/MCP tools before answering" — same principle applies to default-preservation tests.

**Fix:** add a test in Task 3.1.1:

```python
def test_default_no_config_passes_through_normal_message():
    """Regression: default config (no require_mention key) MUST allow plain text in 1:1."""
    adapter = TelegramAdapter({"bot_token": "x"})
    msg = {"text": "hi there", "entities": [], "chat": {"id": "u1"}}
    assert adapter._should_process_message(msg) is True

def test_default_no_config_passes_group_message_too():
    adapter = TelegramAdapter({"bot_token": "x"})
    msg = {"text": "hello bot", "entities": [], "chat": {"id": -1001}, "from": {"id": 7}}
    assert adapter._should_process_message(msg) is True
```

Without these, the test suite "passes" while the default is silently flipped.

### C5. F1 ConsentGate inline-approval interaction is not analyzed for PR 2/3.

Spec §3 invariant I3 is: F1 ConsentGate is the single arbiter; "all inline-button approval paths must call `ConsentGate.resolve_pending(decision, persist)` — no parallel approval state." The existing telegram adapter already has this wired (extensions/telegram/adapter.py:777 `set_approval_callback`, dispatch.py:163-169 `register_adapter`).

PR 2 adds reaction-lifecycle hooks (`on_processing_start` / `on_processing_complete`). These run **fire-and-forget** as `asyncio.create_task(self._safe_lifecycle_hook(...))` (plan line ~2117). Question: what if a reaction hook runs concurrently with a `_send_with_retry` invocation already in flight (PR 2.1)?

Specifically:
- Telegram's `send_reaction` is a separate API call — it doesn't conflict with `sendMessage`.
- BUT both calls go through the same `httpx.AsyncClient` (`self._client`). Connection-pool contention is fine.
- BUT if the client is mid-retry with a `await asyncio.sleep(delay)` for a 5-second backoff, the reaction can arrive at the user **after** the eventual reply, looking weird. Plan doesn't acknowledge this ordering.

The plan also doesn't analyze the `_send_with_retry` interaction with consent-prompt flows. The pre-existing `send_approval_request` is called from `Dispatch._send_approval_prompt` which itself is registered as a `prompt_handler` on `ConsentGate`. If the gate prompts at moment X, the reaction-lifecycle hook is also firing with `on_processing_start` at moment X (it's part of the same per-chat lock window), so the user sees:

1. `on_processing_start` posts 👀
2. F1 prompts inline buttons "Allow once / Allow always / Deny"
3. User clicks
4. Reaction transitions to ✅/❌ via `on_processing_complete`
5. Reply lands

Order seems OK. But test coverage in `test_processing_lifecycle.py` does NOT verify this scenario — there's no integration test for "lifecycle hook + concurrent F1 approval prompt." This is a gap.

**Fix:** add an integration test in PR 3 specifically:

```python
@pytest.mark.asyncio
async def test_processing_lifecycle_during_consent_prompt():
    # Mock ConsentGate that prompts mid-conversation
    # Assert: 👀 reaction lands BEFORE consent prompt
    # Assert: ✅ reaction lands AFTER consent resolution + agent reply
    # Assert: no parallel decision state — gate.resolve_pending called exactly once
    ...
```

### C6. Cross-plugin import contract is not blocked at the test for plan-introduced modules.

`tests/test_cross_plugin_isolation.py:76` enforces no cross-plugin imports today. The plan adds:
- PR 3.9 Slack adopts `format_converters.slack_mrkdwn` (good — `plugin_sdk` import, allowed).
- PR 3.5 Telegram sticker cache imports `opencomputer/cache/sticker_cache.py` — that's `opencomputer.*`, NOT `plugin_sdk.*`. Adapter plugins MUST NOT import from `opencomputer.*` directly per plugin_sdk/CLAUDE.md "What lives in plugin_sdk/" boundary. The plan violates this.

Confirmed: spec §4.10 says "new `opencomputer/cache/sticker_cache.py`" and the adapter calls into it. But adapters live in `extensions/telegram/`, which CLAUDE.md describes as "plugin code" — it must use only `plugin_sdk/*`.

**Fix:** put the sticker cache in `plugin_sdk/cache/sticker_cache.py` (or just `plugin_sdk/sticker_cache.py`). Then telegram adapter can import it. Or: keep the cache in `opencomputer/` but expose an interface via `PluginAPI` that adapters call.

Subagent will not catch this because the plan's task description says `opencomputer/cache/sticker_cache.py` directly. Move the file location, or add a `PluginAPI.get_sticker_cache()` accessor.

---

## 3. High-priority issues (should-fix during execution)

### H1. `_send_with_retry` interaction with `connect_timeout` is wrong.

Plan Task 2.1.2 implementation:

```python
def _is_retryable_error(self, exc: BaseException) -> bool:
    cls = type(exc).__name__.lower()
    if "timeout" in cls and "connect" not in cls:
        return False
    if any(p in cls for p in self._RETRYABLE_ERROR_PATTERNS):
        return True
    msg = str(exc).lower()
    return any(p in msg for p in self._RETRYABLE_ERROR_PATTERNS)
```

The patterns include `"connecttimeout"` AND the timeout-exclusion is `"timeout" in cls and "connect" not in cls`. Good.

But: httpx raises `httpx.ReadTimeout` and `httpx.WriteTimeout` (cls `"ReadTimeout"`, `"WriteTimeout"`). The cls.lower() = `"readtimeout"` / `"writetimeout"` — neither contains "connect" — exclusion path returns False (NOT retryable). Good.

But: what about `httpx.ConnectTimeout`? cls.lower() = `"connecttimeout"` — contains BOTH "timeout" AND "connect" — exclusion does NOT fire. Good.

So this is OK on the timeout side. But test at line ~1875:

```python
async def test_send_with_retry_does_not_retry_timeout():
    async def fn(*a, **kw):
        raise TimeoutError("read timed out")
    with pytest.raises(TimeoutError):
        await adapter._send_with_retry(fn, "chat", "text", base_delay=0.01)
```

Python's built-in `TimeoutError` cls.lower() = `"timeouterror"` — contains `"timeout"` and NOT `"connect"` — exclusion fires, function returns False, `_send_with_retry` raises. Test passes. OK.

But notice `_RETRYABLE_ERROR_PATTERNS` includes `"connectionerror"` (lowercase, no underscore). Python's built-in `ConnectionError` cls.lower() = `"connectionerror"` — string match works. But `_is_retryable_error` checks `cls` first, then `msg`. So `ConnectionError("timeout reached")` → cls match wins, returns True (retry). That's defensible. But `OSError("network unreachable")` — cls = `"oserror"`, no pattern match in cls; msg = `"network unreachable"`, "network" matches → True (retry). Test asserts this. OK.

However: **`pytest.raises(TimeoutError)`** — this passes if a subclass is raised. If httpx ever raises something that inherits from TimeoutError but the user didn't anticipate, the test masks it. Low risk.

**Fix (minor):** rename `_RETRYABLE_ERROR_PATTERNS` to `_RETRYABLE_ERROR_TOKENS` since they're substring tokens, not full patterns. Cosmetic.

### H2. Photo-burst cancel-pending-on-text-arrival is not implemented.

Spec §4.9 specifies: "Skip-merge for `text != ""` events that aren't pure-attachment follow-ups."

Plan Task 2.6.2 implements:
```python
if event.attachments and not event.text and session_id in self._burst_pending:
    # merge
    return
```

This is "skip-merge if text", which means the new text event gets a fresh dispatch. **But what about the in-flight pending burst task?** The pending photo waits up to 0.8s. If a text event arrives at t=0.4s for the same session, the plan dispatches the text event immediately (correct) BUT the photo dispatch still fires at t=0.8s — so you get TWO agent runs out of order: text response first (delivered ~now), then photo response (delivered at 0.8s+agent-run-time).

Question raised in audit prompt: "what happens when a text-only event arrives mid-burst window? The plan says 'skip merge' but does it cancel the pending dispatch?"

Answer per plan: **No, it does not cancel.** The `_burst_pending[session_id]` task continues. Two dispatches happen.

This is wrong. The user sent photos→text in the same intent ("look at these and tell me about X") — they want ONE agent run with photos+text combined.

**Fix:** when text arrives mid-burst, cancel the pending burst task and merge attachments INTO the text event:

```python
if event.text:  # text or text+attachments event
    if session_id in self._burst_tasks:
        self._burst_tasks[session_id].cancel()
        pending = self._burst_pending.pop(session_id, None)
        if pending:
            event = dataclasses.replace(
                event,
                attachments=list(pending.attachments) + list(event.attachments),
            )
        self._burst_tasks.pop(session_id, None)
    return await self._do_dispatch(event, session_id)
```

This is the actual Hermes behavior, per `gateway/platforms/base.py:merge_pending_message_event`. Plan author appears to have missed this nuance.

### H3. Webhook `deliver_only` outgoing-queue interaction underspecified.

Plan Task 3.15 (line ~3022): "When `deliver_only=true`: render via `_render_prompt` template, enqueue via `outgoing_queue.enqueue(platform, chat_id, body)` — no agent run."

Then PR 4.5 Task 4.5 (line ~3074) does cross_platform mode the same way. Audit prompt asks: "does this correctly bypass agent loop, OR does it require Dispatch.handle_message?"

Reading `outgoing_drainer.py:95-124`, the drainer reads queued rows and calls `adapter.send(msg.chat_id, msg.body)` directly. It does NOT call `Dispatch.handle_message` — so no F1 consent gate, no `_session_id_for`, no per-chat lock. **This is intentional**: enqueue is the cross-process send path, not the inbound-message dispatch path.

But spec §3 invariant I6 says: "Outgoing queue is the cross-process send path... Any new 'send via channel' entry point must enqueue, not call adapter.send directly." Plan respects this.

However, the plan does NOT specify how `outgoing_queue.enqueue` is imported from inside the webhook adapter. Adapters live under `extensions/`. Per plugin_sdk/CLAUDE.md, plugins MUST NOT import from `opencomputer.*`. But `OutgoingQueue` lives at `opencomputer/gateway/outgoing_queue.py`.

**Conflict.** Plan as written would have webhook adapter do:

```python
from opencomputer.gateway.outgoing_queue import OutgoingQueue  # FORBIDDEN
```

Two fixes available:
1. Surface enqueue via `PluginAPI` (the existing pattern). Plan can say: "webhook adapter receives a PluginAPI handle (already does for other things) and calls `api.outgoing_queue.enqueue(...)`."
2. Move the queue interface (the public part — `enqueue` / `mark_*`) into `plugin_sdk/transports.py` (already exists per the file listing) and have the implementation in `opencomputer/gateway/outgoing_queue.py` register itself.

**Recommend (1).** Add a Task 3.15a step: "Extend `PluginAPI` with `outgoing_queue` accessor that returns a thin facade over `OutgoingQueue.enqueue`. Update `extensions/webhook/adapter.py` to use it."

### H4. Sticker cache LRU implementation not specified.

Plan §4.10 (spec) says "max 5,000 entries; LRU evict." Plan §3.5 (line ~2960) says "Create `opencomputer/cache/sticker_cache.py` (LRU JSON file at `<profile_home>/sticker_descriptions.json`, max 5000 entries)."

What it doesn't say:
- LRU on what dimension? Last-read time, or last-write time?
- How is "recency" persisted? A bare `{file_unique_id: description}` dict can't track LRU; you need a list-of-keys + dict, OR an `OrderedDict`, OR a `(file_unique_id, timestamp)` mapping. JSON serialization of `OrderedDict` works in Python 3.12 but is brittle (key ordering is implementation-defined for plain dicts in JSON loads — depends on json.loads ordering, which is insertion-order-preserving in 3.7+).
- Concurrent writes? If two telegram pollers (different bots, same profile) hit the same cache, race conditions on the JSON write. Spec §R13 flags `flock` as a known debt for `config.yaml`; this issue applies equally here. Plan does not flag it.

**Fix:** in Task 3.5, specify:
- Use `OrderedDict[str, str]`. On `get`, `move_to_end()` — that's the LRU recency update. Persist via JSON dump of `list(items())` to preserve order. On load, reconstruct via `OrderedDict(loaded_list)`.
- Atomic write: `tmp + os.replace`. Already a pattern in `ThreadParticipationTracker` (Task 1.1.12). Reuse the helper.
- Document: not concurrency-safe. Single-writer assumption holds in practice (one bot per profile per process; profile lock in 14.E enforces this).

### H5. Schedule realism — Matrix E2EE 1 day and WhatsApp bridge 2 days.

Per audit prompt: "Matrix E2EE 1 day — libolm setup alone usually takes a day on macOS."

Confirmed concern. `mautrix[encryption]` pulls `python-olm`, which builds against a system libolm. On macOS:
- `brew install libolm` first (some users have to debug).
- Some `python-olm` versions require specific libolm minor releases.
- E2EE crypto state needs a fresh device session for each test run (or carefully fixtured) — golden-file tests are messy because session keys rotate.
- Device verification (`_verify_device_keys_on_server`) requires either a mock Matrix homeserver or a real one — `tests/test_matrix_e2ee.py` either mocks at the mautrix level (works, but invasive) or stands up a synapse container in CI (heavy).

Realistic: **2-3 days for a rough cut**, 4-5 days for production-grade with full test coverage.

WhatsApp bridge concerns:
- Subprocess management on macOS vs Linux vs Windows. `taskkill /T /F` on Windows requires `subprocess.Popen(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)`. POSIX `os.killpg(SIGTERM)` requires `start_new_session=True` or `os.setsid`. Cross-platform mocks are tedious.
- Baileys (the Node lib) updates frequently; pin version + add a pin-update CI test or you'll wake up to a breaking change.
- QR code rendering. `qrcode-terminal` Node module prints QR to stdout — fine in TTY mode, garbage in journald.
- Test isolation: a real Baileys connect tries to call WhatsApp's servers. Tests must mock the entire HTTP API on `127.0.0.1:3001`. Mocking a Node subprocess from Python tests is a 1-day task by itself.

Realistic: **4-5 days**, not 2. Doubles the PR 6 budget from 5 to 8-9 days.

### H6. PR 3 has 16 sub-tasks compressed into "follow the same TDD pattern" — high subagent risk.

Plan §3.2-3.16 (lines 2935-3033) compresses to:

```
### Task 3.2: Telegram — MarkdownV2 converter wiring
- Modify `TelegramAdapter.send` to wrap `text` via `format_converters.markdownv2.convert(text)` and pass `parse_mode="MarkdownV2"`. On API error containing "can't parse", retry with plain text + no `parse_mode`.
- Test: `tests/test_telegram_format.py` — sends MarkdownV2; falls back to plain on parse error.
- Commit: `feat(telegram): MarkdownV2 outbound formatting + plain-text fallback`
```

This is too thin for subagent execution. A subagent will:
1. Open `extensions/telegram/adapter.py` (1019 lines).
2. Find `def send`. There may be MULTIPLE send paths (`send`, `send_photo`, `send_document`, `send_voice`, `send_approval_request`, ...). Plan doesn't say which to wrap.
3. Add the MarkdownV2 conversion. Discover the message uses `parse_mode="MarkdownV2"` already in some paths (existing). Discover `send_photo` has a `caption` field that ALSO needs MarkdownV2. Plan doesn't mention captions.
4. Implement plain-text fallback. Discover Telegram's "can't parse" error message is `"Bad Request: can't parse entities: ..."` not `"can't parse"`. Tests pass with the wrong substring.

Subagents will need to re-read Hermes source for each task. The plan claims "subagents won't need to re-read Hermes" but the plan-level summaries lose too much detail.

**Fix:** for tasks 3.2 through 3.16, expand each task to include:
- Exact target methods (e.g. "wrap `send`, `send_photo`'s `caption`, `edit_message` — list of 3 methods").
- Exact error string to match ("Bad Request: can't parse entities").
- The original Hermes file:line to mirror.

This expansion adds ~300 lines to PR 3 section but unblocks subagent execution. Without it, expect 30-50% of PR 3 subagent runs to require rework.

### H7. PR 3 should be split.

PR 3 has 16 sub-tasks across 11 adapter files. PR 3 alone is 2.5 days of compressed work. Realistic: 4 days. Plan §11 PR boundary table says PR 3 = "Adapter wiring (the bulk)" depending on PR 2.

**Recommended split:**
- **PR 3a** — Telegram-only wiring (mention boundaries, MarkdownV2, retry, fatal cap, sticker cache) — 5 tasks, ~1.5 days.
- **PR 3b** — Discord, WhatsApp, Slack, Matrix wiring (mention/format/retry) — 5 tasks, ~1 day.
- **PR 3c** — Long tail: email, signal, sms, imessage, webhook deliver_only — 6 tasks, ~1 day.

Three PRs are easier to review (each ≤ 1.5 days), parallelizable (3a/3b/3c have no shared adapter), and revert-friendly if one breaks.

### H8. Ruff and lint coverage is only spot-checked.

Plan steps run `ruff check` after each task on changed files only (e.g. Step 1.1.14). At PR boundaries (Step 1.6.3, 2.7.1) it runs on the whole tree. But `ruff check` flags `BLE001` (blind exception catch) on patterns like `except Exception:  # noqa: BLE001` that the plan litters around. The `# noqa` directives suppress these — but if ruff config tightens (or someone mistypes `# noqa`), the whole tree turns red.

Verify the existing `pyproject.toml` ruff config disables BLE001 globally OR confirm every embedded `try/except Exception` in the plan has a `# noqa: BLE001`.

I count ~15 raw `except Exception:` in the plan code blocks. Most have the noqa comment. A few (`try` blocks in extract_local_files implementation, one in the lifecycle hook) don't. Sample non-comment instance:

```python
text = _MD_FENCE_RE.sub(lambda m: m.group(1), text)
text = _MD_INLINE_CODE_RE.sub(r"\1", text)
```

(no exception there — fine.)

```python
async def _safe_lifecycle_hook(self, coro) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001
        self._log.debug("lifecycle hook raised", exc_info=True)
```

OK. `ruff` should be happy. Low severity; just verify pre-flight that pyproject's ruff rules aren't tightened.

---

## 4. Medium issues (nice-to-fix)

### M1. Format converters: 4 modules vs single dialect dispatcher.

Plan creates `plugin_sdk/format_converters/{markdownv2,slack_mrkdwn,matrix_html,whatsapp_format}.py`. Each module exports `convert(text) -> str`. Total ~600 lines.

Audit prompt asks: "Could this be one module with `convert(text, dialect)` API?"

**Yes, and it's modestly better.** A unified module exposes:

```python
def convert(text: str, dialect: Literal["markdownv2", "slack", "matrix", "whatsapp"]) -> str:
```

Trade-offs:
- Pro: single import surface; easier to add new dialects (e.g. "discord_md" later).
- Pro: shared placeholder/stash machinery (the placeholder/code-fence stash logic is duplicated 4× across the 4 modules — moving to a single `_stash_code_blocks(text)` helper saves ~50 lines).
- Con: one module bloats to ~700 lines; testing per-dialect needs `pytest.mark.parametrize`.
- Con: Hermes pattern is per-module (each dialect lives next to its adapter); preserving symmetry helps reviewers familiar with Hermes.

**Recommendation:** keep 4 modules but extract a shared `plugin_sdk/format_converters/_common.py` with `_stash_code_blocks`, `_unstash`, the placeholder regex constants. Saves ~50 lines and makes the dialect-specific parts more readable. Plan does NOT mention this.

### M2. Photo-burst window 0.8s default — no test for tunability.

Spec §11 open question: "Photo-burst window: 0.8s (Hermes default) — confirm via dogfood. If user finds it too long/short, make configurable."

Plan does not surface this as configurable. `Dispatch.__init__` hard-codes `self._burst_window_seconds = 0.8`. If the user wants to tune this from config, no path exists.

**Fix:** plumb `_burst_window_seconds` through to the gateway config loader. Add to `cfg.gateway.photo_burst_window: float = 0.8`. One-line change in Dispatch's __init__ to read from cfg. Add a test that asserts `Dispatch(loop, burst_window=0.5)._burst_window_seconds == 0.5`.

### M3. `extract_local_files` security claim is overstated.

Plan Task 2.4.2 implementation:

```python
_BARE_PATH_RE = _re.compile(r"(?<![/\w])(/[^\s`'\"<>]+\.[a-zA-Z0-9]{1,5})(?=\s|$|[.,;:!?])")
```

Comment says "Relative paths NOT extracted (security: prevents path-traversal attacks where the agent emits `./../etc/passwd`)." But:
- Absolute paths can also be path-traversal: agent emits `/etc/passwd` → matches → file exists → attached → **leaks /etc/passwd to user via chat**.
- The "relative paths excluded" claim doesn't address: the agent IS the threat here (a misaligned agent could exfiltrate via attachments). Excluding relative paths doesn't protect against absolute-path leaks.

**Recommendation:** add an attachment allowlist by directory:

```python
ALLOWED_ATTACHMENT_DIRS = (
    Path.home() / "Documents",
    Path("/tmp"),
    Path(profile_home) / "outputs",
    # Or read from cfg.attachments.allowed_dirs
)

def _is_in_allowlist(p: Path) -> bool:
    return any(p.resolve().is_relative_to(d.resolve()) for d in ALLOWED_ATTACHMENT_DIRS)
```

Add a test that asserts `/etc/passwd` is rejected even if it exists. The plan currently does not.

This is a real security gap. Risk depends on threat model, but a minimal mitigation (allowlist via `cfg.attachments.allowed_dirs`) is cheap and worth it.

### M4. Reaction-lifecycle hooks fire-and-forget — observability gap.

Plan Task 2.2.3 wires:
```python
asyncio.create_task(self._safe_lifecycle_hook(
    adapter.on_processing_start(event.chat_id, message_id)
))
```

If the reaction send fails 100% of the time (e.g. bot lacks "manage_reactions" permission on Discord), the user sees no reaction AND no log message. `_safe_lifecycle_hook` logs at DEBUG level — invisible at default INFO.

**Fix:** log at WARNING the FIRST failure per (platform, error-class), then back off to DEBUG for subsequent. Add a `_lifecycle_hook_failures: dict[str, int]` counter on Dispatch.

### M5. `_set_fatal_error` retryable-flag semantics don't match how supervisor consumes them.

Plan Task 2.3 wires `_check_fatal_errors_periodic` in Gateway:

```python
if retryable:
    await adapter.disconnect()
    adapter._fatal_error_code = None  # <-- mutating private state from outside
    ...
    await adapter.connect()
else:
    self._log.error("adapter %s fatal-non-retryable: %s", ...)
```

Issues:
1. Gateway mutates `adapter._fatal_error_code` directly — leaking encapsulation. Should have `adapter.clear_fatal_error()` method.
2. `await adapter.disconnect()` then `await adapter.connect()` — for telegram, `connect()` reacquires the scope_lock. Inside the SAME process the lock won't conflict. But: the polling task was cancelled in `disconnect`; the adapter is in a half-state. `connect()` may fail because httpx client is already aclose'd. Plan doesn't define adapter recovery semantics.
3. Non-retryable: gateway logs ERROR but leaves the adapter in `_adapters` list. The drainer's `adapters_by_platform` dict (server.py:93) was built ONCE at start time — even if the adapter is dead, the drainer keeps trying to send through it.

**Fix:** define an explicit `Adapter._restart()` semantics. Or: on non-retryable, remove adapter from `gateway._adapters` and `drainer.adapters` (would need a lock; drainer is hot-looping).

Punts are OK for v1 (this is operational hardening), but flag as Phase 2 work.

### M6. Pydantic dependency for tests assumed but not pinned.

`pyproject.toml` already pins pydantic for `manifest_validator.py`. The plan imports pytest-asyncio for `@pytest.mark.asyncio` decorator. Confirmed OC has pytest-asyncio per CLAUDE.md "pytest-asyncio must be configured (it already is per OC's setup)" — but this is a hand-waved claim. Verify pyproject.toml dev deps include `pytest-asyncio>=0.23` AND the config defaults to auto mode, OR every test file that uses asyncio marks must declare the mode explicitly.

```bash
grep -A1 "asyncio_mode\|asyncio" /Users/saksham/Vscode/claude/OpenComputer/pyproject.toml
```

Subagent should run this pre-flight. Plan does not.

---

## 5. Low / observations (FYI)

### L1. Plan's "approximate counts" inflate the new test count.

Spec §7 claims `~150 new tests, ~+3500 LOC`. Counting actual test additions in plan code blocks: I see ~115 explicit test functions across 21 new test files. The remaining ~35 are implied in the compressed Tasks 3.2-3.16 / 4 / 5 / 6 task summaries. Subagents may write fewer if the plan says "Test: tests/test_xxx.py — assert X"; one test function per item is the floor.

Realistic count: 100-130 new tests. Not a problem; just framing.

### L2. The "rough"/"focused"/"calendar" day distinction is informal.

Spec §9 says "15.5 days of focused work. With opus subagents in parallel, calendar elapse target: 8-10 days." Plan inherits this. With (a) subagent rework for compressed Tasks 3.2-3.16, (b) Matrix E2EE actual ~3 days, (c) WhatsApp bridge actual ~5 days, the realistic focused-work total is **22-28 days**. Calendar elapse with parallelism: **12-18 days**.

Set expectations correctly to avoid mid-execution scope-cuts driven by deadline pressure.

### L3. `extract_local_files` regex doesn't handle Windows paths.

Pattern: `(/[^\s`'\"<>]+\.[a-zA-Z0-9]{1,5})` — anchored on leading `/`. Windows paths like `C:\Users\Saksham\foo.png` won't match. OC currently runs on Windows post the OI removal (per CLAUDE.md "Cross-platform support extended... to macOS, Linux, Windows"). Bare-path extraction will silently skip Windows paths.

Low priority: Windows users typing absolute paths in chat is rare; the agent could be taught to use `~/` (POSIX-style HOME) instead.

### L4. Test for `truncate_message_smart` indicator format is brittle.

Plan Task 1.2.2:
```python
def test_truncate_message_smart_indicator_appended():
    text = "x" * 250
    chunks = truncate_message_smart(text, max_length=50)
    assert "(1/" in chunks[0]
    last_idx = len(chunks)
    assert f"({last_idx}/{last_idx})" in chunks[-1]
```

Subagent will implement `f"{c} ({i+1}/{n})"` per the spec — but the `assert "(1/" in chunks[0]` pattern matches `"(1/2)"`, `"(1/9)"`, AND `"(1/99)"` etc. Fine. The last assertion `f"({last_idx}/{last_idx})"` requires exactly `(N/N)` as substring. Both work.

But if there are >9 chunks, the indicator overhead may exceed the reserved 10 chars (`"(99/100)"` = 8 chars + space = 9; tight but OK). For >999 chunks, indicator is 11+ chars, exceeds reserve, breaks. Plan caps `indicator_overhead = 10` — correct for ≤99 chunks.

Edge case: a 4096-char Telegram limit divided by ~50 chars per chunk = ~80 chunks for a really long output. Within safe range. Note as comment.

### L5. `MediaItem` is referenced before definition in Task 2.4.

Tests at Task 2.4.3 (line ~2398) import `MediaItem`. The class is defined later in the same task (line ~2440 implementation). Order is preserved when subagent reads top-to-bottom, but if subagent runs the test file first to verify it fails (Step 2.4.X "verify import error"), they'll get an import error of the wrong sort (`MediaItem` from the wrong module path). Cosmetic.

### L6. Plan's PR-creation block uses `--draft` initially. Good. But:

```bash
gh pr edit --add-label hermes-port --body-file - <<'EOF'
... (updated description noting PR 2 included)
EOF
```

`gh pr edit --body-file -` reads from stdin, not from a heredoc literal. The heredoc is fine (`bash` interprets it). But: after PR 1 is merged, PR 2's commit message+description should explain "follows PR #N" — plan doesn't surface that.

Cosmetic. No action.

### L7. "Bear minimum" mention-pattern config (`telegram.mention_patterns: list[str]`) isn't tested.

Spec §4.6 mentions wake-words via regex (`r"\bhermes\b"`). Plan §3.1 doesn't include a test for mention_patterns. Subagent will likely skip the implementation.

Either drop `mention_patterns` from spec (it's a Hermes feature that's already specific to that bot's name; OC users rarely call their bot "hermes"), or add a test.

---

## 6. Suggested plan revisions

### Revisions BEFORE PR 1 starts

1. **Add Step 0.4 — "Read manifest_validator.PluginManifestSchema; document that `capabilities` field is NOT supported. Any new plugin.json must omit it."** (Addresses C3.)

2. **Add Step 0.5 — "Verify pyproject.toml's pytest-asyncio config and ruff BLE001 rule."** (Addresses M6 + L6.)

3. **Replace Spec §3 invariant table with a SUBAGENT INSTRUCTIONS section** that hands a one-paragraph briefing to each subagent BEFORE the task, summarizing the contract that subagent must respect:
   - "Do not import from `opencomputer.*` from inside `extensions/<adapter>/` files. If you need core services (outgoing_queue, sticker_cache), get them via `PluginAPI` (`plugin_api.outgoing_queue`, `plugin_api.sticker_cache`) — these may not exist yet; PROPOSE adding them rather than importing direct."
   - "Do not modify `MessageEvent` or other frozen dataclasses; use `dataclasses.replace()`."
   - "Do not change `Dispatch.handle_message`'s return contract from `str | None` to `None`."

### Revisions to PR 1

4. **Task 1.1.5 (TextBatchAggregator):** add `import asyncio` at the top of the test file. The plan's "Append to tests/test_channel_helpers.py" leaves it ambiguous; explicitly call out the imports section.

5. **Task 1.4 (markdownv2):** add a fuzz-style test that submits 1000 randomly generated markdown strings and asserts the output round-trips through Telegram's `parse_mode=MarkdownV2` (or at minimum: doesn't raise from `escape_mdv2` with control characters). The plan's tests are all positive cases.

6. **Task 1.5 (slack_mrkdwn):** the test:
   ```python
   def test_mrkdwn_no_double_escape():
       assert to_mrkdwn("&amp;") == "&amp;"
   ```
   passes the implementation's negative-lookahead `re.sub(r"&(?!(amp|lt|gt|quot|apos);)", "&amp;", text)`. But entity-references like `&#39;` (numeric) would be double-escaped. Add: `assert to_mrkdwn("&#39;") == "&#39;"` and update the regex if needed.

### Revisions to PR 2

7. **Task 2.6 (photo-burst) — REWRITE.** Per C2, the proposed implementation breaks the reply path. Replace with the cancel-pending-on-text variant per H2. Add a regression test that asserts: `await dispatch.handle_message(text_event_with_no_pending) → returns assistant_text` (i.e. the old contract is preserved).

8. **Task 2.6.2 — fix MessageEvent reconstruction.** Use `dataclasses.replace`, not `_replace`. (C1.)

9. **Task 2.3 (fatal_error):** add `clear_fatal_error()` method. Don't mutate `_fatal_error_code` from outside the adapter. Add a test that asserts `clear_fatal_error()` resets all three private fields.

10. **Task 2.2 (lifecycle hooks):** add an integration test that mocks a fake `_send_with_retry` mid-flight and a concurrent `on_processing_start` reaction call. Verify: ordering is start-reaction → reply-send → complete-reaction. (Addresses C5.)

### Revisions to PR 3

11. **Split PR 3 into PR 3a/3b/3c.** Per H7. PR 3a = telegram (5 tasks); PR 3b = discord+slack+matrix+whatsapp (4 tasks); PR 3c = email+signal+sms+imessage+webhook (5 tasks).

12. **Tasks 3.2-3.16 EXPAND each task.** Per H6. Specifically:
    - Task 3.2 (Telegram MarkdownV2): list `send`, `send_photo.caption`, `edit_message`, `send_approval_request.prompt_text`. Specify error string `"Bad Request: can't parse entities"`.
    - Task 3.5 (sticker cache): move file path from `opencomputer/cache/sticker_cache.py` to `plugin_sdk/sticker_cache.py` (per C6) OR expose via `PluginAPI`.
    - Task 3.6 (Discord mention): include `bot.user.mentioned_in(message)` (the discord.py-canonical check) in addition to scanning `message.mentions`.
    - Task 3.11 (Email automated filter): test `_NOREPLY_PATTERNS` against `Postmaster@example.com` (capital P) — the regex has `re.I` so it should match.

13. **Task 3.1.1 (Telegram mention boundaries):** add Default-config regression tests per C4.

14. **Task 3.15 (webhook deliver_only):** add Task 3.15a — "Extend PluginAPI with `outgoing_queue` accessor; the new code path uses `self._plugin_api.outgoing_queue.enqueue(...)`." Per H3.

### Revisions to PR 4

15. **Task 4.4 (webhook idempotency):** specify what counts as `delivery_id`. GitHub uses `X-GitHub-Delivery`. Stripe uses `Stripe-Signature`. Generic webhooks may not have one. Default to `sha256(body + token_id)` if header absent, with a TTL of 1 hour.

### Revisions to PR 5

16. **Task 5.1 (DM Topics):** add `flock` (per Spec §R13) for `<profile_home>/telegram_dm_topics.json`. The plan flags this as known debt but does not fix it. Use `fcntl.flock(LOCK_EX)` on POSIX; `msvcrt.locking()` on Windows.

### Revisions to PR 6

17. **Task 6.1 (Matrix E2EE):** budget 3 days, not 1. Add a CI-skip flag for E2EE tests that require libolm — they should run only when `pip install opencomputer[matrix-e2ee]` is in the environment.

18. **Task 6.2 (WhatsApp bridge):** budget 4 days, not 2. Add explicit:
    - Task 6.2a — Mock the Node bridge HTTP API in tests (do NOT spawn real subprocesses).
    - Task 6.2b — Cross-platform process kill: test on macOS + linux (CI matrix); document Windows limitation.
    - Task 6.2c — Detect coexistence with existing `whatsapp` (Cloud API) plugin: if both enabled, log a warning and let user choose which one handles which numbers. Currently undefined.

19. **Decision: WhatsApp bridge — defer or scope-cut?** Per audit prompt section 8: "WhatsApp bridge runs Node.js subprocess. That's a big maintenance commitment. Is it actually worth it for a personal agent that already has WhatsApp Cloud API as an alternative?"

    **Recommendation: drop WhatsApp bridge from this port.** Reasoning:
    - User already has WhatsApp Cloud API plugin (extensions/whatsapp/) — works for outbound; inbound via webhook.
    - The Cloud API requires "business verification" but for a personal bot the user can use the test/development WhatsApp Business account (one phone, no verification).
    - Bridge adds: Node.js install, Baileys lib supply chain, subprocess management, QR login, cross-platform kill, ~600 LOC, ~2 weeks of dogfood time.
    - Bridge reward: same WhatsApp surface, just personal-account.
    - **Better path:** scope-cut PR 6.2 entirely. Re-evaluate after dogfood gate (per CLAUDE.md §5 "park until real demand signals").

20. **Decision: Matrix E2EE — defer or scope-cut?** Per audit prompt section 8: "Matrix E2EE adds libolm dep. Is the user actually going to use Matrix?"

    **Recommendation: keep Matrix E2EE in scope but as opt-in `pip install opencomputer[matrix-e2ee]`.** Reasoning:
    - Matrix E2EE without encryption support is uselessly insecure — most modern Matrix rooms are encrypted by default. Adapter without E2EE = "broken" in practice.
    - libolm is a one-time install cost, not ongoing maintenance.
    - 3 days vs nothing: defensible if user actually has a Matrix homeserver. Per user profile "MCP Servers ... Telegram channel installed" — Matrix is currently NOT in heavy use. So timing-wise, defer to post-v1.0 dogfood.

    Net: **scope-cut Matrix E2EE from this port.** Refactor PR 6 to: WhatsApp bridge (if kept) + Discord forum threads only. If WhatsApp bridge also scope-cut, PR 6 is just Discord forum threads = 2 days, not 5.

### Replacement PR plan summary

| PR | Scope | Days (focused) | Ship-gate change |
|---|---|---|---|
| **PR 1** | plugin_sdk modules + format converters + tests | 2 | unchanged |
| **PR 2** | BaseChannelAdapter retry + lifecycle + extract_* + photo-burst (rewritten per C2/H2) + fatal-error | 2 | +0.5 day for photo-burst rework |
| **PR 3a** | Telegram (5 tasks) | 1.5 | new split |
| **PR 3b** | Discord/Slack/Matrix/WhatsApp adapter wiring | 1 | new split |
| **PR 3c** | Email/Signal/SMS/iMessage/Webhook deliver_only | 1 | new split |
| **PR 4** | Tier 2 ops hardening | 1.5 | unchanged |
| **PR 5** | DM Topics + channel-skill bindings (with flock) | 3 | +0.25 day for flock |
| **PR 6** | ~~Matrix E2EE~~ + ~~WhatsApp bridge~~ + Discord forum threads | 2 | from 5 to 2 days |

Total: **14.25 days focused work**, down from 15.5. Calendar with parallelism: **8-10 days** as originally targeted.

If user insists Matrix E2EE + WhatsApp bridge stay in: **22-28 days focused**, **15-20 calendar**.

---

## Appendix A — Verified file:line evidence

- `plugin_sdk/core.py:103-112` — MessageEvent is `@dataclass(frozen=True, slots=True)`. Confirms C1.
- `plugin_sdk/channel_contract.py:108-114` — BaseChannelAdapter.handle_message awaits handler return + sends. Confirms C2.
- `plugin_sdk/channel_contract.py:34-74` — ChannelCapabilities flag enum has REACTIONS but NOT a "fatal-error" or "burst-merge" bit. Plan adds these implicitly.
- `opencomputer/gateway/dispatch.py:186-282` — Dispatch.handle_message current shape; per-chat lock, typing heartbeat, run_conversation, return text. Plan changes return semantics.
- `opencomputer/gateway/dispatch.py:159-161` — Existing F1 ConsentGate prompt_handler wiring. Plan does not break this but tests don't cover the integration.
- `opencomputer/gateway/outgoing_drainer.py:111` — Drainer calls `adapter.send` directly; bypasses Dispatch. Confirms H3 reasoning.
- `opencomputer/plugins/manifest_validator.py:124` — `extra="forbid"` on PluginManifestSchema. Confirms C3.
- `opencomputer/plugins/manifest_validator.py:121-198` — schema fields list does NOT include `capabilities`. Confirms C3.
- `extensions/telegram/adapter.py:777-790` — set_approval_callback already wired. F1 inline-approval path is live.
- `extensions/telegram/adapter.py:792-880` — send_approval_request implementation. Goes through ConsentGate via dispatch._handle_approval_click.
- `extensions/whatsapp/adapter.py:67-75` — current `connect()` returns `None`, not `bool`. Plan's new `connect()` (in `whatsapp-bridge`) should also use `-> bool` per BaseChannelAdapter ABC. Existing whatsapp adapter has a TYPE BUG (`connect(self) -> None` violates `connect(self) -> bool` from base class). Pre-existing; not introduced by plan. FYI for future cleanup.
- `extensions/{ambient-sensors,browser-control,skill-evolution,voice-mode}/plugin.json` — all four carry `"capabilities": [...]`. All four fail discovery (per `manifest_validator.PluginManifestSchema(extra="forbid")`).

---

## Appendix B — Brutal summary in 5 bullets

1. **The plan's photo-burst implementation is broken and breaks the assistant-reply path for half the adapters.** Must rewrite Task 2.6.
2. **Sticker cache violates the plugin_sdk boundary** — extensions can't import from `opencomputer.*`. Move it.
3. **PR 6 is 5 days for two features that should probably be scope-cut** — Matrix E2EE 3 days realistic, WhatsApp bridge 5 days realistic. Drop both, ship a 2-day Discord-forum-threads-only PR 6, save ~6 days.
4. **PR 3 is too compressed for subagents.** Split 3 ways, expand task descriptions.
5. **Schedule is 30-50% light.** Tell the user 22-28 days realistic, not 15.5, and hold scope (don't fold ad-hoc tweaks in).

End audit.
