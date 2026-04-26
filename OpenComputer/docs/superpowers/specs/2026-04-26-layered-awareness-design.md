# Layered Awareness — design spec

> **Date:** 2026-04-26
> **Author:** brainstorming session, `feat/layered-awareness` branch
> **Status:** design approved (in-conversation), MVP plan to follow at `docs/superpowers/plans/2026-04-26-layered-awareness-mvp.md`
> **Supersedes parts of:** `~/.claude/plans/there-are-many-pending-tranquil-fern.md` §F (Sub-project F was the conceptual ancestor — F1, F3, F4, F5 already shipped on `main`; this spec finishes F's intent with a different architectural decomposition)

## TL;DR

Build a system where **OpenComputer already knows the user before they ever start using it.** Eight overlapping layers running at different cadences (seconds → minutes → hours → continuous), each contributing to a single user-model graph. MIT-licensed, local-first, no data leaves the laptop.

Compared to the original Sub-project F roadmap (a linear F1→F10 pipeline), this design:

1. **Tiers ingestion by latency** so the agent is useful within minutes, deeper after hours, and continuously sharper thereafter.
2. **Names two missing components** that F1-F10 didn't include: a Life-Event Detector (recognizes "got fired" / "studying for an exam" / "going through a breakup" patterns) and a Curious Companion loop (asks indirect, contextual questions to fill gaps).
3. **Drops the data-controller / multi-tenant privacy concerns** that bloated the original. This is a single-user local agent under MIT — no GDPR scope, no scraping ToS exposure for the framework itself, no consent-fatigue UX nightmare. Consent stays as F1 capability gates, not a fortress.
4. **Uses macOS Spotlight as the FTS layer** instead of building our own. Saves ~1/3 of the engineering. Cross-platform (Linux/Windows) gets handled later with platform-native equivalents (or `tantivy`).

## Success criteria

This system is "working" when, on a fresh OpenComputer install, the user can have these moments within their first session:

1. **Name + identity.** Agent greets the user by name. Knows their primary email, GitHub handle, what city they're in, what they do.
2. **Current-context awareness.** Agent references something the user is *actively* working on this week ("how's the OpenComputer plan going?", "still on that stock briefing flow?").
3. **Style match.** Agent's first responses are calibrated to the user's preferred tone (concise / verbose, formal / casual) without being told.
4. **Life-event sensitivity.** When the user mentions a hard week (lost a job, breakup, health scare, exam stress), the agent has either already noticed via signal patterns OR responds with appropriate emotional warmth, not generic acknowledgement. *This is the headline success metric.*
5. **Indirect Q&A.** Within ~3 days of use, the agent surfaces a thoughtful, indirect question to fill a gap in its model: "I noticed you've been deep in stocks the last 2 days — actively trading or research mode?"

Non-goals:

- Not building a "personality clone" or chatbot mimic. The agent stays itself; it just knows the user.
- Not building a CRM or productivity tracker. The user-model is for *the agent's reasoning*, not user-facing analytics.
- Not building a cross-device sync system. Single laptop, single agent. Multi-device is post-V4.

## Architecture — eight layers

```
LAYER 0 — Identity Reflex                   (seconds)
LAYER 1 — Quick Interview                   (90 seconds, install-time)
LAYER 2 — Recent Context Scan               (5 minutes, install-time)
LAYER 3 — Background Deepening              (hours, idle-throttled)
LAYER 4 — Continuous Observation            (always-on, lightweight)
LAYER 5 — Life-Event Detector               (subscribed to streams)
LAYER 6 — Plural Personas (auto-classifier) (per-turn)
LAYER 7 — Curious Companion                 (weekly cadence)
```

### Layer 0 — Identity Reflex

**What:** read what the user has already presented to themselves on the system.

**Sources (zero-consent — all already on the user's own machine):**
- `$USER`, `$HOME`, `socket.gethostname()`
- `git config --global user.email` and `user.name`
- `~/.gitconfig` for additional emails
- `~/Library/Mail/V*/MailData/Accounts.plist` for mail accounts (FDA-gated; falls back to None)
- `~/Library/Application Support/Google/Chrome/Default/Preferences` for Chrome account email (best-effort)
- macOS Contacts.app `me` card via AppleScript `tell application "Contacts" to get name of my card`

**Output:** `IdentityFacts` dataclass with `name`, `emails: list[str]`, `phones: list[str]`, `github_handle: str | None`, `city: str | None`, `primary_language: str` (system locale).

**Cost:** <1 second.

**Persistence:** writes Identity nodes + edges to F4 user-model graph with `source="identity_reflex"` and `confidence=1.0`.

### Layer 1 — Quick Interview

**What:** five thoughtful questions at install time, pre-personalized using Layer 0 data.

**Trigger:** `opencomputer profile bootstrap` command, or auto-prompted on first chat if no `bootstrap_complete` marker exists.

**Question shape (template-driven, Jinja2):**
```
Hi {{name}}! I'm OpenComputer — your local agent.
Before we start, 5 quick questions so I can be useful from the get-go:

1. What are you working on this week? (one sentence is fine)
2. What's on your mind right now — anything I should know?
3. How do you prefer responses — concise and action-first, or thorough?
4. Anything I should NOT do without asking? (e.g. "never send emails without confirming")
5. Anything else about you that would help me help you?
```

**Persistence:** answers go into the user-model graph as `user_explicit` edges with `confidence=1.0` and `source_reliability=1.0`.

**Skip path:** `--skip` flag persists `bootstrap_complete=True` with empty answers; agent still has Layer 0 identity.

### Layer 2 — Recent Context Scan

**What:** a 5-minute one-shot ingestion of "what's happening this week" so the agent has current context, not just identity.

**Sources (consent-gated, per-source):**

| Source | Window | Consent capability |
|---|---|---|
| Files modified in `~/Documents`, `~/Desktop`, `~/Downloads` | last 7 days | `ingestion.recent_files` (IMPLICIT) |
| Calendar events | next 7 days | `ingestion.calendar` (EXPLICIT, FDA-gated) |
| Browser history | last 7 days | `ingestion.browser_history` (EXPLICIT) |
| Git log across detected repos in `~/Vscode`, `~/Projects`, etc. | last 7 days | `ingestion.git_log` (IMPLICIT) |
| Telegram/iMessage threads (if FDA granted) | last 7 days | `ingestion.messages` (EXPLICIT) |

**Pipeline:** each source enumerates artifacts → batches them → sends to local LLM extractor (Ollama via subprocess) → extractor returns structured JSON → JSON becomes motifs → motifs become user-model edges.

**LLM extraction prompt template:**
```
Given this artifact (file/event/commit/message), extract:
- topic: 1-3 words
- people: list of names mentioned
- intent: what is the user trying to do (one sentence)
- sentiment: positive / neutral / negative / unknown
- timestamp: ISO 8601
Return JSON only.
```

**Throttle:** runs at install-time only. Re-run on `opencomputer profile refresh`.

### Layer 3 — Background Deepening

**What:** continuation of Layer 2 over a wider window — 30 days → 90 days → 1 year → all-time. Idle-throttled (CPU < 20%, laptop plugged in, not in use).

**Pause/resume:** state persisted as `<profile_home>/profile_bootstrap/deepening_cursor.json`. Resumes from last completed window after crash or user activity.

**Storage:** raw artifact contents (deduplicated, content-addressed) live in `<profile_home>/profile_bootstrap/raw_store/`. Derived motifs flow through existing F2 SignalEvent bus → F3 motif inference → F4 graph importer.

**Embedding:** each chunk gets a vector embedding via local model (BGE-small, ~30MB) stored in Chroma at `<profile_home>/profile_bootstrap/vector/`. Used by Layer 7 (Curious Companion) for similarity queries.

**Cost ceiling:** disk usage capped at user-configurable limit (default 10GB). When ceiling hit, oldest raw is GC'd; vectors + motifs survive.

### Layer 4 — Continuous Observation

**What:** always-on, lightweight signal stream from active use.

**Sources:**

| Source | Capture rate | Mechanism |
|---|---|---|
| Browser tab activity | every URL change | Browser extension (Chrome MV3) → POST to `http://127.0.0.1:18791/browser-event` |
| File system activity | every change in user-allowed dirs | `fswatch` subprocess (macOS) / `watchdog` (cross-platform) |
| App focus changes | every app switch | `NSWorkspace` notifications via PyObjC |
| Window title sampling | every 30s | AppleScript poll |
| Voice (optional, future) | wake-word activated | Whisper.cpp subprocess |

**Output:** all events fan out into the existing `opencomputer.ingestion.bus` TypedEventBus as `SignalEvent`s.

**Privacy posture:** events carry **labels and metadata, never raw content** — same stance as F2/F3 motif store. Browser sends URL + page title + visit duration, not page text. File watcher sends path + size + mtime, not contents (Layer 3 picks up content separately when artifact ages out of "fresh").

### Layer 5 — Life-Event Detector

**What:** named pattern matchers subscribed to the F2 SignalEvent bus that watch for life-event signatures.

**Initial pattern library (each is a small Python class implementing `LifeEventPattern`):**

| Pattern | Signature | Surfacing |
|---|---|---|
| `JobChange` | sudden drop in work-mail volume + new contacts on LinkedIn + searches for "resignation"/"severance"/"unemployment" | Surface gently in conversation: "noticed your work rhythm shifted — anything you want to talk about?" |
| `ExamPrep` | repeated tab visits to `*.edu`, `khanacademy`, etc. + concentrated time on one topic | "looks like you've been deep in [topic] — exam coming up?" |
| `RelationshipShift` | sudden drop in messages with one frequent contact + late-night browsing pattern + searches for therapy/dating apps | NEVER surface unprompted; lower internal "tone" weight only |
| `HealthEvent` | searches for symptoms + medical sites + prescription-finder visits | NEVER surface unprompted; hide health-correlated nodes from public-facing summaries |
| `Travel` | hotel/airline searches + maps activity for non-home locations + calendar entries with location | Pre-populate context: "trip to Mumbai next Tuesday — want me to draft a packing list?" |
| `Burnout` | declining file-edit volume + reduced commit frequency + late-night activity creep + sleep schedule shifts | NEVER surface as "burnout"; gentle "how are you doing this week?" cadence increase |

**Confidence threshold:** each pattern has a `surface_threshold` (default 0.7). Below threshold, pattern stays internal — feeds the user-model with low-confidence edges but doesn't fire conversational hooks.

**User control:** `opencomputer profile patterns list / mute <pattern> / unmute <pattern>` CLI surface. Patterns can be muted permanently or for a window.

### Layer 6 — Plural Personas

**What:** auto-detected mode classifier. The user is not one person; they're "Saksham coding" / "Saksham trading" / "Saksham relaxed" / "Saksham triaging admin" / "Saksham learning." Each persona has its own preferences + system-prompt overlay.

**Source ancestry:** Hermes Agent ships a multi-persona system at `sources/hermes-agent/personas/` — port the storage shape and switching logic. OpenComputer's existing manual `/persona` slash command (Phase 7.A) becomes the override path; Layer 6 is the auto-detector.

**Classifier inputs (per turn):**
- Foreground app (from Layer 4 NSWorkspace events)
- Time of day + day of week
- Recent file activity (last 30 minutes)
- Last 3 messages content (lightweight keyword + LLM)

**Classifier output:** persona ID from a configurable registry. Default registry: `coding`, `trading`, `relaxed`, `admin`, `learning`, `unknown`.

**Persona overlay:** each persona's `<profile_home>/personas/<id>.yaml` declares:
```yaml
name: coding
system_prompt_overlay: "User is in coding mode — be concise, tool-heavy, default to technical depth."
preferred_tone: terse
preferred_response_format: bullet
disabled_capabilities: []
```

**Switching cadence:** classifier runs at start of each user turn. Persona changes log to audit ("user switched from `relaxed` to `coding` at T+2.3s"). User can override with `/persona <id>` (existing manual surface).

### Layer 7 — Curious Companion

**What:** a weekly background pass that:

1. Inspects the user-model graph for **gaps** (low-confidence nodes that should be high-confidence) and **hypotheses** (medium-confidence inferences worth confirming).
2. Generates 1-3 indirect questions per week using a local LLM with prompt template:
   ```
   Based on these observations, formulate ONE indirect, friendly question
   to ask the user that would help confirm or deny the hypothesis without
   sounding like an interrogation. Phrase it like a curious friend would.

   Observations: {observations}
   Hypothesis: {hypothesis}
   ```
3. Queues the question for delivery at a contextually appropriate moment (next chat session, or via the channel adapter the user prefers).
4. On answer, writes back to the user-model graph and lowers the gap's "needs-question" weight.

**Cadence:** runs Sunday 11 AM local time by default (configurable). Skips if user has been inactive for 7+ days.

**Surfacing policy:** never interrupts an active task. Questions are queued; delivered on next idle moment in chat ("by the way, while you're here…").

**User control:** `opencomputer profile companion mute / unmute / cadence <weekly|monthly|never>`.

## Storage strategy

| Layer | Storage |
|---|---|
| Raw artifacts (Layer 3) | Content-addressed store at `<profile_home>/profile_bootstrap/raw_store/` |
| Vector embeddings (Layer 3) | Chroma DB at `<profile_home>/profile_bootstrap/vector/` |
| Structured user-model (all layers) | Existing F4 `<profile_home>/user_model/graph.sqlite` (SQLite + FTS5) |
| Full-text recall over filesystem/mail/messages | macOS Spotlight via `mdfind` (free, native, no maintenance) |
| Life-event detector state | `<profile_home>/profile_bootstrap/patterns_state.json` |
| Persona registry | `<profile_home>/personas/*.yaml` |
| Curious-companion question queue | `<profile_home>/profile_bootstrap/companion_queue.json` |

**Why Spotlight as FTS instead of FTS5 over our own corpus:**
- Spotlight already indexes user's filesystem, mail, contacts, messages, calendar (when FDA granted)
- Queryable via `mdfind` shell command or `NSMetadataQuery` (PyObjC) — no maintenance burden
- Saves ~1/3 of engineering vs. building our own FTS pipeline
- Cost: Spotlight is macOS-only; Linux/Windows fallback uses `tantivy` (Rust FTS, MIT-licensed) in a future cross-platform pass

## License posture

OpenComputer is MIT. All dependencies must be MIT-compatible (MIT, BSD, Apache 2.0, ISC):

| Dependency | License | Use |
|---|---|---|
| Ollama | MIT | Local LLM runtime for extraction |
| llama.cpp | MIT | Underlying inference (via Ollama) |
| BGE-small embedding model | MIT | Local embeddings for vector store |
| Chroma | Apache 2.0 | Local vector DB |
| Whisper.cpp | MIT | Local STT (Layer 4 voice, future) |
| PyObjC | MIT | macOS native API bindings |
| Playwright (if used for browser auto in V4) | Apache 2.0 | Headless browser, future |
| Open Interpreter (existing OI bridge) | AGPL-3.0 | Subprocess-isolated; out-of-process boundary preserved |
| Honcho (existing extension) | AGPL-3.0 | Docker subprocess only; not vendored |

**No AGPL imports in core.** Existing patterns (OI subprocess wrapper, Honcho Docker overlay) preserved.

## Staging — MVP → V2 → V3 → V4

This spec covers the full vision. **Implementation is staged**, with each stage shipped + dogfooded before the next is started.

### MVP (this plan: `2026-04-26-layered-awareness-mvp.md`)

**Includes:** Layer 0 (Identity Reflex) + Layer 1 (Quick Interview) + Layer 2 (Recent Context Scan) + Layer 4 minimal (browser extension capturing tab events only).

**Ships:** in ~3 weeks of focused work.

**Deliverable:** `opencomputer profile bootstrap` command. Run once; agent goes from blank to "knows your name, current week, recent context" in <6 minutes.

**Dogfood gate:** 1-2 weeks of real use before V2 starts.

### V2 — Background Deepening (separate plan, post-dogfood)

Adds Layer 3 (idle-throttled deep ingest of historical mail/browser/files/git over 30d → 90d → all-time). Adds raw artifact store + vector index. Adds Spotlight integration for FTS.

**Ships:** ~2 weeks.

### V3 — Patterns + Personas (separate plan)

Adds Layer 5 (Life-Event Detector with 6 starter patterns) + Layer 6 (Plural Personas, port from Hermes).

**Ships:** ~2-3 weeks.

### V4 — Curious Companion + advanced ingestion (separate plan)

Adds Layer 7 (Curious Companion weekly question loop). Optionally Layer 4 voice ingestion (Whisper). Optionally headless-browser scraping for cookie-bearing platforms (LinkedIn / X / IG / FB / Reddit) for users who want their public profiles ingested.

**Ships:** ~2 weeks.

**Total full-build:** ~9-10 weeks elapsed time, but MVP delivers within 3 weeks with most of the daily-use value.

## Trade-offs and alternatives considered

### Alternative 1 — "Just ask"
Skip ingestion entirely. Agent learns from chat conversations only. **Rejected:** that's what every cloud LLM already is. We'd ship no edge.

### Alternative 2 — "Hermes-style"
Use only `MEMORY.md` + `USER.md` + chat history + Honcho synthesis. **Rejected:** slow to learn, no current-context awareness, doesn't satisfy "useful in minutes" success criterion.

### Alternative 3 — "The Firehose"
Capture everything raw (screen recording, audio, all keystroke metadata), defer all extraction to query time. **Rejected:** disk usage 100GB+/year, no fast surfaces for "what does the agent know about me," every query expensive.

### Alternative 4 — "The Distillery"
Aggressive eager LLM extraction → discard raw → work only with motif graph. **Rejected:** lossy; if extraction logic improves, no way to re-derive.

### Alternative 5 — "The Index" (a.k.a. vanilla Approach 3 from brainstorm)
Build raw store + FTS5 + vector + graph as four parallel indices. **Rejected:** doesn't deliver "useful in minutes" — bootstrap pass is too long. **Refined into this Layered Awareness design** that tiers ingestion by latency.

### Alternative 6 — Layered Awareness without Plural Personas
Skip Layer 6, use only the existing manual `/persona` command. **Considered, rejected.** Hermes's auto-classifier pattern is well-tested, port is small, and the asymmetry between "agent watches everything but treats user as one mode" is unnecessarily flat.

### Alternative 7 — Skip Layer 7 (Curious Companion)
The whole "agent asks indirect questions" loop is significant complexity. **Considered, accepted as V4-deferred, not skipped.** It's the difference between "agent has data on you" and "agent feels like a friend" — qualitative threshold matters too much to drop.

### Alternative 8 — Skip browser extension; parse history files only
**Considered, rejected for MVP.** History files are post-hoc, miss DOM context, file-locked when browser is open. Extension is more invasive but ~10x cleaner signal. User explicitly OK'd shipping an extension.

## Open questions for V2+

These are NOT blocking MVP but should be answered before V2 starts:

1. **Cross-platform FTS strategy.** Spotlight is macOS-only. Linux uses `recoll` or `tantivy`? Windows uses `Windows Search` or `tantivy`?
2. **Vector DB persistence size cap.** When raw store hits its ceiling, do we GC vectors or keep them?
3. **Persona auto-detection accuracy.** What's the false-switch rate the user finds tolerable? 5% per turn? 1%?
4. **Life-event detector tuning.** Each pattern's confidence threshold needs calibration on real data. How long of a dogfood window before V3 ships defaults?
5. **Curious-companion delivery channel.** Always in-chat? Or push-notify via Telegram for high-confidence questions?
6. **Headless-browser-with-cookies for public-web ingestion (V4).** Worth building? Or call it "out of scope, use the existing WebFetch/WebSearch surface for ad-hoc public-web queries"?

## Consent-gate integration (F1)

Each ingestion source declares a capability claim. F1 ConsentGate enforces at runtime:

```python
F1_CAPABILITIES["ingestion.recent_files"] = ConsentTier.IMPLICIT
F1_CAPABILITIES["ingestion.calendar"] = ConsentTier.EXPLICIT
F1_CAPABILITIES["ingestion.browser_history"] = ConsentTier.EXPLICIT
F1_CAPABILITIES["ingestion.git_log"] = ConsentTier.IMPLICIT
F1_CAPABILITIES["ingestion.messages"] = ConsentTier.EXPLICIT
F1_CAPABILITIES["ingestion.continuous_observation"] = ConsentTier.EXPLICIT
F1_CAPABILITIES["ingestion.browser_extension"] = ConsentTier.EXPLICIT
F1_CAPABILITIES["pattern.life_event_detect"] = ConsentTier.IMPLICIT
F1_CAPABILITIES["companion.ask_question"] = ConsentTier.IMPLICIT
```

User can revoke any capability via `opencomputer consent revoke <id>`. Revocation immediately stops the corresponding ingestion. Existing audit-log infrastructure captures every ingestion + every revocation.

## File map (created/modified across all stages)

```
opencomputer/profile_bootstrap/             ← NEW package, MVP
├── __init__.py
├── identity_reflex.py                      ← Layer 0
├── quick_interview.py                      ← Layer 1
├── recent_scan.py                          ← Layer 2
├── llm_extractor.py                        ← shared LLM extraction helper
├── deepening.py                            ← Layer 3 (V2)
├── continuous_observation.py               ← Layer 4 (V2)
├── life_event_detector.py                  ← Layer 5 (V3)
├── persona_classifier.py                   ← Layer 6 (V3)
└── curious_companion.py                    ← Layer 7 (V4)

extensions/browser-bridge/                  ← NEW plugin, MVP
├── plugin.json
├── plugin.py
├── adapter.py                              ← Python listener (HTTP + bus publish)
└── extension/                              ← Chrome MV3 extension
    ├── manifest.json
    ├── background.js
    └── content.js

opencomputer/cli_profile.py                 ← MODIFIED (new `bootstrap` subcommand)
opencomputer/agent/prompt_builder.py        ← MODIFIED (inject user-model knowledge)
opencomputer/agent/prompts/base.j2          ← MODIFIED (new {{ user_facts }} block)
opencomputer/agent/consent/capability_taxonomy.py  ← MODIFIED (new ingestion.* claims)
plugin_sdk/core.py                          ← MODIFIED (extend SignalEvent metadata)

tests/test_profile_bootstrap_*.py            ← NEW (one per layer)
tests/test_browser_bridge.py                ← NEW
docs/superpowers/plans/2026-04-26-layered-awareness-mvp.md  ← MVP plan
```

## End-of-spec review

This spec was self-reviewed for: placeholder absence, internal consistency, scope (single subsystem with clear staging), no ambiguity in requirements. Spec covers MVP through V4 in scope; MVP is the single deliverable for the first plan.
