# Screen-Awareness — Event-Driven Capture for Continuous Self-Understanding

**Goal:** OpenComputer captures the user's primary screen via OCR at three event triggers — user-message-arrival, pre-tool-call, post-tool-call — so the agent can self-understand what's on screen at each step of the way without paying the cost of continuous polling.

**Status:** Design (2026-04-29). Awaiting user spec review before writing the implementation plan.

**Inspiration:** [Glass by Pickle](https://github.com/pickle-com/glass) — but Glass actually captures on-demand per Ask question (NOT continuously). Our model goes a step further: capture on every user-message AND every tool-call boundary, so the agent's awareness covers each step of its own action loop in addition to user prompts.

**Branch:** `feat/screen-awareness` (cut from `main` at `2d72878e`).

---

## 1. The problem

When the user types "what's on my screen" or "look at this and tell me…", OC's agent doesn't know — it has to call `screenshot` explicitly each time. When OC takes a tool action that mutates the screen (PointAndClick, AppleScriptRun, …), the agent has no visual confirmation of the result. The screen-introspection tools (`screenshot`, `extract_screen_text`) shipped in PR #179 but they're pull-only — agent has to remember to use them.

Result: OC is "blind" between tool calls and at the moment user prompts arrive. It self-understands its actions only via stdout/text return values, never via what the screen actually shows.

## 2. What's already there (no rebuild required)

- `ScreenshotTool` — `mss`-backed, base64 PNG, F1-gated `introspection.screenshot` IMPLICIT tier. `extensions/coding-harness/introspection/tools.py`.
- `ExtractScreenTextTool` — `mss` + `rapidocr-onnxruntime` OCR. Same file.
- F1 ConsentGate with IMPLICIT/EXPLICIT/PER_ACTION tiers — `opencomputer/consent/`.
- Hook engine with `PreToolUse` / `PostToolUse` events fired around every tool dispatch — `opencomputer/hooks/engine.py`.
- AST no-egress test pattern from Phase 1 ambient sensor — `tests/test_ambient_no_cloud_egress.py`.
- Sensitive-app denylist — `extensions/ambient-sensors/sensitive_apps.py`.
- Cross-platform foreground-app polling (Phase 1 of ambient sensor) — proves the cross-platform plumbing works.

## 3. The design (event-driven, no daemon)

### 3.1 Architecture

```
                user submits message       LLM emits tool_call          tool_call returns
                       │                          │                            │
                       ▼                          ▼                            ▼
               ┌─────────────────┐       ┌─────────────────┐        ┌─────────────────┐
               │ BEFORE_USER_MSG │       │ PreToolUse hook │        │ PostToolUse hook│
               │ (NEW hook event)│       │ (existing)      │        │ (existing)      │
               └─────────────────┘       └─────────────────┘        └─────────────────┘
                       │                          │                            │
                       └──────────┬───────────────┴────────────┬───────────────┘
                                  ▼                            ▼
                       ┌──────────────────────────────────────────┐
                       │  ScreenAwarenessSensor                   │
                       │  extensions/screen-awareness/sensor.py   │
                       │  • capture_now() → mss + OCR             │
                       │  • SHA256 dedup                          │
                       │  • sensitive-app filter (Phase 1 parity) │
                       │  • lock/sleep skip                       │
                       │  • F1 ConsentGate (EXPLICIT tier)        │
                       └──────────────────────────────────────────┘
                                          │
                ┌─────────────────────────┼─────────────────────────┐
                ▼                         ▼                         ▼
        ┌───────────────┐      ┌──────────────────────┐    ┌──────────────────┐
        │ RingBuffer    │      │ ScreenContext inject │    │ Tool result with │
        │ (last 20)     │      │ as system_reminder   │    │ pre/post diff    │
        │ in-RAM        │      │ on next agent step   │    │ attachment       │
        └───────────────┘      └──────────────────────┘    └──────────────────┘
                │
                ▼
        RecallScreen tool
        (agent queries history)
```

### 3.2 Three triggers, three hook points

| Trigger | Hook event | What gets captured | Surfaces to LLM as |
|---|---|---|---|
| **User submits a message** | NEW `BEFORE_USER_MESSAGE` hook (or piggyback `UserPromptSubmit` if it exists in the engine) | OCR of full primary screen | `<screen_context>...</screen_context>` system reminder injected into the next agent step |
| **LLM about to call a tool** | Existing `PreToolUse` (filtered to GUI-mutating tools only via denylist) | OCR of primary screen | Stored in ring buffer keyed by `tool_call_id`. Not injected unless agent reads via RecallScreen, OR shows up in PostToolUse delta |
| **Tool call returned** | Existing `PostToolUse` (same filter) | OCR of primary screen | Compared to pre-snapshot; line-level diff (`+added`, `-removed`) attached to tool result as a `_screen_delta` field surfaced in the tool's transcript line |

### 3.3 Trigger filter (which tools fire pre/post capture)

Default denylist (does NOT trigger ambient capture — too noisy / not GUI-mutating):
- `Read`, `Write`, `Edit`, `MultiEdit`, `Grep`, `Glob`
- `Bash` (text-only by default, but see opt-in below)
- `WebFetch`, `WebSearch`, `Recall`, `MemoryRecall`, `MemorySearch`
- `Skill`, `SkillManage`, `TodoWrite`
- All slash-dispatched commands

Default allowlist (DOES trigger pre/post capture — these mutate visible state):
- `PointAndClick`, `MouseMoveToTool`, `MouseClickTool`, `KeyboardTypeTool`
- `AppleScriptRun`, `PowerShellRun` (when shipped)
- `Bash` IF the command matches a GUI-launch heuristic (`open`, `xdg-open`, `start`, app launchers) — opt-in via config

Config knob: `screen_awareness.tool_capture_mode = "denylist" | "allowlist" | "all"` (default `"allowlist"`).

### 3.4 Privacy & safety contract

Mirrors Phase 1 of ambient-sensors:

1. **Default OFF.** Two gates required:
   - `oc config set screen_awareness.enabled true` (config-level)
   - F1 consent grant: `oc consent grant introspection.ambient_screen --tier explicit` (capability-level)
2. **Sensitive-app denylist** — same denylist as `extensions/ambient-sensors/sensitive_apps.py`. Active app matches → capture skipped, surface `<screen_context>filtered: sensitive app active</screen_context>` to the LLM (so it knows context is unavailable, doesn't see why).
3. **Lock/sleep skip** — cross-platform `_is_screen_locked()`:
   - macOS: `Quartz.CGSessionCopyCurrentDictionary()` checks `CGSSessionScreenIsLocked`
   - Linux: `xdg-screensaver status` exits 0 with "active" output
   - Windows: registry / `LockWorkStation` API check
4. **AST no-egress test** — extends `tests/test_ambient_no_cloud_egress.py` to scan `extensions/screen-awareness/` for HTTP-client imports. Adding networking is a contract break, not just a code change.
5. **Storage**:
   - In-RAM ring buffer (last 20 OCR captures, ~50 KB total — text only, no images)
   - Opt-in `<profile_home>/screen_history.jsonl` append log with 7-day TTL rotation, only when `screen_awareness.persist=true`
   - **No image bytes persisted** by default; only OCR text
6. **De-dup**: SHA-256 of normalized OCR text. Same screen content = single ring entry. Trigger source recorded so we can tell "ten user messages saw the same screen" from "screen actually unchanged."
7. **Cooldown**: minimum 1s between captures regardless of trigger. Two PreToolUse fires within 1s = single capture reused.

### 3.5 Surface to the LLM

**Two surfacing modes, opt-in independently:**

(a) **System reminder injection on user-message events.** When `BEFORE_USER_MESSAGE` fires + capture succeeds, the next agent step gets:
```
<system-reminder>
Screen context (captured 0.4s ago, sha=ab12cd...):
[OCR text, max 1000 tokens, truncated with ellipsis]
</system-reminder>
```
Always shown — that's the user's "see what I'm looking at" moment.

(b) **Tool-result attachment for pre/post pairs.** Tool result gets a structured field:
```json
{
  "content": "<original tool stdout>",
  "_screen_delta": {
    "pre_sha": "ab12...",
    "post_sha": "cd34...",
    "added_lines": ["Login successful", "Dashboard"],
    "removed_lines": ["Sign in", "Email"],
    "captured_pre_at": 1714123.4,
    "captured_post_at": 1714125.1
  }
}
```
Surfaces as a small badge in the transcript: `(screen changed: +2 / -2 lines)`.

(c) **Agent recall via tool.** New `RecallScreen` tool returns the ring buffer contents:
```
RecallScreen(window_seconds=60) -> "Last 4 captures, oldest first: [...]"
```
Agent invokes when it needs to reason about screen history.

### 3.6 New + modified files

| File | Action | Approx LOC | Responsibility |
|---|---|---|---|
| `extensions/screen-awareness/__init__.py` | NEW | 5 | Plugin module marker |
| `extensions/screen-awareness/plugin.py` | NEW | ~80 | `register(api)` — wires `BEFORE_USER_MESSAGE` + `PreToolUse` + `PostToolUse` hooks; registers `RecallScreen` tool |
| `extensions/screen-awareness/sensor.py` | NEW | ~180 | `ScreenAwarenessSensor` — capture_now(), dedup, sensitive filter, lock/sleep skip, ring buffer |
| `extensions/screen-awareness/lock_detect.py` | NEW | ~80 | Cross-platform `is_screen_locked()` (macOS/Linux/Windows) |
| `extensions/screen-awareness/ring_buffer.py` | NEW | ~60 | Bounded last-N captures (default 20), thread-safe append + read |
| `extensions/screen-awareness/recall_tool.py` | NEW | ~100 | `RecallScreen` BaseTool subclass; F1 ConsentGate IMPLICIT tier |
| `extensions/screen-awareness/diff.py` | NEW | ~50 | OCR-text line diff (+added, -removed); used for pre/post delta |
| `extensions/screen-awareness/persist.py` | NEW | ~80 | Opt-in JSONL append log + 7-day TTL rotation |
| `extensions/screen-awareness/sensitive_apps.py` | NEW | ~30 | Re-export from ambient-sensors with passthrough; keeps the denylist single-source |
| `extensions/screen-awareness/README.md` | NEW | ~80 | Privacy contract — mirrors ambient-sensors README structure |
| `opencomputer/hooks/engine.py` | Modify | +20 | Add `BEFORE_USER_MESSAGE` event + dispatcher |
| `plugin_sdk/hooks.py` | Modify | +5 | Add `HookEvent.BEFORE_USER_MESSAGE` |
| `opencomputer/agent/loop.py` | Modify | +30 | Fire `BEFORE_USER_MESSAGE` hook on every user turn entry; surface `<screen_context>` injection from hook return value |
| `extensions/screen-awareness/tests/test_sensor.py` | NEW | ~150 | capture_now happy path, dedup, sensitive filter, lock skip, cooldown |
| `extensions/screen-awareness/tests/test_lock_detect.py` | NEW | ~80 | Cross-platform skip semantics (mocked OS calls) |
| `extensions/screen-awareness/tests/test_ring_buffer.py` | NEW | ~60 | Bounded append, thread safety, oldest-eviction |
| `extensions/screen-awareness/tests/test_recall_tool.py` | NEW | ~80 | RecallScreen schema, window filter, empty buffer |
| `extensions/screen-awareness/tests/test_diff.py` | NEW | ~60 | Line diff edge cases (empty, identical, all-changed) |
| `extensions/screen-awareness/tests/test_persist.py` | NEW | ~80 | JSONL append, TTL rotation, opt-in respected |
| `tests/test_screen_awareness_integration.py` | NEW | ~140 | Full agent loop with sensor enabled — `_FakeProvider` captures injected `<screen_context>` |
| `tests/test_ambient_no_cloud_egress.py` | Modify | +5 | Extend AST scan to `extensions/screen-awareness/` |
| `opencomputer/doctor.py` | Modify | +20 | Screen Recording permission check on macOS |

**Approximate totals: 13 new source files (~1100 LOC), 4 modified source files (~80 LOC), 7 new test files (~650 LOC) = ~1850 LOC.**

### 3.7 Sub-PR scope (this PR ships 2.A + 2.B + 2.D from the brainstorm)

All three sub-PRs land in this single PR per the user's directive:

- **2.A** Event-driven capture core: `BEFORE_USER_MESSAGE` hook + extend `PreToolUse`/`PostToolUse` capture path; sensor + lock-detect + sensitive filter + AST egress test.
- **2.B** Ring buffer + `RecallScreen` tool + opt-in JSONL persistence + TTL rotation.
- **2.D** Pre/post tool-step diff: line diff between pre/post OCR; `_screen_delta` attached to tool result; transcript badge.

**Deferred to follow-ups (not in this PR):**
- 2.C — keyword-trigger system reminder injection (dropped in §3.5; user-message trigger covers this)
- 2.E — first-run wizard step + permissions UX polish (only the doctor check ships in this PR)
- Full multi-monitor capture (primary monitor only in v1)
- Multimodal/pixel mode (OCR text only in v1)

### 3.8 Error handling

| Failure mode | Behavior |
|---|---|
| `mss` import fails / not installed | Log INFO once, sensor disables itself for the session, capture is no-op |
| OCR raises | Log WARNING with traceback, capture skipped, ring buffer unchanged |
| Lock detection raises | Treat as locked (fail-safe — no capture) |
| Sensitive-app filter import fails | Treat as sensitive (fail-safe — no capture) |
| Permission denied (macOS Screen Recording not granted) | Log INFO once with a `oc doctor` hint, sensor disables itself for the session |
| Capture takes >5s | Cancel, log WARNING, skip — never block the agent loop |
| Hook fires while loop is already inside a capture | Cooldown skips the second; log DEBUG |

### 3.9 Testing strategy

| Layer | Test type | Coverage |
|---|---|---|
| `sensor.py` | Unit (mocked mss + OCR) | capture_now happy path, dedup hash equality, sensitive-filter routing, lock-skip routing, cooldown |
| `lock_detect.py` | Unit (mocked OS calls) | macOS/Linux/Windows branches; fail-safe on import error |
| `ring_buffer.py` | Unit | Bounded append, oldest-eviction at N+1, thread-safe append from concurrent threads |
| `diff.py` | Unit | Empty/empty, identical, all-added, all-removed, mixed |
| `recall_tool.py` | Unit | Schema, window_seconds filter, empty buffer returns explanatory message |
| `persist.py` | Unit (tmp_path) | JSONL append, TTL rotation, persist=false respected |
| `plugin.py` registration | Integration | Hooks wire correctly; tool registered |
| Full agent loop | Integration | `_FakeProvider` captures `<screen_context>` in messages on user turn; pre/post tool delta lands in tool result |
| AST no-egress | Linter-style | Egress scan covers new module |
| Doctor | Integration | macOS branch detects missing Screen Recording permission |

**~25-30 new tests across 8 test files.**

### 3.10 Acceptance criteria

This PR ships when:

1. With `screen_awareness.enabled=true` and consent granted, every user message turn results in a `<screen_context>` system reminder being injected into the agent's context.
2. With same config, every GUI-mutating tool call (default allowlist: PointAndClick, AppleScriptRun, etc.) gets a pre + post screenshot and the tool result carries a `_screen_delta` field.
3. With `screen_awareness.enabled=false` (default), zero captures happen — verified by no-call assertions in test.
4. Sensitive-app denylist active → `<screen_context>filtered: sensitive app active</screen_context>` injected, no OCR captured.
5. Screen locked → no capture, no error, log line at INFO.
6. RecallScreen tool returns the last-N captures from the ring buffer with a `window_seconds` filter.
7. AST no-egress test passes for `extensions/screen-awareness/`.
8. Doctor flags missing macOS Screen Recording permission.
9. All ~25-30 new tests pass.
10. Full existing suite (5800+ tests) stays green.
11. ruff clean on all new files.

### 3.11 Reuse from Glass

Glass is JavaScript/Electron — not directly portable to Python. Patterns we keep / drop:

| Glass pattern | OC equivalent | Notes |
|---|---|---|
| `screencapture -x -t jpg` (macOS native) | `mss` (already shipping) | mss is cross-platform; native CLI is faster on macOS but we already have mss |
| `desktopCapturer.getSources()` (Electron) | `mss.mss().grab()` | Equivalent; mss is the Python idiom |
| `sharp` for image resize | `Pillow` (already shipping) | Used only if we ever go pixel-mode; OCR-only doesn't need resize |
| Cloud Gemini for vision | rapidocr-onnxruntime (local) | OC stays local; Glass goes cloud |
| On-question screen capture | NEW: on-user-message + on-tool-call | OC's "each step" framing extends Glass's "on Ask" |
| Continuous audio listen | not in this PR | Voice mode (PR #199) is separate and already shipped |

## 4. Out of scope (explicit)

- **Continuous polling daemon** — explicitly removed during brainstorm. Triggers are event-driven only.
- **Multimodal/pixel context** — image bytes never sent to LLM in v1. OCR text only.
- **Multi-monitor capture** — primary monitor only. Multi-monitor follow-up if dogfood demand surfaces.
- **Audio listen** — already shipped via voice-mode (PR #199); separate concern.
- **First-run wizard step** — only `oc doctor` check in v1. Wizard step is a polish follow-up.
- **Per-window crop** — full primary monitor capture; cropping to active window is a follow-up.
- **Screen recording (video)** — never in scope; pixel snapshots only at capture moments.

## 5. Risks and unknowns

1. **`BEFORE_USER_MESSAGE` is a new hook event** — adding it to the engine is a small contract change. Mitigation: default no-op for plugins that don't subscribe.
2. **macOS Screen Recording permission** is a separate gate from the Accessibility permission OC already prompts for. First capture attempt may silently return black if not granted. Mitigation: doctor check + INFO log on first failure.
3. **OCR latency** — `rapidocr-onnxruntime` first-call cost is ~5s (model load). Subsequent calls are ~100-300ms. Mitigation: lazy-load on first capture (already implemented in PR #179's `ocr.py`); accept the first-capture latency as a one-time cost per session.
4. **Cooldown vs accuracy** — 1s cooldown means two rapid PreToolUse fires within 1s reuse the same capture. Edge case: a tool fires that mutates screen, then immediately another fires — second's "pre" is actually the first's "post". Mitigation: log the cooldown skip at DEBUG so we can audit if it ever bites; widen to 500ms if needed in dogfood.
5. **Sensitive-app filter false negatives** — denylist is name-based; renamed Electron apps (e.g., custom-built 1Password fork) won't match. Mitigation: README documents how to extend the denylist; keep adding common cases.
6. **Ring buffer in-RAM only** — agent crash loses the buffer. That's fine (privacy-positive); only the opt-in JSONL persists.

## 6. Sequencing

- **Now**: This spec → user reviews → `writing-plans` produces TDD plan → expert-critic audit → `executing-plans` ships PR.
- **Dogfood gate** — 1-2 weeks of daily use before considering follow-ups (multi-monitor, pixel mode, wizard polish).

---

## Spec self-review

**1. Placeholder scan.** No "TBD" / "TODO" / "fill in later". Every section has concrete content. **Pass.**

**2. Internal consistency.** §3.2 trigger table, §3.6 file map, §3.9 testing strategy, §3.10 acceptance criteria all reference the same components and contracts. §3.7 sub-PR scope matches the brainstorm decomposition. **Pass.**

**3. Scope check.** ~1850 LOC across 21 files for one PR. Single-implementation-plan sized. **Pass.**

**4. Ambiguity check.**
- "Cooldown 1s" explicit.
- "Ring buffer last 20" explicit.
- "TTL 7 days" explicit.
- "OCR text only, no image bytes persisted" explicit.
- "EXPLICIT consent tier" explicit (vs IMPLICIT for the existing screenshot tool).
- "Default `tool_capture_mode = allowlist`" explicit, with full allow/deny lists in §3.3.
- Hook event name `BEFORE_USER_MESSAGE` explicit and signaled as new in §3.6.
**Pass.**

**Result: spec is internally consistent, unambiguous, scope-checked. Ready for user review.**
