# OpenClaw Port — Handoff for Next Session

**Date prepared:** 2026-04-28
**Author:** Claude (current session)
**Selectivity directive applied:** Only port from OpenClaw what (a) OpenClaw does *materially better* than Hermes/Claude Code, (b) OC doesn't already have a good-enough version of, (c) fits OC's positioning. Resulted in **4 picks**, not the 30+ initially considered.

---

## What's in this folder

| File | Purpose |
|---|---|
| `2026-04-28-major-gaps.md` | **Selective** gap audit. Only the 4 real gaps + a long "deliberately not porting" list. Read this first. |
| `2026-04-28-deep-feature-survey.md` | Reference catalog of OpenClaw's full surface (4455 words, 117 extensions, mobile/canvas/voice subsystems). Use as reference, NOT as a port checklist. |
| `2026-04-28-oc-current-state.md` | Where OC is on `main` (`4db74443`) — 27 extensions, 426 test files, 9 in-flight PRs. |
| `inventory.md` | OLD (2026-04-22) feature-mapping table. Stale; superseded by the three above. Kept for archaeology. |

The executable plan is at:

| File | Purpose |
|---|---|
| `../../../docs/superpowers/plans/2026-04-28-openclaw-tier1.md` | Tier 1 implementation plan covering all 4 picks. ~580 lines. Phase 0 pre-flight + Sub-projects A/B/C/D + self-audit. |

---

## The four picks (in priority order)

| # | Title | Sub-project | Effort | Why it passes the filter |
|---|---|---|---|---|
| 1 | Multi-agent isolation + channel-binding router | A | XL (multi-PR) | Hermes is profile-per-process. OpenClaw routes inbound channel messages to one of N isolated agents inside one Gateway via deterministic `(channel, accountId, peer, ...)` binding match. **OpenClaw does this materially better than Hermes;** OC has no equivalent. |
| 2 | Standing Orders + cron integration | B | L | Hermes has cron (job-execution-as-syntax). OpenClaw layers `## Program:` blocks in AGENTS.md as the "permanent operating authority" abstraction. **The text-contract DSL is uniquely OpenClaw.** |
| 3 | Active Memory blocking pre-reply sub-agent | C | M | OC has reactive Recall (model decides) and post-response reviewer. **OpenClaw runs a bounded sub-agent BEFORE every eligible reply** — proactive vs reactive. |
| 4 | Block streaming chunker + `humanDelay` | D | M | OC streams tokens raw to channels (looks robotic on Telegram). OpenClaw chunks at paragraph/newline/sentence boundaries with `humanDelay` randomized 800-2500ms pauses. |

**That's the whole port list.** Anything not in this table is in `2026-04-28-major-gaps.md` § "Deliberately not porting" with a one-line rationale.

---

## How to resume in another session

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git checkout main && git pull

# Read in this order:
#   1. docs/refs/openclaw/HANDOFF.md            ← THIS FILE
#   2. docs/refs/openclaw/2026-04-28-major-gaps.md
#   3. docs/superpowers/plans/2026-04-28-openclaw-tier1.md

# Then start with Phase 0 pre-flight (verify all OC API assumptions before coding):
#   - Read the plan's Phase 0 (5 verification tasks)
#   - Create branch: git checkout -b prep/openclaw-tier1-decisions
#   - Run each Phase-0 task; record findings to:
#     docs/superpowers/plans/2026-04-28-openclaw-tier1-DECISIONS.md
#   - Commit + push (no PR needed; the doc is the contract)

# Then start Sub-project A (multi-agent isolation), beginning with PR-A1:
#   - git checkout -b feat/multi-agent-foundation
#   - Implement Tasks A1.1 → A1.5 with TDD
#   - Open PR-A1 to main
#   - Wait for review/merge before A2

# Sub-projects B, C, D can run in parallel after A's PR-A2 (SessionDB migration) lands.
# Recommended execution skill: superpowers:subagent-driven-development for parallel
# agents per task; superpowers:executing-plans for sequential single-engineer execution.
```

---

## What you should NOT do

The user explicitly said: **"Don't bombard and fill the whole thing with OpenClaw features. We've taken many things from Hermes and many from OpenClaw already. Be very selective. Only the ones that truly need the OpenClaw upgrade."**

So:

- **Do not** mass-port the 50 OpenClaw provider plugins. `litellm` covers most. Add specific providers (e.g., Groq for fast Whisper-large-v3 STT) only when there's a concrete need.
- **Do not** port the 25 OpenClaw channel adapters. The Hermes megamerge (PR #221) shipped 13 of the relevant ones. Niche channels (nostr/twitch/irc/qqbot/zalo/wechat/feishu/line/voice-call SIP) skip.
- **Do not** port mobile apps, canvas, voice-wake, Talk Mode, or Voice Call — CLAUDE.md §5 wont-do.
- **Do not** port Lobster, TaskFlow, Background Tasks ledger expansion, Diagnostics OTEL, sandbox-browser, ACP expansion, hook taxonomy expansion (beyond the single `BeforeAgentReply` event needed for Sub-project C).
- **Do not** port the `acpx` / Codex / opencode external-CLI-as-harness pattern — it's at odds with OC's positioning (OC *is* the harness; bridging external harnesses through us is the wrong direction).
- **Do not** restructure plugin manifests to JSON (`openclaw.plugin.json` shape). OC's Python-declarative `register(api)` + `PluginManifest` is cleaner.

The "Deliberately not porting" table in `2026-04-28-major-gaps.md` has the full list with rationale.

---

## Connection to other in-flight work

This plan does **not** depend on any of the 9 open Hermes PRs (#220-#228). It can ship in parallel.

Once Hermes PRs merge, the OpenClaw plan picks up cleanly:
- Skills Hub (PR #220) doesn't conflict with multi-agent.
- Provider runtime flags (PR #224) doesn't conflict.
- `/<skill-name>` auto-dispatch (PR #225) doesn't conflict.
- Edge TTS (PR #227) and Groq STT (PR #228) don't conflict.

---

## Memory entry to save

After the plan ships its first PR, save a project memory at `~/.claude/projects/-Users-saksham-Vscode-claude/memory/`:

```yaml
---
name: OpenClaw Tier 1 plan
description: Selective port of 4 OpenClaw capabilities (multi-agent + Standing Orders + Active Memory + block chunker). NOT a parity port.
type: project
---

OpenClaw plan-of-record for next ship-wave (2026-04-28). 4 picks only:
- A: Multi-agent isolation + channel-binding router (XL multi-PR)
- B: Standing Orders + cron integration (L)
- C: Active Memory blocking pre-reply sub-agent (M)
- D: Block streaming chunker + humanDelay (M)

Why: OpenClaw does these materially better than Hermes/Claude Code, OC doesn't
have a good-enough version, fits OC's positioning. Everything else is in
"deliberately not porting" — see major-gaps.md.

Plan: docs/superpowers/plans/2026-04-28-openclaw-tier1.md
Gap audit: docs/refs/openclaw/2026-04-28-major-gaps.md
Reference: docs/refs/openclaw/2026-04-28-deep-feature-survey.md
OC state: docs/refs/openclaw/2026-04-28-oc-current-state.md
```
