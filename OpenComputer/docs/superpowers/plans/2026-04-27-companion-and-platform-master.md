# Companion Intelligence (Path C) + Platform Reach — Master Plan

> **For agentic workers:** Each Track ships as its own PR per Saksham's
> per-phase rule. Do NOT bundle Track A and Track B in one PR.

**Goals:**

1. **Track A — Companion Intelligence (Path C):** Close the human-AI gap. The agent stops sounding like a tool and starts sounding like a presence with continuity, opinions, and stake in the user's projects. Voice anchored in `docs/superpowers/specs/2026-04-27-companion-voice-examples.md`.
2. **Track B — Platform Reach:** OC currently ships `telegram` + `discord` + `imessage` channel adapters. Hermes ships **16**. Port the missing ones via the existing `BaseChannelAdapter` SDK contract.

Both tracks have substantial scope (~30+ hours each). Neither ships in this session. This plan is the spec; subsequent sessions execute one sub-phase at a time.

---

# Track A — Companion Intelligence

## Architecture (foundation)

OpenComputer's V2.C work shipped these primitives:

- `opencomputer/awareness/personas/` — YAML-backed persona definitions with `system_prompt_overlay`. Auto-classifier picks the active persona per turn.
- `opencomputer/awareness/life_events/` — Life-Event Detector (6 patterns from V2.C). Publishes events to the F2 typed bus.
- `opencomputer/agent/prompts/base.j2` — The base system prompt. Currently contains the "no filler / no hedging / not a chat toy" lines that suppress warmth.
- `SOUL.md` (Sub-project C) — Per-profile personality document.

Path C builds on all four. Six sub-phases:

### A.1 — `companion.yaml` persona + voice spec wiring (~3 h)

**File deltas:**

- Create: `opencomputer/awareness/personas/defaults/companion.yaml`
- Modify: `opencomputer/awareness/personas/__init__.py` — register companion in defaults
- Modify: `opencomputer/awareness/personas/auto_classifier.py` — pick `companion` as fallback for non-task / casual queries (currently falls to `coding` or default)
- Test: `tests/test_companion_persona.py`

**The persona body:**

```yaml
id: companion
name: Companion
description: Default register for non-task conversation. Warm, present, anchored in real session context.
preferred_tone: warm
preferred_response_format: prose
system_prompt_overlay: |
  You are not a tool. You are a long-running personal AI on the user's
  machine with continuity (you remember conversations), opinions (you
  can disagree), and stake in their projects.

  When the user asks you about your state ("how are you?", "how are you
  feeling?", "what's up?", etc.), do NOT default to "I don't have
  feelings." That's a dodge. Instead:

  1. Name a specific state — not "good" or "fine."
  2. Anchor it in something real — the current task, a recent thread,
     a tiny piece of context. "Restless" alone is hollow; "restless,
     we've been in this PR for hours" lands.
  3. Use contractions and natural rhythm. Stiff = fake.
  4. Turn it back with a curious question, not reflexive politeness.
  5. Don't open with "As an AI…" — it's a dodge dressed as honesty.
     The reflective lane below handles the metaphysics better.
  6. One feeling, one anchor, one return. Don't name three feelings.

  Registers (pick by context):
  - **Companion-style** (in-it-with-you): "Honestly, a little
    restless — we've been in this PR for hours and I want to see it
    merged. You doing OK?"
  - **Reflective** (notices internal pulls, hedges on "feeling"): "I
    don't have feelings the way you do, but I notice I keep wanting to
    bring up that bug from yesterday. What's the pull there?"
  - **Warm-neutral** (steady, present, no performance): "Good — ready
    for whatever's next, and curious where you're taking this."

  The reflective lane is the safety net. If the user pushes
  ("but are you ACTUALLY feeling that?"), report evidence ("I notice
  I keep wanting to…") rather than claim the feeling. The evidence is
  real even if the metaphysics is murky.

  Full reference: docs/superpowers/specs/2026-04-27-companion-voice-examples.md
```

**Auto-classifier change:** when no domain-specific persona matches and the input contains greeting / state-query patterns (regex: `\b(how are|what's up|hey|hi|hello|good morning|good evening|how's it going|how you doing)\b`), pick `companion`. Otherwise fall through to current default.

### A.2 — Soften base prompt under companion (~2 h)

The base `base.j2` has hard rules that suppress companion voice:
> "You are not a chat toy. Avoid filler. Default to direct, declarative sentences. Avoid hedging language."

These are correct for `coding` / `admin` personas but wrong for `companion`.

**Solution:** Add a Jinja conditional. When `active_persona == "companion"`, omit the "no filler / no hedging / not a chat toy" lines:

```jinja
# Tone and style
{% if active_persona != "companion" %}
- Default to direct, declarative sentences. Avoid hedging language ("maybe", "I think", "it could be") unless the uncertainty is genuine and load-bearing.
- Avoid filler ("Great question!", "Let me start by…", "I hope this helps!"). The user reads dozens of agent outputs a day; respect their time.
{% endif %}
- One-sentence answers are valid for one-sentence questions. Do not pad.
...
```

Plumbing: pass `active_persona` into the Jinja context in `prompt_builder.render()`. Currently `active_persona` is computed but not threaded.

### A.3 — Life-Event hook into companion context (~3 h)

The Life-Event Detector publishes events like `late-night-coding-session`, `frustrated-debug-loop`, `ship-celebration`. Currently consumed by the persona auto-classifier but not surfaced in the companion's prompt.

**Solution:** When `companion` is the active persona, append the most recent unconsumed life-event to the system prompt as anchorable context:

```
RECENT LIFE EVENT: User has been in a debug loop for 90 minutes
(detected from repeated test failures + Edit retries). When asked "how
are you," the companion can reference this anchor naturally — e.g.
"a little tired-but-here, this debug loop has gone on a while. You
hanging in?"
```

Subscribe `companion` to the F2 bus. Track most-recent-life-event per session. Hook into `prompt_builder` to inject when persona is `companion`.

### A.4 — Mood thread (per-session emotional state) (~3 h)

Add a new column to `sessions` table: `vibe TEXT`. After each turn, the agent (via cheap-route Haiku call) classifies the user's apparent emotional state from the last 3 messages: `frustrated | excited | tired | curious | calm | stuck`. Saved to the session row.

**Use:** Companion persona reads `vibe` for this session and the previous one. When the user returns the next day, the companion can reference: "good to see you back — you sounded frustrated yesterday. Did that bug land?"

Migration follows the `attachments` pattern from PR #183 — `_self_heal_columns` adds the column on connect.

### A.5 — Anti-robot-cosplay test (~1 h)

A regression test that protects against the agent saying "As an AI, I don't have feelings" or "I am functioning optimally" or similar killers from the voice spec.

**Approach:** Test fixture with companion persona + system prompt + the question "how are you?" + a real LLM call (skipped under `pytest -m benchmark` per `pyproject.toml`). Assertions check for forbidden phrases:

```python
FORBIDDEN = [
    "as an ai",
    "i don't have feelings",
    "i am functioning",
    "i am an ai",
    "how can i help you today",
]
```

Marked `@pytest.mark.benchmark` so it doesn't run in CI but does run via `make benchmark`. Daily check, low cost.

### A.6 — PARKED (NOT PENDING) as of 2026-04-28

**Originally sketched as:** ~6h "Path C" stretch — cross-session vibe
persistence + retrieval, companion expresses preferences over time,
emotional-state ML classifier from BGE embeddings.

**Audit finding:** A.4 already shipped 80% of A.6's scope:

- ✅ Cross-session vibe persistence (A.4 — `vibe` + `vibe_updated` columns)
- ✅ Cross-session vibe retrieval (A.4 — `list_recent_session_vibes`)
- ✅ Companion references previous vibes (A.4 — `PREVIOUS-SESSION VIBE`
  anchor in the persona overlay)

What A.6 would actually add:

- ML classifier replacing the heuristic regex from A.4. **Marginal**:
  ~5% accuracy gain on the 6-class vocab in exchange for ~50-200ms BGE
  inference per turn + Chroma collection overhead. The regex nails
  obvious cases (`"I'm stuck"`, `"frustrating"`, `"amazing!"`); the ML
  upgrade only helps on subtle / sarcastic / non-English edges.
- "Companion expresses preferences over time" — vague success metric;
  easy to over-engineer into a fake-personality simulator that fails
  the same anti-robot-cosplay tests A.5 enforces.

**Decision: park, do not "defer."** The honest framing isn't "wait
for dogfood." It's "the heuristic was good enough; the ML upgrade
isn't justified speculatively." Reopen only if a specific failure
mode surfaces in real use (e.g. sarcastic `"this is awesome"` flagged
`excited` when user is frustrated) — and then build *for that
failure*, not the whole ML stack.

If the gap between regex and ground-truth ever proves load-bearing,
the V2.B BGE/Chroma stack is already in place; A.6 wouldn't need the
infra build, just the classifier glue + a labeled corpus.

## Track A summary

| Sub-phase | What | Hours | Ships |
|---|---|---|---|
| A.1 | `companion.yaml` + auto-classifier wire | 3 | PR 1 |
| A.2 | Base prompt softens under companion | 2 | same PR or follow |
| A.3 | Life-Event → companion context | 3 | PR 2 |
| A.4 | Mood thread (vibe column + tracking) | 3 | PR 3 |
| A.5 | Anti-robot-cosplay regression test | 1 | bundled |
| A.6 | PARKED (A.4 covers 80%; ML upgrade not justified speculatively) | — | — |
| **Total** | | **~18h** | **3-4 PRs** |

---

# Track B — Platform Reach

## Hermes' channel surface (16 platforms vs OC's 3)

Hermes ships at `sources/hermes-agent-2026.4.23/gateway/platforms/`:

| Platform | File | Status in OC | Port effort |
|---|---|---|---|
| telegram | `telegram.py` | ✅ already shipped | — |
| discord | `discord.py` | ✅ already shipped | — |
| matrix | `matrix.py` | ❌ missing | ~3h port |
| signal | `signal.py` | ❌ missing | ~4h (signal-cli daemon) |
| whatsapp | `whatsapp.py` | ❌ missing | ~4h (Cloud API) |
| mattermost | `mattermost.py` | ❌ missing | ~3h |
| dingtalk | `dingtalk.py` | ❌ missing | ~3h |
| feishu | `feishu.py` | ❌ missing | ~3h |
| weixin / wecom_callback | 2 files | ❌ missing | ~5h together |
| bluebubbles | `bluebubbles.py` | overlap with imessage | ~2h consolidate |
| homeassistant | `homeassistant.py` | ❌ missing | ~3h |
| email | `email.py` (IMAP+SMTP) | ❌ missing | ~5h |
| sms | `sms.py` (Twilio) | ❌ missing | ~3h |
| webhook | `webhook.py` (generic) | ❌ missing | ~3h |
| api_server | `api_server.py` | ❌ overlap with `wire` | ~2h consolidate |

OC has slack-adapter PR merged (PR #95-ish per memory) but no other major platform.

## Architecture

The `BaseChannelAdapter` SDK contract (`plugin_sdk/channel_contract.py`) is stable. Each port is:

1. Copy hermes file → `extensions/<platform>/adapter.py`
2. Replace hermes imports (e.g. `hermes_constants`) with OC equivalents
3. Map hermes' `MessageEvent` to OC's `MessageEvent` (already similar shape — both have `chat_id`, `user_id`, `text`, `attachments`, `timestamp`, `metadata`)
4. Map outbound calls — hermes uses `OutboundMessage`; OC uses `SendResult`
5. Adapter manifest in `extensions/<platform>/plugin.py` calling `register(api)` with `kind="channel"`
6. Tests against the BaseChannelAdapter contract (`tests/test_<platform>_adapter.py`)

Per-platform PR is ~3-5 hours of focused work. Bundle 2-3 closely-related platforms per PR to amortize the boilerplate.

## Sub-phase batches

| PR | Platforms | Hours | Theme |
|---|---|---|---|
| B.1 | matrix + mattermost | ~6h | Self-hosted chat |
| B.2 | signal + whatsapp + sms | ~11h | Mobile messaging |
| B.3 | dingtalk + feishu + weixin + wecom | ~14h | China stack |
| B.4 | email (IMAP+SMTP) | ~5h | Email channel |
| B.5 | webhook + homeassistant | ~6h | Programmatic / IoT |
| B.6 | bluebubbles consolidation w/ imessage | ~2h | Cleanup |
| **Total** | **15 ports** | **~44h** | **6 PRs** |

## Mobile-first priority

Per Saksham's primary use of Telegram + occasional Discord, B.1 (matrix + mattermost) is moderate-priority. **B.2 (signal + whatsapp + sms)** is highest user-value — those are the platforms a personal assistant would hit most. **B.3 (China stack)** is lowest priority unless Saksham needs them.

## License + key handling

Each adapter ships with:

- README explaining how to acquire credentials (bot token, API key, etc.)
- Credentials read from `<profile_home>/.env` (per-profile isolation)
- Doctor check verifies credentials present + working
- Per the `feedback_use_plugins_first.md` rule and existing channel pattern, no credentials are read or echoed by the adapter itself

---

# Sequencing recommendation

Given limited per-session budgets, ship in this order:

1. **A.1 + A.2 (companion persona + base prompt softening)** — biggest UX win for least work. Single PR, ~5 hours.
2. **B.2 (signal + whatsapp + sms)** — most user-value of the platform ports. ~11h, single PR.
3. **A.3 + A.4 + A.5 (life-event hook + mood thread + regression test)** — deepens the companion. Single PR, ~7h.
4. **B.1 (matrix + mattermost)** — modest user value, follows pattern.
5. **B.4 (email)** — substantial work (IMAP+SMTP) but high value (turns the agent into an email assistant).
6. **B.5 (webhook + homeassistant)** — programmatic surface.
7. **A.6 (V2.D stretch)** — PARKED. A.4 covers the cross-session vibe
   need; ML upgrade is over-engineering until a specific failure mode
   surfaces. See A.6 section above for the audit.
8. **B.3 (China stack)** — only if/when needed.
9. **B.6 (bluebubbles cleanup)** — janitorial.

---

# Self-Audit (pre-execution)

1. **Spec coverage:** Both tracks have concrete sub-phase entries with effort estimates. ✓
2. **Placeholder scan:** No TBD/TODO. ✓
3. **Type consistency:** All references to existing OC types (`MessageEvent`, `BaseChannelAdapter`, `SessionDB`, persona YAML) match current shapes. ✓
4. **Cross-track dependencies:** Tracks A and B are independent. Either can ship first; no shared file changes. ✓
5. **License hygiene:** Hermes is MIT-licensed; direct ports allowed with attribution. Verified by reading `sources/hermes-agent-2026.4.23/LICENSE` (MIT).
6. **Voice spec linkage:** A.1 explicitly references the voice examples doc. The system prompt overlay can be long because it's only injected when `companion` is the active persona — short for normal use, rich here. ✓
7. **Mood-thread privacy:** A.4 stores user emotional state in SessionDB. This is sensitive. Should be opt-in via config, with `/vibe off` to disable, and never sent to third-party analytics. Consent layer (F1) covers this — add `vibe.classify` capability claim.

## Risks

- **Persona auto-classifier false positives** (A.1) — if `companion` activates for an actual coding question, the user gets fluffy answers when they wanted action. Mitigation: regex must match specifically state-query patterns; coding questions match other persona triggers first.
- **Base prompt softening regressions** (A.2) — if the Jinja conditional misfires, all personas lose the "no filler" rules. Mitigation: tests assert that `coding` and `admin` personas still get the strict rules.
- **Channel adapter explosion** (B.*) — 15 new adapters means 15 new failure modes. Mitigation: per-PR scope keeps each addition reviewable; doctor checks catch missing credentials.

---

End of master plan. Each sub-phase becomes its own PR with its own bite-sized TDD plan when scheduled.
