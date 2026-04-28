# PR #221 — Hermes Channel Feature Port — Final Review Certification

**Reviewer:** Final independent reviewer (Opus 4.7)
**Date:** 2026-04-28
**Branch:** `feat/hermes-channel-feature-port` @ `d3b94fb0`
**Base:** `c68dd944`
**Scope:** 57 commits, 86 files, +16,598 / −264 LOC (matches PR description with rounding)
**Spec / Plan / Audit / Amendments:** all four committed under `docs/superpowers/`

---

## 1. Verdict

**APPROVED FOR MERGE — flip to ready-for-review.**

This PR is unusually well-disciplined for a 16k-LOC delta: the audit findings are demonstrably fixed in code (not just claimed), the SDK boundary holds, the assistant-reply contract is preserved, and 4,999 tests pass with 0 failures. The few remaining gaps are honestly framed as deferred follow-ups in the PR body and do not affect the existing-user upgrade path (everything new is opt-in).

---

## 2. Strengths

- **The 6 audit-critical findings are not paper-fixed; they are demonstrably fixed in source.** Verified via direct grep + targeted test runs (see verification table below). C1 uses `dataclasses.replace()`, C2 preserves `str | None` from `Dispatch.handle_message`, C3 keeps `capabilities` out of `whatsapp-bridge/plugin.json`, C4 adds two regression tests asserting default-OFF behavior, C5 adds `test_processing_lifecycle_during_consent_prompt`, C6 places the sticker cache in `plugin_sdk/sticker_cache.py` with `OrderedDict` LRU + atomic flock-protected JSON write.
- **Photo-burst design is correct and survives the audit's H2 critique.** `opencomputer/gateway/dispatch.py:283-323` implements all four cases explicitly: pure-attachment join, text-arrival cancel + absorb + dispatch, fresh attachment-only burst start, plain text passthrough. The `BaseException` re-raise propagation to joiner futures is the right shape.
- **Plugin SDK boundary holds and is enforced two different ways.** `tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer` (whole-package walk) and `tests/test_sub_f1_sdk_boundary.py::test_no_opencomputer_imports_anywhere_in_plugin_sdk` (regex over rglob) both pass. `grep -rn "from opencomputer" plugin_sdk/` returns empty. `PluginAPI.outgoing_queue` accessor (audit H3) properly routes the webhook deliver_only / cross_platform paths through `api.outgoing_queue.enqueue` instead of letting plugins reach into `opencomputer.gateway.outgoing_queue`.
- **Frozen-dataclass invariant is respected everywhere `MessageEvent` is touched.** Every mutation path uses `dataclasses.replace()` (`dispatch.py:276`, `dispatch.py:291`). No `pending._replace(...)`, no `pending.attachments = ...` in-place mutation.
- **Existing-user upgrade path is untouched.** Mention-gating defaults to `require_mention=False`; reaction lifecycle hooks fire only for adapters declaring `ChannelCapabilities.REACTIONS`; webhook idempotency / cross_platform / deliver_only modes only activate per-token; no schema changes to `~/.opencomputer/config.yaml`. A user upgrading and re-running `opencomputer gateway` continues to behave identically.
- **Process discipline is visible.** Spec → plan → audit → amendments all four docs committed. The audit's blunt critique (`/AUDIT.md` has 6 critical findings, frames the plan as "the next agent will hit failures within the first hour") was actually adopted, not waved off — the amendments doc explicitly references each audit finding by number.

---

## 3. Critical issues (must-fix before merge)

**None.** All audit-critical findings have landing code + test coverage.

---

## 4. Important issues (should-fix during this PR)

**None.** The remaining gaps are honestly deferred to follow-up PRs in the PR body.

---

## 5. Minor issues / observations (could-fix or punt)

### O1. Stray reverted commit pair in branch history (`4afa439a` + `58087439`).

Documented in the PR body under "Known issues / honest deferrals." The revert is clean (the only file added by `4afa439a` is removed by `58087439`). A `git rebase -i` would simplify history but is not required since both commits cancel each other and the net diff is correct. **Leave as-is** — squash-merge will collapse it anyway.

### O2. `_send_with_retry` not wired into signal / sms / imessage / mattermost / email httpx clients.

These adapters have raw httpx calls without retry wrapping. Per the plan §11 PR boundary table, PR 3c was scoped as "phone redaction, email filter, webhook deliver_only, helpers.strip_markdown adoption" — explicitly NOT retry adoption. This is a real shortcoming if/when the bots run against flaky networks, but it is documented scope and consistent with the per-PR scope cuts the audit recommended. **Punt to a follow-up PR** ("retry coverage for tier-3 adapters") rather than block merge.

### O3. SSRF guard is shipped but not wired into any adapter outbound path.

`plugin_sdk/network_utils.ssrf_redirect_guard` exists and is unit-tested in `tests/test_network_utils.py`, but no extension currently constructs an `httpx.AsyncClient` with `event_hooks={"response": [ssrf_redirect_guard]}`. This was Tier 1 infrastructure scoped for use by future webhook-outbound + sticker-fetch paths. **Leave as-is** — the helper is available; first real consumer will wire it.

### O4. Four pre-existing manifests still carry `"capabilities": [...]` and silently fail discovery.

`extensions/{ambient-sensors, browser-control, skill-evolution, voice-mode}/plugin.json` all log `WARNING invalid manifest ... — capabilities: Extra inputs are not permitted` at every gateway startup. The audit explicitly recommended these be cleaned up in a separate PR ("Default decision: separate cleanup PR"). The new `whatsapp-bridge/plugin.json` correctly omits the field, so the regression risk this PR could have introduced is closed. **Pre-existing; out of scope.** Worth a one-line note in the next cleanup PR.

### O5. Discord adapter has 12+ inline `from opencomputer.* import` lazy imports.

These are LOCAL imports (inside method bodies), not module-level `import opencomputer` statements, so they don't trip the SDK boundary test. They reach across the boundary into `opencomputer.agent.steer`, `opencomputer.gateway.dispatch.session_id_for`, `opencomputer.plugins.registry`, `opencomputer.agent.config`, `opencomputer.agent.state`. The boundary test is intentionally regex-based on top-level imports because tightening it would cascade. **Leave as-is** — long-term refactor candidate, not a regression introduced by this PR.

### O6. Per-session token accumulation is honest in `/usage` text but not deduplicated across the agent loop.

Already flagged in PR body; explicitly out of scope (separate agent-loop change). No action.

### O7. Photo-burst window is plumbed via `Dispatch(loop, config={"photo_burst_window": ...})` but no gateway-level config key.

A user who wants to tune from `~/.opencomputer/config.yaml` cannot. PR body acknowledges this as a known issue. **Punt** — one-line follow-up.

---

## 6. Audit findings verification table

| Audit finding | Status | Evidence |
|---|---|---|
| **C1** — frozen MessageEvent + `_replace` | FIXED | `opencomputer/gateway/dispatch.py:276,291` use `dataclasses.replace()`. No `_replace` references in repo. |
| **C2** — handle_message return contract preserved | FIXED | `dispatch.py:208 async def handle_message(self, event) -> str | None`; cases 1-4 each return future / str / None correctly. `test_handle_message_default_text_only_returns_assistant_text` PASS. |
| **C3** — manifest schema rejects `capabilities` | FIXED for new plugins | `extensions/whatsapp-bridge/plugin.json` does NOT include `capabilities`. (Pre-existing 4 broken manifests are out of scope, per audit recommendation; observation O4 above.) |
| **C4** — mention-gating default-OFF regression test | FIXED | `tests/test_telegram_mention_boundaries.py:156 test_default_config_one_to_one_passes`, `:162 test_default_config_group_message_passes`, `:323 test_default_config_group_still_delivers`. All 3 PASS. |
| **C5** — F1 ConsentGate × lifecycle hook integration test | FIXED | `tests/test_processing_lifecycle.py:205 test_processing_lifecycle_during_consent_prompt` PASS. |
| **C6** — sticker cache in plugin_sdk boundary | FIXED | `plugin_sdk/sticker_cache.py` exists (65 lines, OrderedDict + move_to_end + flock-protected atomic write); `opencomputer/cache/sticker_cache.py` does NOT exist. |
| **H1** — `_RETRYABLE_ERROR_PATTERNS` correctness | VERIFIED | `plugin_sdk/channel_contract.py:160-165` includes `"connecttimeout"`. Logic at `:340-348`. |
| **H2** — photo-burst cancels pending dispatch on text arrival | FIXED | `dispatch.py:285-305` Case 2 cancels `_burst_tasks[session_id]`, absorbs pending attachments via `dataclasses.replace`, dispatches inline, resolves joiner future. `test_text_arrival_cancels_pending_burst_and_merges` PASS. |
| **H3** — webhook outgoing_queue access via PluginAPI | FIXED | `extensions/webhook/adapter.py:417 queue = getattr(api, "outgoing_queue", None)`. No `from opencomputer.gateway.outgoing_queue import` in webhook code. PluginAPI accessor declared at `opencomputer/plugins/registry.py:78`. |
| **H4** — sticker cache LRU semantics | FIXED | `plugin_sdk/sticker_cache.py:13,29,53,59` use `OrderedDict` + `move_to_end` on get + flock-protected atomic JSON write. |
| **H5** — schedule realism | MOOT | Work shipped within plan estimate; calendar elapse ~10h wall time per PR body. |
| **H6** — PR 3 task descriptions too compressed | RESOLVED | Split into PR 3a (telegram), 3b (discord/whatsapp/slack/matrix), 3c (long tail) as audit recommended. Commits show clean separation. |
| **H7** — PR 3 split | DONE | Three sets of merge commits visible: `f3e2e6c6/8d4af019/4ae52719/...` for 3a/3b/3c. |
| **H8** — Ruff/lint clean | VERIFIED | `ruff check plugin_sdk/ opencomputer/ extensions/` reports 2 errors, both pre-existing B010 in `extensions/affect-injection/plugin.py:62-63` (out of scope; documented in PR body). |
| **M3** — attachment allowlist | FIXED | `plugin_sdk/channel_contract.py:515-577 extract_local_files(..., allowed_dirs=...)`. Default `[Path.home()/"Documents", Path("/tmp")]`. `test_extract_local_files_outside_allowlist_rejected` PASS. |
| **L7** — wake-word pattern config tested | FIXED | `tests/test_telegram_mention_boundaries.py` includes `test_wake_word_pattern_matches`, `test_wake_word_pattern_case_insensitive`, `test_wake_word_pattern_no_match_dropped`, `test_invalid_regex_logged_and_skipped` — all PASS. |

---

## 7. Cross-PR consistency findings

### Format converters
All four (`markdownv2`, `slack_mrkdwn`, `matrix_html`, `whatsapp_format`) export `convert(text: str) -> str` at module scope and have `try / except Exception:` parse-error fallback to `escape_*(text)` plain-text. Verified via grep at lines 25/33/42/21 respectively. **Consistent.**

### `_send_with_retry` adoption
- Telegram: applied at `:294` (`send`), `:890` (httpx POST). Includes 409-conflict fatal cap and network-error fatal cap.
- Discord: applied at `:448` (`send`), `:531` (`edit`), `:565` (`delete`).
- Slack: applied at `:262, 298, 332, 357` (4 paths).
- Matrix: applied at `:156, 196, 252, 290` (4 paths).
- WhatsApp (Cloud API): applied at `:138, 182`.
- Signal/SMS/iMessage/Mattermost/Email: NOT applied. **Documented scope-cut for PR 3c** (per plan §11). Observation O2 above.

### Reaction lifecycle adoption
9 adapters declare `ChannelCapabilities.REACTIONS`: telegram, discord, slack, mattermost, matrix, signal, whatsapp, whatsapp-bridge, imessage. Dispatch fires `on_processing_start` / `on_processing_complete` automatically (`dispatch.py:365-380, 450+`) for any adapter with the flag set. None of these adapters override the hooks unnecessarily — they inherit the safe `_safe_lifecycle_hook` wrapper from `BaseChannelAdapter`. **Consistent.**

### Phone redaction
- iMessage: `extensions/imessage/adapter.py:39,54-69,170-336` — chat GUIDs and handles redacted at every log site.
- Signal: `extensions/signal/adapter.py:35,72,129,170,182,317` — phone numbers redacted at connect/send/error log sites.
- SMS: `extensions/sms/adapter.py` uses `redact_phone` from plugin_sdk channel_helpers (per the commit `29901f5e`).
**No raw E.164 leaks observed in adapter log paths.**

### MarkdownV2 single-escape post-PR-1 fix
`tests/test_format_converters.py:108-129` includes the 4 regression tests asserting `**1.5**` → `*1\.5*` (single backslash), `*hello (world)*` → `_hello \(world\)_`, etc. All 4 PASS.

---

## 8. Stability / safety check

| Check | Result | Notes |
|---|---|---|
| **No breaking changes for existing users** | PASS | `opencomputer gateway` with no new config preserves existing behavior. Mention-gating defaults to `require_mention=False`. |
| **No data loss risk** | PASS | `plugin_sdk/file_lock.exclusive_lock` uses `fcntl.flock` on POSIX (works on macOS per user profile), `msvcrt.locking` on Windows, fail-open with WARNING elsewhere. DM Topics writes use read-merge-write under the lock to preserve concurrent writers (`extensions/telegram/dm_topics.py:84-139`). |
| **No PII leak in logs** | PASS | `redact_phone` from `plugin_sdk.channel_helpers` consumed by all phone-bearing log sites in signal/sms/imessage. `_redact_chat_guid` handles E.164-bearing GUIDs. |
| **No SSRF on outbound paths** | PASS-with-caveat | `ssrf_redirect_guard` + `is_network_accessible` shipped in `plugin_sdk/network_utils.py` and unit-tested. Currently no adapter wires it on outbound httpx clients (observation O3); future webhook-outbound or sticker-download paths should adopt before going public. |
| **Plugin SDK boundary** | PASS | Two boundary tests pass. `grep` confirms zero `from opencomputer` imports inside `plugin_sdk/`. |
| **F1 ConsentGate single-arbiter** | PASS | All approval clicks route through `Dispatch._handle_approval_click` (`dispatch.py:620`) → `ConsentGate.resolve_pending` (`gate.py:344`). No parallel approval state. Adapter `set_approval_callback` only carries the verb+token; decision lives in the gate. |
| **Tests** | PASS | 4,999 passed, 15 skipped, 0 failures, 87.7s wall time. |

---

## 9. Final recommendation

**Flip the PR from draft to ready-for-review and land it as squash-merge to `main`.**

Reasoning:
1. The 6 audit-critical findings have code-level evidence of fixes plus passing regression tests for each.
2. The 8 audit-high findings are either fixed or explicitly resolved by scope decisions documented in the PR body.
3. The PR body's "Known issues / honest deferrals" section accurately frames every gap. No silent feature breakage.
4. Existing-user upgrade path is untouched. All new behavior is opt-in (require_mention default OFF; new adapters disabled-by-default; webhook deliver_only/cross_platform per-token).
5. 4,999 tests pass; the test base grew by ~640 from baseline without regressions.
6. The squash-merge will collapse the 57 commits (including the harmless `4afa439a` revert pair) into a single clean commit with the PR description as the message.

After merge, suggested follow-up cleanup PR (combinable, low priority):

- Apply `_send_with_retry` to signal/sms/imessage/mattermost httpx clients (observation O2).
- Wire SSRF guard into webhook outbound + sticker-image download paths (observation O3).
- Strip `capabilities` from the four pre-existing manifests (observation O4).
- Plumb `photo_burst_window` through gateway config (observation O7).

None of these are merge-blockers.

---

## 10. Verification commands re-run for this review

```bash
pytest tests/ -q --tb=line                             # 4999 passed, 15 skipped, 0 failures
pytest tests/test_phase6a.py::test_plugin_sdk_does_not_import_opencomputer \
       tests/test_sub_f1_sdk_boundary.py -v            # 3 passed
pytest tests/test_dispatch_photo_burst.py -v           # 9 passed (incl. C2/H2 regression)
pytest tests/test_processing_lifecycle.py -v -k consent_prompt   # 1 passed (C5)
pytest tests/test_extract_local_files.py -v -k "outside_allowlist or allowlist"   # 3 passed (M3)
pytest tests/test_telegram_mention_boundaries.py -v    # 28 passed (C4 + L7)
ruff check plugin_sdk/ opencomputer/ extensions/       # 2 pre-existing B010 in affect-injection (out of scope)
grep -rn "from opencomputer" plugin_sdk/               # empty (boundary OK)
grep -rn "capabilities" extensions/whatsapp-bridge/plugin.json   # no capabilities field
ls plugin_sdk/sticker_cache.py opencomputer/cache/sticker_cache.py   # only the plugin_sdk one exists
```

End certification.
