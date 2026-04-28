# Hermes-style Channel Feature Port — Design Spec

**Date**: 2026-04-28
**Author**: Saksham (with Claude)
**Status**: Approved for implementation; pending writing-plans phase
**Branch (planned)**: `feat/hermes-channel-feature-port`

---

## 1. Goal

Pull the high-value channel adapter and shared messaging-infrastructure features from the Hermes Agent reference repo into OpenComputer, **without** losing the things OpenComputer does better (plugin SDK boundary, F1 consent layer, /steer command, outgoing queue, profile system, centralized session lock + friendly errors in `Dispatch`).

This is **not** a port-everything effort. Hermes is ~30,000 LOC across 21 adapter modules; OC is ~4,500 LOC across 13 adapters. The delta is partly because Hermes targets Chinese platforms (Feishu/DingTalk/WeCom/Weixin/QQ) which we explicitly skip. The remaining delta is real: shared base helpers, format converters, mention-boundary safety, retry semantics, photo-burst handling, reaction lifecycle, and operational hardening.

This spec captures that work and freezes the porting decisions.

---

## 2. Non-goals (will NOT port)

These are deliberate, with rationale:

- **Chinese platforms** (Feishu, DingTalk, WeCom, Weixin, QQ) — ~12k LOC; geographic + language mismatch with the user; no realistic demand.
- **Discord voice-channel join + Whisper STT pipeline** — multi-user team feature; the existing `voice-mode` plugin (PR #199) already covers personal hands-free use via local microphone push-to-talk.
- **Multi-workspace OAuth Slack** — solo user; one-token-per-instance is sufficient.
- **WhatsApp Cloud API removal** — keep as an alternative for users who do have business verification; add the bridge as a sibling plugin.
- **Hermes' per-adapter `_format_user_facing_error` ad-hoc copies** — OC's centralized `Dispatch._format_user_facing_error` is cleaner; do not introduce parallel copies.
- **Hermes' per-adapter session lock** — OC's `Dispatch._locks` is the canonical implementation; per-adapter locks would create double-locking races.

---

## 3. OpenComputer strengths to preserve (explicit invariants)

Any change that violates one of these is a regression and must be reverted:

| # | Invariant | Where it lives | How it's enforced |
|---|---|---|---|
| 1 | `plugin_sdk/*.py` never imports from `opencomputer/*` | `plugin_sdk/` | `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer` |
| 2 | No cross-plugin imports between `extensions/<a>` and `extensions/<b>` | `extensions/` | `tests/test_cross_plugin_isolation.py` |
| 3 | F1 ConsentGate is the single arbiter of capability approvals | `opencomputer/security/consent_gate.py` | All inline-button approval paths must call `ConsentGate.resolve_pending(decision, persist)` — no parallel approval state |
| 4 | Per-chat session lock lives in `Dispatch._locks` | `opencomputer/gateway/dispatch.py` | Adapters do not implement their own per-chat lock |
| 5 | Friendly error mapping lives in `Dispatch._format_user_facing_error` | `opencomputer/gateway/dispatch.py` | Adapters surface raw exceptions; Dispatch translates |
| 6 | Outgoing queue is the cross-process send path | `opencomputer/gateway/outgoing_queue.py` + `outgoing_drainer.py` | Any new "send via channel" entry point must enqueue, not call `adapter.send` directly |
| 7 | Per-profile state is profile-scoped, never shared | `opencomputer/profiles.py` + `<profile_home>/` | Helpers that persist files (e.g. ThreadParticipationTracker) accept `profile_home` as input, never default to `~/.opencomputer/` |
| 8 | Webhook adapter HMAC stays SHA-256 (not Hermes' SHA-1) | `extensions/webhook/tokens.py` | Don't downgrade |
| 9 | Channel adapters' single-instance lock uses `opencomputer.security.scope_lock` | `opencomputer/security/scope_lock.py` | Don't introduce a second locking mechanism |
| 10 | `/steer` mid-run nudge command stays Telegram-specific OR becomes platform-generic via plugin_sdk; never duplicates SteerRegistry | `extensions/telegram/adapter.py` (today) | If generalized, single-source via `plugin_sdk` helper |

---

## 4. Tier 1 — Foundation + adapter wiring (~10 days)

These are the "must ship" items. Each is independently shippable.

### 4.1 Shared base helpers (`plugin_sdk/channel_helpers.py` — NEW)

Port from `gateway/platforms/helpers.py` in Hermes:

- `MessageDeduplicator(max_size=2000, ttl=300s)` — replaces ad-hoc `_seen_messages` dicts in OC's discord/slack/mattermost.
- `TextBatchAggregator(handler, batch_delay=0.6, split_delay=2.0, split_threshold=4000)` — for Telegram/Discord text-batch coalescing.
- `strip_markdown(text)` — pre-compiled regexes; replaces SMS adapter's local `_strip_markdown`.
- `redact_phone(phone)` — country-code + last-4 redaction; for signal/sms/imessage logs.
- `ThreadParticipationTracker(platform_name, profile_home, max_tracked=500)` — persistent set at `<profile_home>/<platform>_threads.json`. **Differs from Hermes**: explicit `profile_home` param (Hermes uses fixed `~/.hermes/`).

**Tests**: `tests/test_channel_helpers.py` — TTL window, expiry refresh, max-size eviction, ttl=0 disables; per-chat aggregation isolation; markdown patterns including fenced/inline code/headers/links; phone redaction E.164 + non-E.164; thread tracker persistence + max bound.

### 4.2 Channel utilities (`plugin_sdk/channel_utils.py` — NEW)

Port from `gateway/platforms/base.py`:

- `utf16_len(s)`, `_prefix_within_utf16_limit(s, budget)`, `_custom_unit_to_cp(s, budget, len_fn)` — UTF-16 budgeting (Telegram's 4096 limit is in UTF-16 units, not codepoints).
- `truncate_message_smart(content, max_length, len_fn=None)` — code-fence-aware splitter. Reopens ` ```lang` across chunks. Protects inline code spans. Appends ` (i/N)` indicator for multi-part replies.
- `SUPPORTED_DOCUMENT_TYPES` — `{".pdf": "application/pdf", ".md": "text/markdown", ".txt": "text/plain", ".log": "text/plain", ".zip": "application/zip", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}`.
- `SUPPORTED_VIDEO_TYPES` — `{".mp4", ".mov", ".webm", ".mkv", ".avi"}`.

**Adapters that adopt this**: telegram, discord, slack, matrix, mattermost, whatsapp (replace per-adapter `_chunk_*` and document allowlists).

**Tests**: `tests/test_channel_utils.py` — UTF-16 boundary, surrogate pair handling, code-fence reopen across split, inline-code protection, multi-part indicator.

### 4.3 Network utilities (`plugin_sdk/network_utils.py` — NEW)

Port from `gateway/platforms/base.py`:

- `_ssrf_redirect_guard(response)` — httpx async response hook; re-validates each redirect target against loopback/private-IP probe.
- `_looks_like_image(data: bytes)` — magic-byte sniffer (PNG, JPEG, GIF, BMP, WEBP). Refuses HTML disguised as image.
- `safe_url_for_log(url, max_len=200)` — strips userinfo/query/fragment.
- `is_network_accessible(host)` — loopback/SSRF probe; DNS-resolution fail-closed.
- `resolve_proxy_url(env_var)` — priority chain: per-platform env → `HTTPS_PROXY/HTTP_PROXY/ALL_PROXY` (upper+lower) → macOS `scutil --proxy` (gated to Darwin only).
- `proxy_kwargs_for_aiohttp(url)` — `aiohttp_socks.ProxyConnector(rdns=True)` for SOCKS, `proxy=` kwarg for HTTP.
- `proxy_kwargs_for_bot(url)` — discord.py / python-telegram-bot proxy kwarg builders.

**Tests**: `tests/test_network_utils.py` — magic-byte rejection of HTML/JSON/empty; SSRF redirect against loopback; proxy chain priority; SOCKS rDNS preserved.

### 4.4 Format converters (`plugin_sdk/format_converters/` — NEW package)

Each is a pure function `convert(text: str) -> str` with parse-error → plain-text fallback.

- `markdownv2.py` — Telegram MarkdownV2 sanitizer with placeholder-protected code fences/links/inline code, table-wrap (`_wrap_markdown_tables`), blockquote/spoiler/strikethrough handling. Final-step regression test: every output must round-trip through Telegram's `parse_mode=MarkdownV2` without 400.
- `slack_mrkdwn.py` — link `[text](url)` → `<url|text>`, `**bold**` → `*bold*`, `*italic*` → `_italic_`, `~~strike~~` → `~strike~`, headers → `*bold*`, blockquote preservation, `&<>` escaping with double-escape avoidance.
- `matrix_html.py` — `markdown` lib if available, regex fallback; outputs `formatted_body` (HTML) and plain `body`. Sanitize URLs.
- `whatsapp_format.py` — `**bold**` → `*bold*`, `__bold__` → `*bold*`, `~~strike~~` → `~strike~`, headers → `*bold*`, links `[text](url)` → `text (url)`. Code-fence + inline-code protection.

**Adapters that adopt this**: telegram (`format_message`), slack (`format_message`), matrix (`_markdown_to_html`), whatsapp (`format_message`).

**Tests**: `tests/test_format_converters.py` — per-converter golden-file tests; parse-error → plain-text fallback; round-trip stability for unchanged-input cases.

### 4.5 BaseChannelAdapter retry + reaction lifecycle

Add to `plugin_sdk/channel_contract.py`:

- Class constant `_RETRYABLE_ERROR_PATTERNS = ("connecterror", "connectionerror", "connectionreset", "connectionrefused", "connecttimeout", "network", "broken pipe", "remotedisconnected", "eoferror")` (read/write timeouts excluded — non-idempotent).
- `_is_retryable_error(exc) -> bool` — matches exception class name or stringified message.
- `_send_with_retry(send_fn, *args, max_attempts=3, base_delay=1.0, **kwargs) -> SendResult` — exponential backoff with jitter; final attempt falls back to plain-text on format errors (`MarkdownV2 parse error`, etc.); on exhaustion returns `SendResult(success=False, error=..., retryable=True)`.
- `on_processing_start(chat_id, message_id)` — default: if `REACTIONS` capability set, calls `send_reaction(chat_id, message_id, "👀")`. Override per-adapter.
- `on_processing_complete(chat_id, message_id, outcome: ProcessingOutcome)` — default: if `REACTIONS`, replaces eyes with `✅` (SUCCESS) / `❌` (FAILURE) / removes (CANCELLED). Override per-adapter.
- `_run_processing_hook(coro)` — swallows hook errors so a hook failure doesn't break message handling.

Add `ProcessingOutcome` enum to `plugin_sdk/core.py`: `SUCCESS, FAILURE, CANCELLED`.

Wire into `Dispatch.handle_message`:
- Before `loop.run_conversation`: schedule `adapter.on_processing_start(chat_id, message_id)` as a fire-and-forget task.
- After (success/failure/exception): `adapter.on_processing_complete(chat_id, message_id, outcome)`.
- Both inside the per-chat lock.

**Tests**: `tests/test_send_with_retry.py`, `tests/test_processing_lifecycle.py` — retry succeeds, retry exhausts, plain-text fallback fires on format error, reaction lifecycle calls happen in order.

### 4.6 Mention-boundary safety (entity-based, OPT-IN gating)

Critical: do NOT change default behavior. Today every group message wakes the bot. Adding mention-required gating without an opt-in flag breaks existing 1:1 chats.

- **Telegram** (`extensions/telegram/adapter.py`): add `_message_mentions_bot(msg, bot_username)` using Telegram's `MessageEntity` types (`mention`, `text_mention`) — never raw substring. Add config `telegram.require_mention: bool` (default False) and `telegram.free_response_chats: list[int]` (chats that bypass require_mention). Add `telegram.mention_patterns: list[str]` (regex wake-words, e.g. `r"\bhermes\b"`).
- **Discord** (`extensions/discord/adapter.py`): use `message.mentions` list (discord.py provides this); add `discord.require_mention` config; add `discord.allow_bots = none|mentions|all` (default `none` — preserves current behavior of ignoring bots); add `discord.allowed_users` ∪ `discord.allowed_roles` OR-semantics.
- **WhatsApp** (`extensions/whatsapp/adapter.py`): use bridge-supplied `mentions[]` array (not text scan). Add `whatsapp.require_mention` config.

**Tests**: `tests/test_telegram_mention_boundaries.py`, `tests/test_discord_allowed_users.py`, `tests/test_whatsapp_mention.py` — substring-but-not-entity rejected; entity-mention accepted; reply-to-bot bypasses gate; `free_response_chats` allowlist; bot allowlist semantics.

### 4.7 Phone redaction in logs (signal / sms / imessage)

Apply `helpers.redact_phone(phone)` in three adapters wherever raw E.164 currently appears in log lines. Adapter-side change only; no SDK change.

### 4.8 Email automated-sender filter

`extensions/email/adapter.py`:
- `_NOREPLY_PATTERNS = re.compile(r"^(noreply|no-reply|donotreply|do-not-reply|postmaster|mailer-daemon|bounce|bounces)@", re.I)`.
- `_AUTOMATED_HEADERS = {"Auto-Submitted", "Precedence", "X-Auto-Response-Suppress", "List-Unsubscribe", "List-Id"}` — presence indicates automated mail.
- `_is_automated_sender(sender_addr, headers)` returns True if either pattern matches; drops the message before agent dispatch (logs at INFO level).

### 4.9 Photo-burst merging (Dispatch)

Port `merge_pending_message_event` semantics to `opencomputer/gateway/dispatch.py`:
- When a `MessageEvent` with `attachments` arrives within `_BURST_WINDOW_SECONDS = 0.8` of another event for the same `session_id`, merge attachments into the in-flight event instead of dispatching a new agent run.
- Skip-merge for `text != ""` events that aren't pure-attachment follow-ups (avoids merging unrelated text into a photo burst).
- Atomic: uses the existing `Dispatch._locks` per-session lock.

**Tests**: `tests/test_dispatch_photo_burst.py` — 5 photos in 0.5s → 1 agent run with 5 attachments; 1 photo + text follow-up after 1s → 2 separate runs; mixed-session events don't cross-pollute.

### 4.10 Sticker vision cache (Telegram)

`extensions/telegram/adapter.py` + new `opencomputer/cache/sticker_cache.py`:
- On inbound sticker: compute key from `file_unique_id`. If cached, inject the cached description as text. If not, defer until vision describe.
- Vision describe path: a duck-typed `vision_describe(image_bytes) -> str` hook on the agent loop or provider. If absent, sticker comes through as `[sticker: ?]`.
- Cache file: `<profile_home>/sticker_descriptions.json` (atomic write tmp + replace; max 5,000 entries; LRU evict).
- Animated stickers (`.tgs` / video): describe the static thumbnail; tag as "animated sticker."

**Tests**: `tests/test_sticker_cache.py` — cache hit short-circuits vision call; cache miss persists after describe; LRU bound; animated sticker tagged.

### 4.11 Webhook `deliver_only` mode

`extensions/webhook/adapter.py`:
- New per-route option `deliver_only: bool` (default False) and `delivery_target: {platform: str, chat_id: str}` (required when `deliver_only=true`).
- When set: webhook payload is rendered via `_render_prompt(template, payload)` (Jinja-style minimal substitution) and sent directly via `outgoing_queue.enqueue(platform, chat_id, body)` — no agent run.
- Validates `delivery_target` reaches a registered adapter at startup; refuses to register the route otherwise.

**Tests**: `tests/test_webhook_deliver_only.py` — webhook → outgoing_queue (no agent invocation); template substitution; missing delivery_target rejected at startup.

### 4.12 Telegram polling fatal cap

`extensions/telegram/adapter.py`:
- Cap 409 retries at 3 attempts × 10s, then surface `_set_fatal_error("telegram-conflict", "another process is polling this bot — stop it or rotate token", retryable=False)`.
- Cap network errors at 10 attempts (5/10/20/40/60/60... up to 60s), then `_set_fatal_error("telegram-network", ..., retryable=True)`.
- Add `_set_fatal_error` method on BaseChannelAdapter (port from Hermes' `_set_fatal_error` + `_notify_fatal_error`); gateway picks it up via `Adapter._fatal_error_*` attributes and decides supervisor restart vs. give up.

**Tests**: `tests/test_telegram_conflict_fatal.py`, `tests/test_telegram_network_fatal.py` — 4th conflict → fatal-non-retryable; 11th network error → fatal-retryable; reset on success.

### 4.13 Bare-local-file auto-detection in agent output

Add to `BaseChannelAdapter`:
- `extract_local_files(content) -> tuple[str, list[Path]]` — detects bare `/path/foo.png` or `~/file.mp4` outside fenced/inline code spans; validates with `os.path.isfile`; returns cleaned text + extracted paths.

Wire into `_process_message_background` (or equivalent `Dispatch.send_response` path): after the agent emits a reply, scan for local file paths, attach them as native media, send the cleaned text alongside.

**Tests**: `tests/test_extract_local_files.py` — bare path outside code; ignored inside ` ``` ` block; ignored inside backticks; non-existent path passed through; relative path NOT extracted (security).

### 4.14 `MEDIA:` and `[[audio_as_voice]]` directives

Add to `BaseChannelAdapter`:
- `extract_media(content) -> tuple[str, list[MediaItem]]` — parses `MEDIA: /path/to/file.mp3` and `[[audio_as_voice]] /path/to/file.ogg` directives in agent output; routes to native voice-send for `[[audio_as_voice]]`.
- Whitelist: png/jpe?g/gif/webp/mp4/mov/avi/mkv/webm/ogg/opus/mp3/wav/m4a/epub/pdf/zip/docx?/xlsx?/pptx?/txt/csv.

**Tests**: `tests/test_extract_media.py` — `MEDIA:` directive routes correctly; `[[audio_as_voice]]` flag chooses send_voice over send_document; quoted paths supported; ext whitelist enforced.

---

## 5. Tier 2 — Operational hardening (~3.5 days)

### 5.1 Telegram IP-fallback transport

Port `gateway/platforms/telegram_network.py` to `extensions/telegram/network.py`:
- `TelegramFallbackTransport(httpx.AsyncBaseTransport)` — sticky-IP retry preserving Host header + TLS SNI = `api.telegram.org`.
- `discover_fallback_ips()` — async DoH queries to `dns.google/resolve` and `cloudflare-dns.com/dns-query`; excludes system-DNS-resolved IPs; falls back to seed `149.154.167.220`.
- `parse_fallback_ip_env(value)` — IPv4-only validator; rejects IPv6/private/loopback/link-local.
- Gated by env `TELEGRAM_FALLBACK_IPS=auto` (DoH discovery) or comma-separated IPs.

### 5.2 Telegram thread-not-found retry

`extensions/telegram/adapter.py`:
- `_is_thread_not_found_error(exc)` — matches `BadRequest("message thread not found")`.
- On send-with-thread-id failure: retry once without `message_thread_id`. Log at WARNING.

### 5.3 Discord allowed_mentions safe defaults

`extensions/discord/adapter.py`:
- `_build_allowed_mentions()` — `everyone=False, roles=False, users=True, replied_user=True`.
- Env overrides: `DISCORD_ALLOW_MENTION_EVERYONE`, `DISCORD_ALLOW_MENTION_ROLES`, `DISCORD_ALLOW_MENTION_USERS`, `DISCORD_ALLOW_MENTION_REPLIED_USER`.

### 5.4 Webhook idempotency cache

`extensions/webhook/adapter.py`:
- Per-route `_seen_deliveries: dict[str, float]` — keyed on `delivery_id` header (or computed hash if absent); 1h TTL eviction.
- Repeat delivery returns 200 with cached response, no agent run.

### 5.5 Slack pause-typing-during-approval

`extensions/slack/adapter.py`:
- When ConsentGate prompts via Slack adapter (Tier 3 5.6 below), call `assistant_threads_setStatus("")` to clear the typing indicator so the user can type their decision in the compose box. Restore on resolve.

### 5.6 Slack format converter wiring

Adopt `format_converters.slack_mrkdwn.convert` in `extensions/slack/adapter.py:format_message`. (Currently sends plain text.)

### 5.7 Matrix HTML format converter wiring

Adopt `format_converters.matrix_html.convert` in `extensions/matrix/adapter.py`. Send both `body` (plain) and `formatted_body` (HTML).

### 5.8 Cross-platform webhook delivery (`cross_platform` mode)

`extensions/webhook/adapter.py`:
- New per-route mode `cross_platform: true`. Routes inbound webhook to `outgoing_queue.enqueue(target_platform, target_chat_id, rendered_body)`.
- E.g.: GitHub webhook → enqueue → Telegram DM. Same flow as `deliver_only` but with template-rendered output.

---

## 6. Tier 3 — Power-user UX (~8 days)

### 6.1 DM Topics + channel-skill bindings (~3 days)

Telegram-specific (Bot API 9.4) but generalizes to Discord channels.

Components:
- `extensions/telegram/dm_topics.py` — wraps `forum_topic_created`/`forum_topic_edited` updates; persists `topic_id → {label, skill, system_prompt}` to `<profile_home>/telegram_dm_topics.json`.
- `BaseChannelAdapter.resolve_channel_prompt(extra, channel_id, parent_id) -> str | None` — per-channel ephemeral system prompt; falls back to thread-parent if not set on thread.
- `BaseChannelAdapter.resolve_channel_skills(channel_id, parent_id) -> list[str]` — returns skill names to auto-load.
- Wire into `AgentLoop.run_conversation` via the existing `RuntimeContext` extras path.
- Telegram-side: `_setup_dm_topics` reads config `telegram.dm_topics: [{label, skill, system_prompt}]`, creates topics via Bot API, persists thread_id back to config.yaml.

CLI:
- `opencomputer telegram topic create --label "Stocks" --skill stock-market-analysis --system "..."` — wraps the Bot API + config update.
- `opencomputer telegram topic list` — shows configured topics.

**Tests**: `tests/test_dm_topics.py`, `tests/test_channel_prompt_resolution.py`.

### 6.2 Matrix E2EE (~1 day)

`extensions/matrix/adapter.py`:
- Optional dep: `mautrix[encryption]` (libolm + bindings). Detect at import.
- If encrypted-room flag is set on inbound: use mautrix's encrypted-event handling for both decrypt-on-receive and encrypt-on-send.
- Crypto state store: `<profile_home>/matrix_crypto/` (libolm session files).
- Device-key verification via `_verify_device_keys_on_server` + `_reverify_keys_after_upload` (port from Hermes).

**Tests**: `tests/test_matrix_e2ee.py` — sends encrypted payload to encrypted room; falls back to plain on unencrypted room; rejects unverified device with WARNING.

### 6.3 WhatsApp Node.js bridge (~2 days)

NEW plugin `extensions/whatsapp-bridge/` (separate from existing `extensions/whatsapp/` Cloud API).

- Bridge stack: Baileys (Node.js, `npm install @whiskeysockets/baileys`).
- Subprocess management: spawn Node bridge on first connect; HTTP API on `127.0.0.1:3001`; long-poll `/messages` for inbound; POST `/send` for outbound.
- `_kill_port_process(port)` + `_terminate_bridge_process` — cross-platform kill (Windows `taskkill /T /F` vs POSIX `killpg(SIGTERM/SIGKILL)`).
- QR code login flow: on first connect, print QR to terminal (and emit to dispatch as a system message) — user scans with WhatsApp mobile app.
- Echo suppression via `recentlySentIds` on Node side.

**Tests**: `tests/test_whatsapp_bridge.py` — bridge subprocess lifecycle; QR emission; echo suppression; cross-platform kill semantics (mocked subprocess module).

### 6.4 Discord forum threads + auto-thread (~2 days)

`extensions/discord/adapter.py`:
- `_thread_parent_channel`, `_resolve_interaction_channel`, `_create_thread`, `_auto_create_thread`, `_handle_thread_create_slash`, `_dispatch_thread_session`, `_send_to_forum`, `_forum_post_file`, `_format_thread_chat_name`, `_get_parent_channel_id`, `_is_forum_parent`, `_get_effective_topic`.
- Slash command tree: `/ask /reset /status /stop /steer /queue /background /side /title /resume /usage /thread`.
- Sync policy: `DISCORD_COMMAND_SYNC = safe|bulk|off` (default `safe` — diff/recreate; `bulk` = overwrite; `off` = skip).
- Channel-skill bindings via `_resolve_channel_skills` (shared with DM Topics work above).

**Tests**: `tests/test_discord_threads.py`, `tests/test_discord_slash_commands.py`.

---

## 7. Module layout (new files, summary)

```
plugin_sdk/
  channel_helpers.py          NEW — MessageDeduplicator, TextBatchAggregator, strip_markdown, redact_phone, ThreadParticipationTracker
  channel_utils.py            NEW — utf16_len, truncate_message_smart, SUPPORTED_DOCUMENT_TYPES, SUPPORTED_VIDEO_TYPES, MessageType, ProcessingOutcome
  network_utils.py            NEW — _ssrf_redirect_guard, _looks_like_image, safe_url_for_log, resolve_proxy_url, proxy_kwargs_for_*
  format_converters/
    __init__.py               NEW
    markdownv2.py             NEW — Telegram MarkdownV2
    slack_mrkdwn.py           NEW — Slack mrkdwn
    matrix_html.py            NEW — Matrix HTML
    whatsapp_format.py        NEW — WhatsApp syntax
  channel_contract.py         EDIT — add _send_with_retry, on_processing_*, _is_retryable_error, _set_fatal_error, extract_local_files, extract_media, resolve_channel_prompt, resolve_channel_skills
  core.py                     EDIT — add ProcessingOutcome enum

opencomputer/
  cache/
    sticker_cache.py          NEW — file_unique_id → description LRU
  gateway/
    dispatch.py               EDIT — wire on_processing_*, photo-burst merge

extensions/
  telegram/
    adapter.py                EDIT — mention boundaries, MarkdownV2 converter, _send_with_retry, fatal cap, sticker_cache integration
    network.py                NEW — TelegramFallbackTransport (Tier 2)
    dm_topics.py              NEW — topic create/list/persist (Tier 3)
  discord/
    adapter.py                EDIT — message.mentions, allowed_mentions, ALLOW_BOTS, allowed_roles
    threads.py                NEW — forum-thread + slash-command logic (Tier 3)
  slack/
    adapter.py                EDIT — slack_mrkdwn converter, pause-typing-during-approval
  matrix/
    adapter.py                EDIT — matrix_html converter, optional E2EE
  whatsapp/
    adapter.py                EDIT — bridge-mention parsing, whatsapp_format converter
  whatsapp-bridge/            NEW PLUGIN (Tier 3) — Baileys subprocess bridge
    adapter.py
    plugin.py
    plugin.json
    bridge_supervisor.py
  signal/adapter.py           EDIT — phone redaction
  sms/adapter.py              EDIT — phone redaction, helpers.strip_markdown
  imessage/adapter.py         EDIT — phone redaction
  email/adapter.py            EDIT — automated-sender filter
  webhook/
    adapter.py                EDIT — deliver_only mode, idempotency cache, cross_platform mode

tests/
  test_channel_helpers.py     NEW
  test_channel_utils.py       NEW
  test_network_utils.py       NEW
  test_format_converters.py   NEW
  test_send_with_retry.py     NEW
  test_processing_lifecycle.py NEW
  test_dispatch_photo_burst.py NEW
  test_sticker_cache.py       NEW
  test_extract_local_files.py NEW
  test_extract_media.py       NEW
  test_telegram_mention_boundaries.py    NEW
  test_telegram_conflict_fatal.py        NEW
  test_telegram_network_fatal.py         NEW
  test_telegram_thread_fallback.py       NEW
  test_telegram_fallback_transport.py    NEW (Tier 2)
  test_dm_topics.py                      NEW (Tier 3)
  test_matrix_e2ee.py                    NEW (Tier 3)
  test_whatsapp_bridge.py                NEW (Tier 3)
  test_discord_threads.py                NEW (Tier 3)
  test_discord_allowed_users.py          NEW
  test_webhook_deliver_only.py           NEW
  test_webhook_idempotency.py            NEW (Tier 2)
  test_webhook_cross_platform.py         NEW (Tier 2)
  test_email_automated_filter.py         NEW
  test_phone_redaction.py                NEW

# Approximate counts: 17 new files in plugin_sdk + opencomputer + extensions/whatsapp-bridge,
# 20+ new test files, ~150 new tests, ~+3500 LOC, ~-300 LOC dedup.
```

---

## 8. Risk analysis

| # | Risk | Mitigation |
|---|---|---|
| R1 | Mention-gating breaks existing 1:1 chats | Default `require_mention=False`; opt-in via config |
| R2 | Format converter parse error → user sees raw markdown | Each converter has plain-text fallback; tested end-to-end |
| R3 | Photo-burst merge cross-pollutes sessions | Lock per `session_id`; merge only same-session events; tested |
| R4 | E2EE adds heavy dep (libolm) | Optional extra `pip install opencomputer[matrix-e2ee]`; adapter degrades gracefully if missing |
| R5 | WhatsApp bridge runs Node subprocess — supply chain risk | Pin Baileys version; subprocess sandboxed; bridge stops if account banned |
| R6 | F1 ConsentGate parallel-approval drift | Code review checklist: every new approval-button path MUST end at `ConsentGate.resolve_pending` |
| R7 | Plugin SDK boundary regression (helper imports `opencomputer/*`) | Existing test enforces this; will catch at PR time |
| R8 | Cross-plugin import (e.g. one adapter imports another's helpers) | Existing `test_cross_plugin_isolation.py` enforces |
| R9 | Sticker vision cache balloons to GB | LRU bound 5,000 entries; ~1KB per description = ~5MB max |
| R10 | Telegram fallback IPs go stale | DoH-rediscovery on connect; seed IP as last-ditch |
| R11 | Reaction-lifecycle calls hit rate limits | Hooks are fire-and-forget; failure swallowed in `_run_processing_hook` |
| R12 | Email automated-filter false positives | Dropped messages logged at INFO with reason; user can disable via env |
| R13 | DM Topics persisting thread_id back to config.yaml conflicts with concurrent edits | Use `flock` (already a known latent debt per CLAUDE.md §5.4) |
| R14 | WhatsApp bridge QR code needs to be displayed somewhere | Print to gateway stdout AND dispatch as a system MessageEvent so a Telegram-only user sees it |

---

## 9. Phasing / PR plan

Six PRs, ordered for dependency. Each PR has its own commit boundary; per Saksham's phase-workflow rule (review → push → next phase).

| PR | Scope | Days | Depends on |
|---|---|---|---|
| **PR 1** | `plugin_sdk/channel_helpers.py` + `channel_utils.py` + `network_utils.py` + `format_converters/` + their tests | 2 | — |
| **PR 2** | `BaseChannelAdapter._send_with_retry` + reaction lifecycle + `extract_local_files` + `extract_media` + Dispatch wiring + photo-burst | 1.5 | PR 1 |
| **PR 3** | Adapter wiring: telegram (mention + MarkdownV2 + retry + fatal cap + sticker cache), discord (mention + retry), whatsapp (mention + format), slack (mrkdwn), matrix (html), email (automated-sender), signal/sms/imessage (phone redact), webhook (deliver_only) | 2.5 | PR 2 |
| **PR 4** | Tier 2: Telegram IP-fallback, thread-not-found retry, Discord allowed_mentions, webhook idempotency, webhook cross_platform, slack pause-typing | 1.5 | PR 3 |
| **PR 5** | Tier 3a: DM Topics + channel-skill bindings + `resolve_channel_prompt` | 3 | PR 3 |
| **PR 6** | Tier 3b: Matrix E2EE + WhatsApp bridge + Discord forum threads | 5 | PR 3 |

Total: 6 PRs, ~15.5 days of focused work. With opus subagents in parallel, calendar elapse target: 8-10 days.

---

## 10. Test plan

- All existing tests (~885) must pass after each PR.
- New tests per Section 7: ~150 added across PRs 1-6.
- Continuous: `ruff check plugin_sdk/ opencomputer/ extensions/ tests/` clean.
- Per-PR: full `pytest tests/` runs; specific new tests added must be listed in PR description.
- Smoke: at PR 3 boundary, run `opencomputer gateway` against a real Telegram bot for 24h; verify mention-gating opt-in default unchanged behavior; verify reaction lifecycle visible.
- F1 audit: `python -c "from opencomputer.security.consent_gate import ConsentGate; ..."` round-trip after PR 6 to verify no parallel approval state was introduced.

---

## 11. Open questions (none blocking — flagged for plan-phase)

1. Should `format_converters` be a sub-package of `plugin_sdk` or a sibling top-level package? Default: sub-package (avoids polluting top-level namespace; preserves boundary contract).
2. Sticker vision describer: which provider? Default: try the agent's existing vision provider via `provider.describe_image(bytes)` if available; else mark as `[sticker: ?]`. Concrete provider integration is not in this spec — generic hook only.
3. WhatsApp bridge: bundle Node.js install instructions in README, or assume user installs Node themselves? Default: assume user; document as prereq.
4. Photo-burst window: 0.8s (Hermes default) — confirm via dogfood. If user finds it too long/short, make configurable.

---

## 12. References

- OC channel inventory: `/tmp/oc-channel-inventory.md`
- Hermes channel inventory: `/tmp/hermes-channel-inventory.md`
- Hermes source: `/Users/saksham/Vscode/claude/sources/hermes-agent-2026.4.23/gateway/platforms/`
- Brainstorming skill: `~/.claude/plugins/cache/claude-plugins-official/superpowers/5.0.7/skills/brainstorming/SKILL.md`

---

## 13. Approval & next steps

This spec is approved by Saksham (verbal: "do everything one by one") on 2026-04-28.

Next step: invoke `superpowers:writing-plans` skill to convert this design into a detailed implementation plan with task graph, then run self-audit, then `superpowers:executing-plans`.
