# CC §4 + §10 Visibility — `/context` slash + `oc usage` / `oc context` CLIs + compaction counter

**Status:** Design accepted, ready for implementation.
**Date:** 2026-05-10
**Source spec:** `docs/OC-FROM-CLAUDE-CODE.md` items §4 (`/context`), §10 (`/usage`).
**Branch:** `worktree-cc-from-claude-code-2026-05-10` (off `origin/main` @ `d7179373`).
**Parallel-session avoidance:** Skips OpenClaw items #3 (`cli_secrets.py`, `security/secrets.py`), #4 (`test_skill_requires_gating.py`), parity_doctor — all owned by the OPEN-CLAW-CHANGES session.

## 1. Problem

OC has cache and token telemetry recorded in `SessionDB.sessions.{input,output,cache_read,cache_write}_tokens` and `llm_calls` table since v13 (Hermes B4). The slash `/usage` command renders some of it. But the user has no surface for:

- **Context window % used / remaining** — "am I about to compact?" Answer requires `current_input_tokens / context_window_for(model)`.
- **Compaction count this session** — surfaced nowhere. Users can't tell if their long session has rotated context twice or twenty times.
- **Cross-session aggregation** — `oc cost show` is per-provider per-day; there's no `oc usage` listing per-session totals or filtering by `--since`, `--by-model`, `--session-id`.
- **CLI parity for `/usage`** — `/usage` is in-chat only; `oc usage` doesn't exist.

This blocks measurement of CC §1 (Prompt Caching audit). You can't "expose cache hit stats so Saksham can see actual savings" without a stable surface to read them on. Fix the surface first, audit caching second.

## 2. Out of scope (v1)

Deferred to v1.1+ for clear reasons:

| Drop | Why | Re-open when |
|---|---|---|
| Per-component breakdown (system/tools/messages tokens) | Not derivable from API response; requires tokenizer at prompt-construction. Big lift across providers. | A tokenizer-based counter exists for one provider |
| Effort metrics | Anthropic + OpenAI APIs don't expose | Provider returns it |
| Time-series of context % | YAGNI; static snapshot wins | User asks for it |
| Per-tool-cost rollups | `tool_usage` and `llm_calls` tables exist; aggregation is mechanical follow-up | After v1 ships |

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       slash dispatch                             │
│                                                                  │
│   /usage  ──► UsageCommand     (existing — augment)              │
│              reads runtime.custom["session_*"] keys              │
│                                                                  │
│   /context ─► ContextCommand   (NEW — slash_commands_impl/)      │
│              reads runtime.custom + compaction.context_window_* │
└─────────────────────────────────────────────────────────────────┘
                          ▲                       ▲
                          │ runtime.custom        │ DB
                          │                       │
┌─────────────────────────┴───────────────────────┴───────────────┐
│                          AgentLoop                               │
│                                                                  │
│   • populates runtime.custom["session_tokens_in/out/...]         │
│     after each provider call (existing, line ~2132)              │
│                                                                  │
│   • after compaction succeeds (cresult.did_compact):             │
│     ───► self._db.increment_compaction_count(session_id)         │
│     ───► runtime.custom["session_compactions"] = count           │
└─────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────┐
│                          SessionDB                               │
│                                                                  │
│   sessions.compactions_count INTEGER DEFAULT 0    (v17→v18)      │
│                                                                  │
│   def increment_compaction_count(session_id)                     │
│   def session_usage_summary(session_id) -> SessionUsageRow       │
│   def usage_summary_aggregate(since, model, provider) -> Rows    │
└─────────────────────────────────────────────────────────────────┘
                                  ▲
                                  │
┌─────────────────────────────────┴───────────────────────────────┐
│                       Typer CLI apps                             │
│                                                                  │
│   oc usage [show | summary]    cli_usage.py    (NEW)             │
│   oc context [show]            cli_context.py  (NEW)             │
└─────────────────────────────────────────────────────────────────┘
```

## 4. Components

### 4.1 SessionDB schema v17 → v18

```sql
ALTER TABLE sessions ADD COLUMN compactions_count INTEGER DEFAULT 0;
```

Migration body:
```python
def _migrate_v17_to_v18(conn: sqlite3.Connection) -> None:
    """v18 (2026-05-10) — per-session compaction counter for /context.

    Additive nullable column; legacy rows read 0. Bumped by
    SessionDB.increment_compaction_count() called from AgentLoop
    after CompactionResult.did_compact == True.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "compactions_count" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN compactions_count INTEGER DEFAULT 0"
        )
```

### 4.2 SessionDB helpers

```python
def increment_compaction_count(self, session_id: str) -> int:
    """Bump compactions_count by 1 atomically; return new value."""

@dataclass(frozen=True)
class SessionUsageRow:
    session_id: str
    model: str | None
    started_at: float
    ended_at: float | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    compactions_count: int
    cost_usd: float | None  # joined from llm_calls SUM

def session_usage_summary(self, session_id: str) -> SessionUsageRow | None:
    """Return single-session totals for /context or `oc context show`."""

def usage_summary_aggregate(
    self,
    since: float | None = None,
    model: str | None = None,
    provider: str | None = None,
    limit: int = 50,
) -> list[SessionUsageRow]:
    """Return per-session rows for `oc usage show`. Joins llm_calls
    on session_id for cost_usd. Filterable by since-epoch / model / provider."""
```

### 4.3 Loop integration (1 line — DEPENDENCY MINIMIZATION)

In `loop.py` near line 1888 / 1980 where `cresult.did_compact` is handled:

```python
if cresult.did_compact:
    new_count = self._db.increment_compaction_count(self._session_id)
    self._runtime.custom["session_compactions"] = new_count
```

This is the entire AgentLoop diff. Hunk size ~3 lines added at one site.

### 4.4 `/context` slash command — `slash_commands_impl/context_cmd.py`

```python
class ContextCommand(SlashCommand):
    name = "context"
    description = "Show context window usage + compaction count"

    async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
        # Read state
        in_t = int(runtime.custom.get("session_tokens_in") or 0)
        compactions = int(runtime.custom.get("session_compactions") or 0)
        last_input = int(runtime.custom.get("last_input_tokens") or 0)  # current turn
        model = runtime.custom.get("model") or "(unknown)"

        # Compute max
        from opencomputer.agent.compaction import context_window_with_overrides
        try:
            max_ctx = context_window_with_overrides(model)
        except Exception:
            max_ctx = 200_000  # safe default

        used = last_input or in_t  # prefer current-turn signal
        pct = (used / max_ctx * 100) if max_ctx else 0.0
        threshold = 0.98  # OC default compaction trigger

        lines = ["## Context window"]
        lines.append(f"  model:      {model}")
        lines.append(f"  used:       {used:,} / {max_ctx:,} ({pct:.1f}%)")
        lines.append(f"  remaining:  {max_ctx - used:,} tokens")
        lines.append(f"  compaction: triggers at {threshold*100:.0f}%")
        lines.append(f"  compactions this session: {compactions}")
        lines.append(f"  total session input tokens: {in_t:,}")

        return SlashCommandResult(output="\n".join(lines), handled=True)
```

### 4.5 `/usage` slash augmentation

Add 2 lines after the existing cache row:
```python
compactions = runtime.custom.get("session_compactions")
if isinstance(compactions, int) and compactions > 0:
    lines.append(f"  compactions:   {compactions}")
```

### 4.6 `oc usage` CLI (`cli_usage.py`)

**Implementation note (post-discovery):** `cli_usage.py` was already
shipped in PR #420 Wave 5 T5 with a JSONL-backed top-level callback
(`oc usage`) plus `oc usage --cache-stats`. Rather than duplicate that
surface, this work **adds a single subcommand** that fills the genuine
gap (per-session SessionDB view with compaction count):

```
opencomputer usage sessions [--session-id ID]
                            [--model M]
                            [--provider P]
                            [--since ISO]
                            [--limit N]
```

`sessions`: per-session table from `usage_summary_aggregate()` — input
/ output / cache R-W / compactions / cost columns. Most-recent first.
Limit clamped to `[1, 1000]`. Empty result renders the cli_ui
empty-state with discovery hints.

The existing top-level `oc usage` callback (JSONL telemetry rollups by
provider × model × site) is **unchanged** — both views co-exist. The
JSONL view answers "what did the last 24h cost?"; the SessionDB
subcommand answers "show me each session, with compaction count".

The originally-designed `show` and `summary --by-model / --by-provider`
subcommand shapes were dropped during discovery because the existing
`oc usage` callback already covers cross-provider rollups. Re-open
only if users ask for SessionDB-backed per-model aggregation.

### 4.7 `oc context` CLI (`cli_context.py`)

```
opencomputer context show <session-id>       # render panel for one session
opencomputer context show --current          # render for most-recent session
opencomputer context list [--limit N]        # overview table: every session's % + compactions
```

`show` mirrors `/context` slash output for arbitrary historical
sessions. Reads via `session_usage_summary()` only — does not need
`runtime.custom` (those are in-flight only).

`list` is the discovery surface — without it, you'd need to know a
session id to call `show`. Renders a Rich Table with `Session / Model /
Used/Max / % / Compactions` columns sorted most-recent first.

**Why two commands** (not just `show`): Typer auto-promotes Typer apps
with a single command, collapsing `oc context show <id>` to `oc
context <id>` — which then mis-parses. Registering `list` as a second
command suppresses the auto-promote. Beyond that workaround, `list`
is genuinely useful as the discovery surface for `show`.

### 4.8 cli.py wiring (2 lines)

```python
from opencomputer.cli_usage import usage_app
from opencomputer.cli_context import context_app
app.add_typer(usage_app, name="usage", help="Token + cost usage reports.")
app.add_typer(context_app, name="context", help="Context-window inspection per session.")
```

## 5. Data flow per turn

1. User asks anything → loop iterates → provider returns response with usage block.
2. Loop reads `usage.input_tokens`, `usage.output_tokens`, `usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens` (already done at line ~2112-2163).
3. Loop sets `runtime.custom["session_tokens_in/out/cache_read/cache_write"]`.
4. **NEW:** if compaction ran, loop calls `db.increment_compaction_count()` and writes `runtime.custom["session_compactions"]`.
5. User types `/context` → ContextCommand reads runtime.custom + computes %.
6. User types `oc context show <id>` later → CLI reads `sessions.compactions_count` from DB.

## 6. Test plan

| Test file | Count | Coverage |
|---|---|---|
| `test_compaction_counter.py` | 21 (19 + 2 SQLite-version skips) | Migration v17→v18 idempotent + legacy preserved; `increment_compaction_count` atomic + adversarial; `session_usage_summary` joins `llm_calls.cost_usd` (NULL → None); `usage_summary_aggregate` filters since/model/provider + clamps limit. |
| `test_loop_compaction_increments_counter.py` | 7 | `_record_compaction` bumps counter, writes runtime.custom, handles unknown / empty session, survives DB exception, source-grep test locks both `did_compact` call sites. |
| `test_context_cmd.py` | 13 | `/context` renders all fields; coerces adversarial token values; falls back on missing model; divide-by-zero safe; over-cap doesn't crash. |
| `test_usage_command_compactions_line.py` | 4 | `/usage` renders `compactions` row only when > 0; handles non-int adversarial value. |
| `test_cli_usage_sessions.py` | 13 | `oc usage sessions` table + filters by session-id/model/provider; clamps limit; renders empty state; existing `oc usage` callback still works. |
| `test_cli_context.py` | 11 | `oc context show <id>` + `--current` + `list`; empty state; missing DB; unknown model fallback. |

**Total: 69 new test cases**, plus the slashes and 31 supporting/edge
tests in adjacent files. ruff clean on all touched + new files.

Goal: ≥80% coverage on new modules — verified via inspection
(no untested branches in `context_cmd.py`, `cli_context.py`, the new
`SessionDB` helpers, or `_record_compaction`).

## 7. Migration / rollout

1. Bump `SCHEMA_VERSION` to 18.
2. Migration runs on first DB open — additive ALTER, no rollback path needed.
3. Legacy sessions read `compactions_count = 0` (default). Acceptable — they had compactions, but didn't track them. Cosmetic only.
4. No config changes. No env var. Pure additive.

## 8. Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Parallel session collides on `loop.py` | Med | Med | Single-line touch at known site (line ~1980, post-compaction). Their work is in steer/cross-loop area. Resolve in PR. |
| Migration v18 fails on partially-migrated DBs | Low | Low | Idempotent `cols` check; pattern proven in v14, v17 |
| `context_window_with_overrides()` raises | Low | Low | Try/except with default 200k |
| Missing `runtime.custom` keys | Low | Low | All reads use `.get(key) or 0` pattern |

## 9. Acceptance criteria

- `pytest tests/test_compaction_counter.py tests/test_loop_compaction_increments_counter.py tests/test_context_cmd.py tests/test_usage_command_compactions_line.py tests/test_cli_usage_sessions.py tests/test_cli_context.py` — all passing.
- Targeted regression: `pytest -k "compact or session_db or usage or context_cmd or migration or context_engine"` (~285 cases) green.
- Loop / slash / compaction battery (~223 cases) green.
- Full CLI suite (~699 cases) green.
- Full suite: `pytest tests/` — see §10 below for the actual result and any pre-existing env-level errors.
- Lint: `ruff check OpenComputer/opencomputer OpenComputer/tests` — clean on all touched/new files.
- Manual: `oc context --help` and `oc usage sessions --help` render. (In-chat `/context` is unit-tested via `ContextCommand.execute()` rather than driving a real chat session — the slash-dispatch contract is exercised by 13 tests.)

## 10. References

- `docs/OC-FROM-CLAUDE-CODE.md` §4 (`/context`), §10 (`/usage`).
- Existing: `opencomputer/agent/slash_commands_impl/usage_cmd.py`.
- Existing: `opencomputer/cli_cost.py` (Typer + Rich pattern reference).
- SessionDB: `opencomputer/agent/state.py:43-79` (sessions DDL), `:773-815` (llm_calls), `:1720-1747` (record_usage).
- Loop wiring: `opencomputer/agent/loop.py:2112-2163` (runtime.custom population).
- Compaction: `opencomputer/agent/compaction.py:178` (CompactionResult.did_compact), `:241` (context_window_for).
