# OpenComputer Evolution — User Guide

> **Status: B4 landed.** Reflection, skill synthesis, prompt-evolution proposals, monitoring dashboard, and capability-atrophy detection all work. Only **B3 (auto-collection of trajectories from real agent runs)** is still pending — it depends on Session A's TypedEvent bus (`opencomputer/ingestion/bus.py`), which doesn't exist yet. Until B3 ships, you seed trajectories manually for dogfood.

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
| **B1** | ✅ Landed (PR #41) | Subpackage skeleton; trajectory dataclasses; SQLite storage; rule-based reward function; reflection + synthesis stubs |
| **B2** | ✅ Landed (PR #58) | GEPA-style reflection engine wired to provider registry; skill synthesis with III.4 hierarchical layout (atomic write + path-traversal guard); CLI (`reflect`, `skills list/promote`, `reset`); Jinja2 prompt templates |
| **B3** | ⏸ Blocked on Session A's TypedEvent bus | Auto-collection of trajectories from real agent runs (`opencomputer/ingestion/bus.py` doesn't exist yet on main) |
| **B4** | ✅ Landed (this branch) | Migration 002 (reflections + skill_invocations + prompt_proposals tables); `PromptEvolver` (diff-only, never auto-applies); `MonitorDashboard` (reflection history + reward trend + atrophy flags); CLI (`prompts list/apply/reject`, `dashboard`, `skills retire`, `skills record-invocation`) |

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

## What's actually usable today (B2)

You can now run the full reflection → synthesis loop manually. **Trajectory auto-collection is still B3** (waits for Session A's TypedEvent bus), so until then you can either: (a) try the CLI against a fresh empty trajectory store and see the "no data" path, or (b) seed the SQLite DB with synthetic trajectories for dogfood evaluation.

### CLI commands

The `opencomputer evolution` subapp must be wired into the main CLI before `opencomputer evolution …` works. Until Session A folds it in via a one-line PR, you can invoke it directly:

```bash
# Direct invocation (works today):
python -m opencomputer.evolution.entrypoint reflect --dry-run
python -m opencomputer.evolution.entrypoint skills list
python -m opencomputer.evolution.entrypoint skills promote <slug>
python -m opencomputer.evolution.entrypoint reset --yes

# After Session A wires the subapp in (cli.py +1 line):
opencomputer evolution reflect --window 30 --dry-run
opencomputer evolution skills list
opencomputer evolution skills promote read-then-edit
opencomputer evolution reset --yes
```

For Session A to wire the subapp in, the change in `opencomputer/cli.py` is one line:

```python
from opencomputer.evolution.entrypoint import evolution_app
app.add_typer(evolution_app, name="evolution")
```

### Dogfood gate — the next decision point

Once trajectories exist (either via B3 auto-collection or hand-seeded), run:

```bash
opencomputer evolution reflect              # real reflection — calls your configured provider
opencomputer evolution skills list          # see what landed in the quarantine
opencomputer evolution skills promote <slug>  # if a skill looks useful, promote it
opencomputer evolution reset --yes          # if everything's noise, wipe and try again
```

**Does the synthesized output actually help your agent?** That's the gate for whether B3 (auto-collection) and B4 (prompt evolution + dashboard) are worth building. Run the loop a few times against real workflows, then signal go / no-go.

### Programmatic surface

```python
from opencomputer.evolution import (
    TrajectoryEvent, TrajectoryRecord,                # data shapes
    new_record, with_event,                           # ergonomic builders
    RewardFunction, RuleBasedRewardFunction,          # scoring
    ReflectionEngine, Insight, SkillSynthesizer,      # working pipeline (B2)
)
from opencomputer.evolution.storage import (
    init_db, insert_record, list_recent, count_records,
)

# Real reflection requires a BaseProvider (use the one your provider plugin gives you):
engine = ReflectionEngine(provider=my_provider, model="claude-opus-4-7", window=30)
records = list_recent(limit=30)
insights = engine.reflect(records)

synth = SkillSynthesizer()
for ins in insights:
    if ins.action_type == "create_skill":
        path = synth.synthesize(ins)
        print(f"synthesized: {path}")
```

---

## Description style guide

Synthesized skill descriptions must be:

- **Third-person.** "Processes...", "Synthesizes...", "Generates...".  Never "I can help" or "You can use".
- **WHAT + WHEN.** The action verb phrase, then a "Use when..." clause.

### Examples

✅ Good:
- `Synthesizes git commit messages from staged diffs. Use when the user asks for help writing commit messages.`
- `Detects repeated grep-then-edit patterns in a session. Use when investigating workflow patterns.`

❌ Bad:
- `I can help you write commit messages.`
- `Use when you want to write commits.` (no WHAT)
- `Helps with git stuff.` (no WHEN, vague WHAT)

The synthesis prompt enforces this voice. The post-synthesis validator catches non-compliant descriptions before they're written. See [docs/skills/AUTHORING.md](../skills/AUTHORING.md) for the full spec.

---

## What's coming in B3 + B4 (gated on dogfood feedback)

**B3** — auto-collection of trajectories from real agent runs. Subscribes (read-only) to Session A's TypedEvent bus when it lands; `opencomputer/ingestion/bus.py` is the dependency. CLI additions: `opencomputer evolution enable / disable`, `opencomputer evolution trajectories show --limit 50`.

**B4** — prompt-evolution proposals (diff-only, never auto-applied) + monitoring dashboard + capability-atrophy detection. CLI additions: `opencomputer evolution prompts list / apply / reject`, `opencomputer evolution dashboard`, `opencomputer evolution skills retire`.

If B2 dogfood shows the synthesized output isn't useful, B3 + B4 are deferred indefinitely and evolution ships at "reflect-on-demand only".

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
