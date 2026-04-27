# Gap-Fill Roadmap — From OC v1 → Hermes-Parity (and beyond)

> **For agentic workers:** This is a multi-PR ROADMAP, not a single-PR plan. Each Tier 1+2 item is its own implementation plan + PR. Sequencing matters because some items build on others.

**Goal:** Close OC's real gaps vs. Hermes / OpenClaw / Glass / Kairos, applying two strict filters:

1. **Framework-lens**: every change must work for ANY user, ANY platform, with opt-in defaults.
2. **"Good enough already?" filter**: only build what closes a gap not already covered by OC's existing 31 tools + 22 plugins + 54 skills (44 bundled + 10 imported via PR #185).

**This roadmap is a planning artifact.** It does NOT auto-execute. Each Tier item below is its own future PR; user picks the order.

---

## 1. Honest current-state inventory

OC already has (verified on `origin/main` at `47f66e7f`):

- **Channel reach**: Telegram + Discord + Slack + iMessage + Matrix + Mattermost + Signal + WhatsApp + Email + Webhook + API-server + HomeAssistant + browser-bridge (observation) — 13 channel adapters scaffolded. Telegram/Discord/terminal/wire are battle-tested; others scaffold-only.
- **Memory**: F4 user-model graph (SQLite + FTS5) + V2.B Spotlight + BGE/Chroma + V2.C life-events + plural personas + Honcho overlay + USER.md + Recall tool + post-response reviewer.
- **Tools (~50+)**: Bash, Read, Write, Edit, MultiEdit, Glob, Grep, WebFetch, WebSearch, TodoWrite, NotebookEdit, SkillTool, AskUserQuestion, PushNotification, PythonExec (PTC), Recall, SessionSearch, MemoryTool, AppleScriptRun, PointAndClick, SpawnDetachedTask, CronTool, voice TTS/STT, screenshot, extract_screen_text, list_app_usage, read_clipboard_once, list_recent_files, plus per-plugin tools (channel adapters, MCP-imported, ambient sensor, etc.).
- **Skills (54)**: 44 bundled (api-design, code-review, security-review, executing-plans, brainstorming, writing-plans, etc.) + 10 imported in PR #185 (PRP workflow + GAN trio + silent-failure-hunter + model-route).
- **Daemons & background**: cron scheduler + ambient sensor + spawn_detached_task + fire_and_forget runner + gateway daemon.
- **Cross-platform CI**: ubuntu + macos + windows on every PR.

OC is NOT "limited". The honest question is which **specific patterns** Hermes/Glass/Kairos do that OC doesn't.

---

## 2. Gap inventory (from prior conversation — re-verified against actual code)

| # | Capability | OC state | Real gap? | Tier |
|---|---|---|---|---|
| 0 | **Auto-skill-evolution loop** | `skill_manage` exists (manual). No automatic detect-extract-stage-review. | YES — Hermes-differentiated | T1 (in flight, T1/8 done) |
| 1 | Inbox triage (WhatsApp/Telegram/Slack) | Adapters exist, no triage workflow. Hermes also doesn't ship one — it's prompt/skill-level. | Small skill add | T2 |
| 2 | Self-healing server ops | cron + Bash exist. No log-watcher pattern. | Tangential to desktop-agent positioning | **SKIP** |
| 3 | Coding via chat (mobile→repo→PR) | All primitives exist (Telegram + coding-harness + git-via-Bash + spawn_detached). Already works. | Cosmetic — formalize as skill | T3 |
| 4 | Meeting notes & follow-ups | Whisper STT + TTS + Notion plugin + cron exist. No assembled pipeline. | Real gap | T2 |
| 5 | Bill/deadline tracker | cron + email + memory + push_notification exist. No assembled tracker. | Real gap, small effort | T2 |
| 6 | Browser automation (control, not just observation) | `browser-bridge` is observation-only. Hermes has full `browser_tool` via `browser_use` library. | Real gap | T1 |
| 7 | Voice conversations (continuous push-to-talk + barge-in) | OC has file-based TTS/STT. Hermes has `voice_mode.py` with sounddevice + VAD + interrupt. | Biggest single gap | T1 |

---

## 3. Tiered plan

### Tier 1 — High-value, framework-shaping (3 items, ~3 days total)

**T1.A — Auto-skill-evolution loop** (already in progress)

- Spec: `OpenComputer/docs/superpowers/specs/2026-04-27-auto-skill-evolution-design.md`
- Plan: `OpenComputer/docs/superpowers/plans/2026-04-27-auto-skill-evolution.md`
- Branch: `feat/auto-skill-evolution` (T1/8 done at `2de2df12`)
- Remaining: T2-T8 (~6 hours)
- **Decision**: continue serially via subagent-driven dev

**T1.B — Voice mode (continuous)**

- **What**: Push-to-talk audio capture loop, VAD-gated transcription, TTS playback with barge-in. Direct port of Hermes's `tools/voice_mode.py` (~600 LOC).
- **Why this and not something else**: highest user-visible impact; OC already has TTS/STT building blocks but no continuous loop; the framework-lens benefits any user who wants hands-free interaction.
- **Module shape**:
  ```
  extensions/voice-mode/
  ├── plugin.json              (default OFF)
  ├── plugin.py                (registration)
  ├── voice_mode.py            (port of Hermes)
  ├── audio_capture.py         (sounddevice wrapper, lazy import)
  ├── vad.py                   (silero-vad or webrtcvad — small package)
  ├── playback.py              (TTS audio playback with barge-in detection)
  ├── README.md                (privacy contract)
  ```
- **Cross-platform**: sounddevice works on mac/linux/win but needs PortAudio. Doctor should warn if missing.
- **Privacy contract**: audio captured locally; transcription via OpenAI Whisper API (configurable); no continuous recording — VAD-gated; pause/disable CLI.
- **Tests**: ~10 tests with mocked audio device (most tests don't need real audio).
- **CLI**: `oc voice talk` — enters interactive voice mode. `oc voice off` — exits.
- **Effort**: ~1.5 days (12 hours)
- **Dependencies**: T1.A complete first (avoid merge conflicts in plugin scaffolding patterns).

**T1.C — Browser automation (control)**

- **What**: Port Hermes's `tools/browser_tool.py` for full browser control via accessibility tree + `browser_use` library OR Playwright MCP. Adds page navigate / click / fill / scrape / snapshot tools.
- **Why this and not something else**: OC's `browser-bridge` is observation-only (Chrome extension POSTs events). Real automation (form-fill, scrape, navigate) is a real gap.
- **Module shape**:
  ```
  extensions/browser-control/
  ├── plugin.json              (default OFF; opt-in)
  ├── plugin.py
  ├── browser_tool.py          (port of Hermes — ~400 LOC)
  ├── providers/
  │   ├── local.py             (Playwright/agent-browser local Chromium)
  │   ├── browserbase.py       (cloud — opt-in by API key)
  ├── README.md
  ```
- **Cross-platform**: Playwright works on all 3 OSes. Needs `playwright install chromium` one-time.
- **Privacy**: never auto-saves screenshots/HTML by default; explicit per-call. Sessions isolated. Cloud backends gated by API key.
- **Tests**: mocked browser provider for unit tests; one smoke test against real Playwright (skipped on CI without browser binary).
- **Effort**: ~1 day (8 hours).
- **Dependencies**: independent of T1.A and T1.B.

### Tier 2 — Real gaps, modest effort (3 items, ~2 days total)

**T2.A — Meeting notes & follow-ups skill**

- **What**: Skill + cron-driven workflow for: (a) ingest audio file → Whisper transcribe → summarize → push notes to Notion / send via Telegram. (b) Follow-up extraction (action items → task list).
- **Why**: Composes existing TTS/STT + Notion MCP + cron + push_notification. Real gap because nothing assembles them.
- **Module shape**: 
  - SKILL.md at `opencomputer/skills/meeting-notes/SKILL.md` (the workflow)
  - Helper at `opencomputer/skills/meeting-notes/extract_action_items.py` (called by the skill)
  - Notion integration via existing MCP plugin (no new code)
- **Effort**: ~1 day (8 hours).
- **Dependencies**: independent.

**T2.B — Inbox triage skill**

- **What**: Skill that, given access to channel adapters, lists unread messages, summarizes, drafts replies, flags urgent (per LLM judge). Cross-channel.
- **Why**: Often-requested AI-assistant capability. OC has the pieces (channel adapters), no orchestrating skill.
- **Module shape**:
  - SKILL.md at `opencomputer/skills/inbox-triage/SKILL.md`
  - Possibly extends `extensions/telegram/`, `extensions/slack/` etc. to expose `messages_unread()` (likely already there in different shapes — survey first)
- **Effort**: ~3 hours.
- **Dependencies**: independent (read-only).

**T2.C — Bill / deadline tracker**

- **What**: Cron-driven workflow scanning email subjects + body for deadline patterns ("due", "by [date]", "renewal", "invoice"). Extracts deadlines, surfaces via push_notification 24h before due.
- **Why**: Real life-management gap; assembles existing email + cron + memory.
- **Module shape**:
  - SKILL.md + small `bills.py` helper
  - Cron job created via existing `opencomputer cron create`
- **Effort**: ~4 hours.
- **Dependencies**: needs Email plugin verified working (currently scaffold-only).

### Tier 3 — Already covered (1 item, ~1 hour)

**T3.A — "Coding via chat" skill (formalization)**

- **What**: SKILL.md formalizing the existing pattern: "user messages from Telegram → agent edits repo → opens PR via gh CLI". OC already supports this; just not documented as a discoverable skill.
- **Module shape**: just `opencomputer/skills/coding-via-chat/SKILL.md`
- **Effort**: ~1 hour.

### Skip

- **Self-healing server ops** — desktop-agent positioning; server-ops belongs in a separate project. Revisit if a concrete user case appears.
- **Bulk-import of remaining 240 ECC skills** — covered by PR #185 self-audit; surgical 10-item import was the right call.

---

## 4. Recommended sequencing

```
NOW                                                                       LATER
─────────────────────────────────────────────────────────────────────────►
  ┌─────────────────────┐
  │ T1.A auto-skill-evo │ ←  IN FLIGHT (T1/8 done, T2-T8 = 6h remaining)
  └──────────┬──────────┘
             ↓
       ┌─────┴──────┐
       │   T1.B     │  ←  Voice mode (highest user-visible impact, 1.5 day)
       │ Voice mode │
       └─────┬──────┘
             ↓
       ┌─────┴──────────┐
       │     T1.C       │  ←  Browser control (independent — could parallel T1.B)
       │ Browser ctrl   │
       └─────┬──────────┘
             ↓
       ┌─────┴────────┐
       │ T2.A meeting │
       │ T2.B triage  │  ←  Cheap quick wins after T1 trio
       │ T2.C bills   │
       └─────┬────────┘
             ↓
       ┌─────┴──────┐
       │ T3.A skill │  ←  Formalize existing pattern
       └────────────┘
```

**Reasoning**:
- T1.A first: in flight; finish before context shifts. No wasted work.
- T1.B (voice) next: highest perceived-value-per-hour. Hands-free interaction is dramatic UX.
- T1.C (browser) parallelizable with T1.B if resources permit; otherwise serially.
- T2 items are cheap — best done in a single batch after T1 since they reuse infra.
- T3.A is a 1-hour cleanup; can land any time.

**Realistic calendar (1 dev = me as subagent dispatcher)**:
- Day 0 (today): T1.A T2-T8 + most of T1.B
- Day 1: T1.B + T1.C
- Day 2: T2.A
- Day 3: T2.B + T2.C + T3.A (parallelize the small ones)

Each item is its own PR with its own opt-in privacy contract.

---

## 5. Self-audit (rigorous expert critic)

### Flawed assumptions

| # | Assumption | Reality | Mitigation |
|---|---|---|---|
| FA1 | "Voice mode is just porting Hermes's voice_mode.py." | Hermes uses sounddevice + numpy + threading; cross-platform audio is notoriously finicky (PortAudio, WASAPI, CoreAudio differences). **Real estimate: 1.5 day MIGHT slip to 2-3 days** if we hit audio-device gotchas. | Set expectation: 1.5 days HAPPY PATH; build in dogfood-pause to catch real-device issues. |
| FA2 | "Browser_use library is stable." | `browser-use` (the GitHub project) had a pivot; Hermes uses `agent-browser` which is a fork. Choosing the right backend matters. | Survey both before T1.C; pick most-maintained option. Don't copy Hermes's choice blindly. |
| FA3 | "Notion MCP works on macOS." | OC has Notion MCP per available-skills list, but I haven't verified active connection. T2.A meeting notes depends on it. | Verify Notion MCP works on Saksham's profile FIRST; if not, T2.A becomes T2.A.1 (fix Notion) + T2.A.2 (add skill). |
| FA4 | "Email plugin is functional." | OC has `extensions/email/` but it's scaffold; never dogfooded. T2.C bills depends on it. | Same verify-first approach. If email plugin doesn't work, T2.C becomes a 1-day item (fix email) + 4-hour item (add tracker). |
| FA5 | "Cross-platform from day 1 won't break voice." | sounddevice on Windows uses WASAPI; on Linux ALSA/PulseAudio; on macOS CoreAudio. Each has subtle bugs. | Build cross-platform tests with mocked audio; document real-device gotchas; lean on `extras_require` so users without audio deps can still install OC. |
| FA6 | "User wants me to build all 7 features." | The user's deeper want is "OC is competitive with Hermes". Building 7 features ≠ being competitive. Some won't be used. | Get user pick after this roadmap is presented. Don't bulk-execute. |
| FA7 | "T1.A in flight will land cleanly." | T2-T8 of auto-skill-evolution have not been written yet. Each task can surface unforeseen issues (e.g. agent loop emission point conflicts with another in-flight session's changes). | T1 already landed cleanly. Continue serially with subagent-driven dev; each task gets its own status check. |
| FA8 | "Each Tier 1 item is its own PR." | TRUE for 1.A and 1.C. For 1.B (voice mode) — there's a follow-up open question about whether the voice-conversational mode should hook into TUI Phase 1's PromptSession or be a separate `oc voice talk` command. | Decide before T1.B starts: separate command is cleaner (avoids invasive change to TUI). |

### Edge cases

| # | Case | Mitigation |
|---|---|---|
| EC1 | User runs `oc voice talk` in SSH session (no audio device). | Doctor preflights; voice mode refuses with clear error. |
| EC2 | Browser automation backend (Playwright local) crashes; no fallback. | Provider abstraction in T1.C — fall through to next backend (Playwright→browserbase) per Hermes's pattern. |
| EC3 | Meeting transcript contains sensitive info (names, financial data). | Existing sensitive-app filter + content-pattern redaction (basic regex). T2.A README explicit about this. |
| EC4 | Inbox triage skill reads user's banking emails / private messages. | Skill respects sensitive-app deny-list; only reads channels user explicitly allowed. |
| EC5 | Bill tracker false-positives: "Submit by Friday" in a colleague's casual email triggers a deadline alert. | Confidence threshold + user can mute false-positives via memory. |
| EC6 | Auto-skill-evolution proposes a skill for sensitive workflow (e.g. "log into bank, transfer $X"). | T1.A already integrates sensitive-app filter at extraction time. Verified in spec §3.4. |
| EC7 | Voice mode + ambient foreground sensor both want clipboard access at the same time. | Both are independent; F1 ConsentGate is per-tool; no race. |
| EC8 | User on Windows hits PowerShell-vs-bash differences in CI. | Already solved in PR #181 with `shell: bash`. Pattern extends to new tests. |
| EC9 | Voice mode keeps recording when user accidentally locks screen. | Pause-on-screen-lock detection (macOS: `caffeinate -s` check; future enhancement). v1: heartbeat + manual stop. |
| EC10 | Browser control opens user's logged-in personal Chrome → automation acts as the user. | T1.C uses isolated browser session by default (Playwright `--isolated`). Document; user opt-in for shared profile. |

### Missing considerations

| # | Item | Action |
|---|---|---|
| MC1 | **Cost guard for new features.** Voice (Whisper API), browser (some backends pay-per-action), meeting notes (Whisper + LLM summarization). | All these new tools must integrate with existing `opencomputer/cost_guard.py`. Daily budgets per feature. |
| MC2 | **Discoverability.** Adding 7 features = 7 more things users need to know about. | Each PR ships with README under its plugin dir + a single line in main `OpenComputer/README.md`. Doctor surfaces capability matrix. |
| MC3 | **Memory + skill collision.** Auto-skill-evolution might create a skill that looks like one of the new T2 skills. | T1.A uniqueness suffix on collision; document. |
| MC4 | **Sequencing dependency: T2.C depends on email plugin working.** | Spike a 30-min email plugin verification BEFORE T2.C kickoff; report findings; potentially split T2.C into sub-tasks. |
| MC5 | **Voice-mode API key dependency.** Whisper is OpenAI-paid. Saksham doesn't have OpenAI key by default (per memory: Anthropic-first). | T1.B should support local Whisper (`whisper.cpp` / `mlx-whisper`) as a fallback. Hermes also has neutts_synth.py for local TTS — port that too. |
| MC6 | **Browser control + ambient sensor conflict.** Both watch foreground/screen. | They're orthogonal; no conflict. Document. |
| MC7 | **Roadmap drift.** I'll write this plan, user picks a subset, then weeks later asks "wait what about X?" | Mark this doc as living; update after each PR with status. |
| MC8 | **The "any user" filter could be too strict.** Some features (Mac-only AppleScript-driven workflows) genuinely don't transfer. | Document per-feature platform support matrix in each README. Don't refuse to ship Mac-specific features; just label them. |

### Alternative approaches considered

- **AA1**: Bulk-import 50+ Hermes tools wholesale instead of selective. **Rejected**: bloat; many duplicate; previous wins were surgical (PR #185).
- **AA2**: Skip voice mode entirely (niche use case). **Rejected**: it IS dramatic UX; one of the few features that's genuinely user-visible-different.
- **AA3**: Skip browser control (overlap with WebFetch). **Rejected**: WebFetch is read-only; control (form-fill, click-through-flow) is genuinely different.
- **AA4**: Build T2 items first as warm-ups. **Rejected**: T1 items have higher value-per-hour; T2 items can wait.
- **AA5**: Make all features default ON. **Rejected**: violates framework-lens; surprise behavior for any user.

### Refinements applied (from this audit)

1. **FA1**: voice mode estimate revised to "1.5 days happy path; 2-3 days realistic with audio gotchas".
2. **FA3+FA4**: T2.A and T2.C get a 30-min "verify dependency works" spike before kickoff.
3. **MC5**: T1.B includes local-Whisper fallback (whisper.cpp / mlx-whisper) — bumps voice-mode estimate to ~2 days but removes API-key dependency.
4. **MC2**: every PR ships with README + 1-line in main README + doctor capability surface.
5. **EC10**: T1.C defaults to isolated browser session.
6. **MC7**: this roadmap is a living doc; update with status after each PR.

### Stress test against real-world constraints

- **API-key constraints (Saksham primarily uses Anthropic)**: T1.B local-Whisper fallback addresses this. T2.A meeting notes can use Anthropic for summarization (cheap model).
- **Repo just went public**: cross-platform CI is now free. No CI-cost constraint.
- **Saksham's stated daily workflow** (stocks + Telegram + coding): T1.A (skills evolution) + T2.B (inbox triage) directly help. T1.B (voice) marginal. T1.C (browser) helpful for web-based stock platforms.
- **Maintenance burden**: 7 new features = ~3,500 LOC + tests = ongoing maintenance. Manageable on a public MIT project; would NOT recommend for private/single-maintainer.

### Effort summary (post-audit)

| Item | Original estimate | Post-audit estimate | Net change |
|---|---|---|---|
| T1.A auto-skill-evolution | 8h | 8h (1h done) | — |
| T1.B voice mode | 12h | **18h** (local-Whisper fallback) | +6h |
| T1.C browser control | 8h | 8h | — |
| T2.A meeting notes | 8h | **8.5h** (+ Notion verification spike) | +0.5h |
| T2.B inbox triage | 3h | 3h | — |
| T2.C bill tracker | 4h | **5h** (+ email verification spike) | +1h |
| T3.A coding-via-chat | 1h | 1h | — |
| **TOTAL** | 44h | **51h** | +7h |

Realistic: ~1 week of focused subagent-driven dev to ship everything Tier 1+2+3.

### Acceptance criteria (the merge bar for each PR)

- [ ] Default OFF / opt-in.
- [ ] Cross-platform (mac/linux/win) or explicitly documented platform restriction.
- [ ] Privacy contract baked into tests.
- [ ] AST no-egress where applicable.
- [ ] Doctor preflight check.
- [ ] CHANGELOG entry under `[Unreleased]`.
- [ ] CI green on all 3 OSes.
- [ ] README under plugin dir + 1-line in main README.

---

## 6. What this roadmap is NOT

This is a planning artifact — not an automatic execution queue. Specifically:

- **Doesn't auto-execute**: each Tier 1+2 item is its own PR with its own subagent-driven plan (writing-plans skill).
- **Doesn't lock the order**: user picks which to ship next based on priority shifts.
- **Doesn't prevent additions**: if a new gap surfaces during dogfood, add it here.
- **Doesn't replace per-feature spec**: each item gets its own spec doc + plan when its turn comes.

The next decision point: which item ships next after T1.A finishes?

---

*Roadmap complete with self-audit. Ready for user review + per-feature plans on demand.*
