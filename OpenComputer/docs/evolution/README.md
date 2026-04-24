# OpenComputer Evolution — User Guide

> **Status: B1 (skeleton).** This document is the user-facing entry point. The subpackage exists in `opencomputer/evolution/`, but reflection + skill synthesis logic land in **B2** (next phase). Read this to understand what's shipping, what's coming, and the safety guarantees.

---

## What is "Evolution"?

Evolution is OpenComputer's opt-in self-improvement subsystem. It does three things:

1. **Watches** what you and the agent do together — tool calls, outcomes, whether the session ended cleanly.
2. **Reflects** on batches of those traces (B2) — using an LLM to spot patterns: "you keep using `Grep` then `Read` together; here's a `GrepRead` skill that combines them."
3. **Proposes** new skills and prompt edits — written to a quarantined namespace where you review them before they're allowed to influence the live agent.

Inspired by the GEPA reflection pattern from [Hermes Self-Evolution](https://github.com/NousResearch/hermes-agent-self-evolution) (MIT). See `docs/evolution/source-map.md` for the deep-scan and `docs/evolution/design.md` for our architecture.

---

## Status table

| Phase | Status | What ships |
|---|---|---|
| **B1** | ✅ Landed (this branch) | Subpackage skeleton; trajectory dataclasses; SQLite storage; rule-based reward function; reflection + synthesis stubs |
| **B2** | Coming | GEPA-style reflection engine; skill synthesis to quarantine namespace; CLI surface (`opencomputer evolution reflect`, `... skills list / promote`, `... reset`) |
| **B3** | Awaits Session A's TypedEvent bus | Auto-collection of trajectories from real agent runs |
| **B4** | After B3 | Prompt-evolution proposals (diff-only, never auto-applied) + monitoring dashboard + capability-atrophy detection |

Each phase ships behind PR review. Nothing here is auto-enabled.

---

## Safety guarantees (the things you should care about)

These are **load-bearing** — verify them in code if you're skeptical (paths in parens):

1. **Disabled by default.** `config.evolution.enabled` defaults to `False`. Reflection only runs when you explicitly invoke it (or, in B3+, opt in to auto-collection). (`opencomputer/evolution/storage.py`, `OpenComputer/docs/evolution/design.md` §10)

2. **Quarantine namespace.** Synthesized skills land in `<profile_home>/evolution/skills/<slug>/`, never in your main skills dir. Promotion requires `opencomputer evolution skills promote <slug>` (B2). The original always stays in the quarantine as the audit trail. (`OpenComputer/docs/evolution/design.md` §8)

3. **No auto-prompt-mutation.** Prompt-evolution (B4) proposes diffs to `<profile_home>/evolution/prompt_proposals/`. You apply them with `opencomputer evolution prompts apply <id>` or reject. The system **never edits your prompts on its own**. (`OpenComputer/docs/evolution/design.md` §11)

4. **Tool-names-only privacy.** Trajectory records store tool names + outcome flags + small structured metadata. They do **not** store raw prompt text. The `metadata` dict is validated at construction time — any string value over 200 characters is rejected with a `ValueError`. Raw prompts remain in the session DB; evolution references them by id only. (`opencomputer/evolution/trajectory.py::TrajectoryEvent.__post_init__`)

5. **Profile-isolated.** Every path resolves through `_home()` (`opencomputer/agent/config.py`), which honors `OPENCOMPUTER_HOME`. Switching profiles with `opencomputer -p <profile>` swaps the entire evolution store, including the quarantine.

6. **Conservative reward.** The MVP reward function (`opencomputer/evolution/reward.py`) is rule-based, not LLM-judge. Three narrow signals (tool success rate + user-confirmed cue + completion flag), no length component (so verbose-but-useless responses are not rewarded), no latency component. LLM-judge reward is explicitly post-v1.1.

7. **Rollback path.** `opencomputer evolution reset` (B2) deletes the evolution DB + quarantine + prompt proposals after `--yes` confirmation. Your `sessions.db` is untouched. If anything goes wrong, this returns you to a clean state in one command.

---

## What's actually usable today (B1)

**Nothing user-visible yet.** B1 is foundational — dataclasses + storage + reward — with no CLI, no auto-collection, no reflection. The point of landing B1 separately is that downstream work (B2 reflection, B3 bus subscription) plugs into a stable, tested base.

If you want to peek at what's in the package right now:

```python
from opencomputer.evolution import (
    TrajectoryEvent, TrajectoryRecord,    # the data shapes
    new_record, with_event,                # ergonomic builders
    RewardFunction, RuleBasedRewardFunction,  # scoring
    ReflectionEngine, Insight, SkillSynthesizer,  # stubs (B2)
)
```

The stubs raise `NotImplementedError("...lands in B2...")` — that's the contract for B1.

---

## What's coming in B2

CLI surface (final shape locked at design time):

```
opencomputer evolution reflect [--window 30] [--dry-run]   # manual reflection trigger
opencomputer evolution skills list                          # show synthesized skills
opencomputer evolution skills promote <slug>                # quarantine → main skills
opencomputer evolution reset                                # rollback (DB + quarantine wipe)
```

After B2 ships, there's a **dogfood gate**: you (the user) try `opencomputer evolution reflect` against a real session, decide if the synthesized skills are useful, and signal whether B3 + B4 are worth building. If the answer is "the output is junk", evolution stops shipping at "reflect-on-demand only" and we don't expand it.

---

## Where to look in code

| Concern | File |
|---|---|
| Data shapes | `opencomputer/evolution/trajectory.py` |
| SQLite + migrations | `opencomputer/evolution/storage.py` + `opencomputer/evolution/migrations/*.sql` |
| Reward scoring | `opencomputer/evolution/reward.py` |
| Reflection (stub) | `opencomputer/evolution/reflect.py` |
| Skill synthesis (stub) | `opencomputer/evolution/synthesize.py` |
| Tests | `tests/test_evolution_*.py` |
| Architecture decisions | `OpenComputer/docs/evolution/design.md` |
| Hermes deep-scan | `OpenComputer/docs/evolution/source-map.md` |
| Coordination protocol (Session A ↔ B) | `OpenComputer/docs/parallel-sessions.md` |

---

## FAQ

**Q: Will Evolution slow down the agent?**
A: B1 — no, nothing runs unless you call into the package. B3 — auto-collection writes to a separate SQLite file in a background thread; the publisher (agent loop) does not block.

**Q: Does Evolution send my prompts to Anthropic?**
A: No. Trajectory records store tool names + outcome flags + small metadata, not prompt text. When B2 reflection runs, it sends the **trajectory rows** (tool sequence + outcomes + metadata previews) to whichever provider you've configured. You can read exactly what gets sent at `opencomputer/evolution/prompts/reflect.j2` once B2 ships.

**Q: Can a synthesized skill accidentally take destructive actions?**
A: A synthesized skill is just a SKILL.md file. It does nothing until your agent invokes it. It only reaches the agent's skill discovery pool after you run `opencomputer evolution skills promote <slug>`. Your existing skill-invocation safety (consent prompts, plan mode, etc.) applies unchanged.

**Q: How do I disable Evolution if it's making bad suggestions?**
A: `opencomputer evolution disable` (B3) turns off auto-collection. `opencomputer evolution reset --yes` wipes everything (DB + quarantine + proposals).

**Q: Is this AGPL-contaminated?**
A: No. We **do not vendor** Hermes Self-Evolution code (its license is MIT, but we chose to write OpenComputer-native code for architectural fit). Hermes's optional Phase 4 component (Darwinian Evolver) is AGPL v3 — we don't import it; if we ever integrate it, it'll be as a subprocess, never as a library.

---

*This README is updated each phase as new functionality ships. Last edit: B1 landing (2026-04-24).*
