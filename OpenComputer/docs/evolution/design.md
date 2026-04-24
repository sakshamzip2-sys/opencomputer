# OpenComputer Evolution — Architecture & Design Decisions

> **Phase B1 design doc.** Companion to `source-map.md` (Hermes deep-scan).
> Audience: Session B implementers + Session A reviewers + future maintainers.

---

## 1. Goals (recap from plan)

The `opencomputer/evolution/` subpackage brings GEPA-style self-improvement into OpenComputer as a **self-contained**, **opt-in** subsystem that:

1. **Collects** (state, action, outcome, reward) trajectories from agent runs
2. **Reflects** on batches of trajectories (LLM call) to extract insights
3. **Synthesizes** new skills (and proposes prompt edits) from those insights
4. **Quarantines** generated artifacts in a separate namespace until the user opts to promote them
5. **Monitors** its own behavior (reward trend, atrophy flags, skills created vs. promoted)

**Hard constraint:** Evolution **subscribes** to events emitted by the agent loop; it never **modifies** the publisher. This is what makes parallel-session work safe.

---

## 2. Divergences from Hermes Self-Evolution (and why)

The Hermes reference (see `source-map.md`) is the closest existing implementation. We deliberately diverge on these points:

| Decision | Hermes | OpenComputer Evolution | Why |
|---|---|---|---|
| **Reward function** | LLM-as-judge (multi-dimensional rubric) | Rule-based (tool success + user-confirmed + task-completed) for MVP | LLM-judge is post-v1.1 (plan §scope; reduces cost + latency + reward-gaming risk) |
| **Optimizer** | DSPy GEPA loop | None in MVP; Insight → SkillSynthesizer | DSPy is a heavy dep + heavy API surface; bring later if dogfood justifies |
| **Skill format** | Tightly coupled to Hermes SKILL.md | III.4 hierarchical layout (SKILL.md + references/ + examples/) | OpenComputer has III.4 in main; reuse it |
| **Provider call** | Anthropic SDK directly | OpenComputer **provider registry** | Plan §refinements (self-audit assumption #2) — keeps Honcho overlay + plugin abstraction intact |
| **Storage** | JSONL files in output/ | SQLite with `schema_version` field | Matches OpenComputer pattern (`agent/state.py`); enables CRUD + windowed queries |
| **Skill destination** | Overwrites source SKILL.md | Quarantine namespace `_home() / "evolution" / "skills" /`; explicit promotion CLI | Plan §risks ("Synthesized skill collides with user-written skill") |
| **Reflection batch size** | N/A (DSPy controls) | Default 30, user-configurable | Plan §refinements (assumption #4) — context window safety on smaller models |
| **Trajectory contents** | Full session traces | Tool names + outcome flags + metadata; **no raw prompt text** | Plan §refinements (edge case #3 privacy) — prompts referenced by `session_db.messages` row id, not inlined |

**Borrowed conceptually** (from Hermes' source-map):
- Constraint-first thinking — synthesized skills must pass validation before promotion (B2 deliverable)
- Train/val/holdout separation — defer to B2's reflection design
- Two-stage relevance filtering — defer to B2 (when Insight ranking matters)

---

## 3. Module map (B1 deliverables in **bold**)

```
opencomputer/evolution/
├── __init__.py              ← B1 — version + public exports
├── trajectory.py            ← B1 — TrajectoryEvent, TrajectoryRecord (frozen+slots);
│                                   B3 will add register_with_bus()
├── storage.py               ← B1 — SQLite + self-contained migration runner
├── reward.py                ← B1 — RewardFunction Protocol + RuleBasedRewardFunction default
├── reflect.py               ← B1 — ReflectionEngine + Insight dataclass (B2 fills logic)
├── synthesize.py            ← B1 — SkillSynthesizer stub (B2 fills logic)
├── prompt_evolution.py      ← B4 — diff-only prompt mutation proposals
├── monitor.py               ← B4 — dashboard aggregations
├── entrypoint.py            ← B2 — `opencomputer evolution` typer subapp
├── cli.py                   ← B2 — CLI commands wired by entrypoint
├── prompts/
│   ├── reflect.j2           ← B2 — Jinja2 reflection prompt template
│   └── synthesize.j2        ← B2 — Jinja2 skill-content template
└── migrations/              ← B1 — numbered SQL files for self-contained migrations
    └── 001_evolution_initial.sql
```

---

## 4. Data shapes

### 4.1 `TrajectoryEvent` — single (state, action, outcome) tuple

```python
@dataclass(frozen=True, slots=True)
class TrajectoryEvent:
    session_id: str            # FK into agent_state.sessions.id (NOT inlined content)
    message_id: int | None     # FK into agent_state.messages.id, when applicable
    action_type: str           # "tool_call" | "user_reply" | "assistant_reply" | "error"
    tool_name: str | None      # PascalCase tool name if action_type == "tool_call"
    outcome: str               # "success" | "failure" | "blocked_by_hook" | "user_cancelled"
    timestamp: float           # Unix epoch seconds
    metadata: Mapping[str, Any]  # tool-specific extras (counts, sizes, exit codes — NEVER prompts)
```

**Privacy rule:** `metadata` is inspected at construction time; values that look like raw prompt text (length > 200 chars) are rejected with `ValueError`. References (ids, paths, counts, exit codes, tool names) only.

### 4.2 `TrajectoryRecord` — session-bounded sequence

```python
@dataclass(frozen=True, slots=True)
class TrajectoryRecord:
    id: int | None             # PK assigned at storage insert (None pre-insert)
    session_id: str
    schema_version: int        # = 1 for B1; bumps on schema evolution
    started_at: float
    ended_at: float | None     # None while session ongoing
    events: tuple[TrajectoryEvent, ...]  # tuple, not list — immutable
    completion_flag: bool      # whether session reached a clean terminal state
```

`schema_version` is per-record (not just per-DB) so old records remain readable after a schema migration adds a column — readers detect older versions and adapt.

### 4.3 `Insight` — output of reflection (B1 stub, B2 implementation)

```python
@dataclass(frozen=True, slots=True)
class Insight:
    observation: str
    evidence_refs: tuple[int, ...]      # trajectory record ids supporting the observation
    action_type: Literal["create_skill", "edit_prompt", "noop"]
    payload: Mapping[str, Any]          # action-type-specific (slug, draft text, etc.)
    confidence: float                   # 0.0–1.0
```

### 4.4 SQLite schema (B1 migration `001`)

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trajectory_records (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    record_schema_version INTEGER NOT NULL,
    started_at      REAL NOT NULL,
    ended_at        REAL,
    completion_flag INTEGER NOT NULL DEFAULT 0,
    reward_score    REAL,                  -- NULL until reward computed
    created_at      REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_traj_session ON trajectory_records(session_id);
CREATE INDEX IF NOT EXISTS idx_traj_ended_at ON trajectory_records(ended_at);

CREATE TABLE IF NOT EXISTS trajectory_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id       INTEGER NOT NULL,
    seq             INTEGER NOT NULL,      -- ordering within record
    message_id      INTEGER,               -- FK into agent_state.messages.id
    action_type     TEXT NOT NULL,
    tool_name       TEXT,
    outcome         TEXT NOT NULL,
    timestamp       REAL NOT NULL,
    metadata_json   TEXT,                  -- JSON serialised, prompt-text-stripped
    FOREIGN KEY (record_id) REFERENCES trajectory_records(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_record ON trajectory_events(record_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_tool   ON trajectory_events(tool_name);
```

**Why a separate database file?** Plan §risks edge case #2 ("DB lock contention"): keeping evolution in `_home() / "evolution" / "trajectory.sqlite"` (separate from `sessions.db`) means a stuck reflection write can't block the agent loop's session writes.

---

## 5. Storage & migration

### 5.1 Self-contained migration runner (B1)

F1's framework is not yet on main (CLAUDE.md §5 confirms Sub-project F is parked). B1 ships a **minimal** migration runner that we'll later swap for F1 once it lands.

Design:
- `migrations/NNN_<name>.sql` — numeric prefix sets order
- `schema_version` table records `(version, applied_at)` per applied migration
- `apply_pending(conn)` runs every NNN > MAX(version) in order
- Idempotent: re-running on an up-to-date DB is a no-op

```python
def apply_pending(conn: sqlite3.Connection) -> list[int]:
    """Apply all pending migrations in order. Returns versions newly applied."""
    _ensure_schema_version_table(conn)
    current = _max_applied(conn)
    pending = [m for m in _discover_migrations() if m.version > current]
    applied: list[int] = []
    for m in sorted(pending, key=lambda x: x.version):
        with conn:  # atomic per migration
            conn.executescript(m.sql)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (m.version, time.time()),
            )
        applied.append(m.version)
    return applied
```

**Refactor path:** Once Session A ships F1's migration framework (likely as `opencomputer/agent/migrations/`), we replace `evolution.storage.apply_pending` with a thin call into the F1 runner. Tests continue to validate end-to-end semantics.

### 5.2 Path resolution

```python
from opencomputer.agent.config import _home  # canonical profile-aware home

def evolution_home() -> Path:
    p = _home() / "evolution"
    p.mkdir(parents=True, exist_ok=True)
    return p

def trajectory_db_path() -> Path:
    return evolution_home() / "trajectory.sqlite"
```

We **import** `_home` rather than duplicate it — single source of truth for profile-awareness. (Reading another module's underscore-prefixed helper is a soft norm violation; documented here so future readers see it's intentional.)

### 5.3 Concurrency

Same approach as `agent/state.py`:
- WAL mode on connection open
- Application-level retry with jitter on `OperationalError: database is locked`

---

## 6. Reward function

### 6.1 Protocol

```python
class RewardFunction(Protocol):
    def score(self, record: TrajectoryRecord) -> float:
        """Return reward in [0.0, 1.0]. Higher = better trajectory."""
```

### 6.2 Default: `RuleBasedRewardFunction`

Conservative MVP. Three signals, equal weight (configurable):

| Signal | Source | Weight |
|---|---|---|
| `tool_success_rate` | Fraction of `tool_call` events with `outcome == "success"` | 0.5 |
| `user_confirmed` | Whether session ended with positive user signal (heuristic: last user reply not "stop", "no", "wrong", "undo") | 0.3 |
| `task_completed` | `record.completion_flag` (set by agent loop on graceful end) | 0.2 |

**Edge cases:**
- Empty record (no events) → 0.0
- All events are `user_reply` (no tool calls) → score from `user_confirmed` + `task_completed` only, weighted 0.3 + 0.2 = 0.5 max
- Ongoing session (`ended_at is None`) → return None (caller filters; reward is undefined for in-flight)

**Anti-gaming notes (plan §risks edge case #5):**
- Doesn't reward verbosity (no length component)
- Doesn't reward speed (no latency component)
- LLM-judge reward is **explicitly v1.1+** with hardened guardrails

### 6.3 Future: LLM-judge reward (post-MVP)

Hermes uses multi-dimensional rubric scoring. We will replicate this **only after** the rule-based baseline shows clear value in dogfood, and only with explicit user opt-in (config flag), because LLM-judge:
- Costs more
- Introduces another model into the loop
- Risks reward-hacking via prompt-pattern matching

---

## 7. Reflection engine (B2 — stubbed in B1)

### 7.1 Public API (frozen at B1)

```python
class ReflectionEngine:
    def __init__(self, *, provider: BaseProvider, window: int = 30):
        ...

    def reflect(self, records: list[TrajectoryRecord]) -> list[Insight]:
        """Run a single reflection pass. B1: raises NotImplementedError."""
```

**Provider source:** `BaseProvider` from `plugin_sdk.provider_contract` — never `anthropic.Anthropic()` directly. Plan §refinements assumption #2.

### 7.2 Window = 30 default (plan §refinements assumption #4)

Default reduced from Hermes's larger batches because:
- 30 records × ~500 tokens/record = ~15K tokens — comfortable on Sonnet/Haiku
- Larger windows fit Opus but consume budget faster
- User can override via `--window`

### 7.3 Prompt template

`opencomputer/evolution/prompts/reflect.j2` (Jinja2 — project convention; B2 fills content). Will include:
- System message framing the reflection task (find patterns; propose actions)
- Each trajectory rendered compactly (tool sequence + outcomes)
- Output schema instruction (return JSON list of Insight)

---

## 8. Skill synthesis (B2 — stubbed in B1)

### 8.1 Output location

```
<evolution_home>/skills/<slug>/
├── SKILL.md            ← frontmatter + body (III.4 layout)
├── references/         ← optional supporting files
└── examples/           ← optional usage samples
```

`<slug>` derived from Insight's payload (sanitised). Never overwrites existing dirs — appends `-2`, `-3`, etc.

### 8.2 Atomic write (plan §worst-case WC5)

Synthesis writes to `<slug>.tmp/` first, then `os.replace(tmp, final)`. A reflection LLM call that fails mid-flight leaves no half-written skill on disk.

### 8.3 Promotion CLI (B2)

```
opencomputer evolution skills list
opencomputer evolution skills promote <slug>
opencomputer evolution skills retire <slug>
```

`promote` copies (not moves) the skill to the user's main skills dir (`_home() / "skills" / "<slug>"`) — original stays in evolution namespace as the audit trail. User can re-run `promote` after edits to refresh.

---

## 9. Bus subscription (B3 — not in B1)

**Prerequisite:** Session A's `opencomputer/ingestion/bus.py` exists. Per CLAUDE.md §5 ("Sub-project F parked") and `parallel-sessions.md` ("None yet — Session A has not yet shipped F2 TypedEvent bus"), B3 **waits**.

**B3 design (sketch — locked in at B3 start):**
- `trajectory.register_with_bus(bus)` subscribes to `agent_loop.*` and `tool_dispatch.*` event types
- Each subscriber callback is **idempotent** and **non-blocking** (queues to background writer thread; bus publisher never blocks on evolution writes)
- Subscribers use **public bus API only** — no reaching into publisher internals (plan §worst-case WC3)

---

## 10. Configuration

```python
@dataclass(frozen=True, slots=True)
class EvolutionConfig:
    enabled: bool = False                 # opt-in, hard default
    auto_collect: bool = False            # if True, register_with_bus() at startup (B3+)
    reflection_window: int = 30
    reward_weights: tuple[float, float, float] = (0.5, 0.3, 0.2)
```

**Where it lives:** A new top-level config dataclass. In B1 we ship the dataclass + tests; **wiring** it into the main `Config` requires touching `opencomputer/agent/config.py` which is **Session A reserved**. We propose the field via PR review (plan §refinements: "may need to touch `agent/config.py`; require coordination").

**B1 workaround:** `EvolutionConfig` instances are constructed locally inside evolution code paths (defaults from env vars). When Session A folds in the central wiring, evolution swaps to reading from the global config. No-op for users since `enabled=False` is the default either way.

---

## 11. CLI surface (final shape; B2 + B4 implement)

```
opencomputer evolution reflect [--window 30] [--dry-run]    # B2
opencomputer evolution skills list                          # B2
opencomputer evolution skills promote <slug>                # B2
opencomputer evolution skills retire <slug>                 # B4
opencomputer evolution trajectories show [--limit 50]       # B3
opencomputer evolution enable | disable                     # B3
opencomputer evolution prompts list                         # B4
opencomputer evolution prompts apply <id>                   # B4
opencomputer evolution prompts reject <id>                  # B4
opencomputer evolution dashboard                            # B4
opencomputer evolution reset                                # B2 (rollback path)
```

`opencomputer evolution reset` (plan §refinements; missing consideration #2) deletes evolution DB + skills + prompt proposals after `--yes` confirmation. Session DB (`sessions.db`) untouched.

---

## 12. Testing strategy (recap from plan + specifics)

| Layer | Coverage Target | B1 Scope |
|---|---|---|
| Unit | 90% of new evolution code | trajectory dataclasses, storage CRUD, reward scoring, migration runner |
| Integration | E2E: trajectory → reflection → skill file | Stubbed out in B1; full E2E in B2 |
| Migration round-trip | Apply to fresh DB; idempotent on up-to-date DB | Yes |
| CI baseline | `pytest tests/` ≥ Session A's current count | 809 baseline (per CLAUDE.md §4); B1 PR adds tests, never edits |

Test files (all new in B1):
- `tests/test_evolution_trajectory.py`
- `tests/test_evolution_storage.py`
- `tests/test_evolution_reward.py`
- `tests/test_evolution_reflect.py` (mostly stub-behavior tests in B1)
- `tests/test_evolution_synthesize.py` (mostly stub-behavior tests in B1)

---

## 13. Self-audit (design-doc level)

### 13.1 Did we cover the plan's flagged assumptions?

| Plan assumption | Handled where |
|---|---|
| F1 not ready | §5.1 — self-contained migration runner with documented refactor path |
| Anthropic SDK direct call | §7.1 — uses `BaseProvider`, not Anthropic SDK directly |
| `~/.opencomputer/` hardcoded | §5.2 — imports `_home()`; never hardcodes |
| Reflection on 100 trajectories | §7.2 — default 30 |
| Evolution invisible to Session A | §11 — `[generated]` tag in CLI listings; B2 README explains |

### 13.2 New risks introduced by THIS design

1. **Importing `_home()` from `agent/config.py`.** The leading underscore signals "private to module". If Session A renames it (without a deprecation alias), evolution breaks. Mitigation: write a small adapter test that asserts `_home()` exists + returns a `Path`; if Session A renames it we fail loudly and adjust.
2. **Schema-version-per-record.** Cleaner than DB-wide alone but doubles mental load. Mitigation: docstring on `TrajectoryRecord.schema_version` explains exactly when to bump.
3. **Migration runner duplicates F1.** When F1 lands we delete `migrations/` runner and route to F1. Until then, we maintain two patterns. Mitigation: a `# TODO(F1)` comment at the top of `storage.py` and a one-line note in `parallel-sessions.md`.

### 13.3 What we explicitly chose NOT to do in B1

- ❌ Wire `EvolutionConfig` into central `Config` (Session A reserved file — coordinate via PR review)
- ❌ Implement reflection / synthesis logic (B2)
- ❌ Subscribe to bus (B3 — bus doesn't exist yet)
- ❌ Build dashboard (B4)
- ❌ Modify `agent/loop.py` to emit trajectory events (publisher is Session A's; we subscribe in B3)
- ❌ Vendor Hermes code (license is MIT but architectural fit is poor — borrow concepts only)

### 13.4 Quantified uncertainty (this design)

| Claim | Confidence |
|---|---|
| `_home()` import is stable for B1 timeframe | 90% — Session A's most recent commits don't touch it |
| Migration runner spec is sufficient | 85% — known shape from `agent/state.py`; risk is corner cases |
| Privacy rule (no raw prompts in metadata) is enforceable | 80% — depends on subscriber discipline in B3 |
| Reward function is gameable | 30% — narrow signals limit damage but creative agents could find loopholes |

---

## 14. Open questions (resolve at B2 / B3)

- **Q1.** Should reflection cache its LLM responses to avoid re-cost on dry-runs? (B2 — likely yes, key off trajectory id range hash.)
- **Q2.** When Session A's TypedEvent bus ships, does it expose enough event metadata for evolution? (B3 — review at that time.)
- **Q3.** Should `EvolutionConfig` live in `agent/config.py` (central) or `evolution/config.py` (subpackage)? Slight preference for central once Session A is amenable (consistency). (B2/B3 — coordinate.)

---

## 15. Status

- **B1 design**: Locked. Implementation tasks 3–6 in the controller's TodoWrite map 1:1 to §3 module map.
- **B2 design**: Sketched (§7, §8, §11). Will be revisited at start of B2 with any learnings from B1 dogfood.
- **B3 design**: Sketched (§9). Locked at B3 start once bus exists.
- **B4 design**: Sketched (§11). Locked at B4 start.

This document is updated when design decisions change. The companion `source-map.md` is fixed — it's a snapshot of the Hermes source as scanned on 2026-04-24.
