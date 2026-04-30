# Profile-Suggest — Layered Discoverability Design

**Date:** 2026-04-30
**Status:** Approved (verbal), proceeding to plan.
**Author:** Claude (with Saksham's iterative guidance)

## Problem

OpenComputer supports per-profile data isolation (separate `sessions.db`, `MEMORY.md`, `USER.md` etc. under `~/.opencomputer/profiles/<name>/`). Profile switching is intentionally manual (`oc -p <name>`) because auto-switching would orphan in-flight sessions. But this creates a **discoverability gap**:

1. Users in `default` may not realize a specialized profile would help (e.g., 18 of 30 sessions are `coding` mode but no `work` profile exists).
2. Users with multiple profiles may forget which one applies to their current task.
3. New users don't know `/profile-suggest` exists.

Per-message agent-push (Learning Moments alone) is too noisy for a feature that fires ~10 times in a user's lifetime.

## Approach: User-pull slash command + 4 layered discovery surfaces

**Primary feature:** `/profile-suggest` — pure on-demand analysis. Reads existing data, no background overhead.

**Discovery layers** (so users actually find the command):
1. **Always-listed in `/help`** (free — comes with SlashCommand registration)
2. **Empty-state teaching** in `oc profile list` and `oc profile create` (extends existing v2 Mechanism D)
3. **Behavioral Learning Moment** that fires once when user shows multi-context usage on `default` profile
4. **`oc doctor` check** — surfaces "you have multi-context usage but no specialized profile" as a doctor warning

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    Existing infrastructure                       │
│  • Persona classifier (PR #271) — runs every turn, sets         │
│    active_persona_id. We log this per-turn.                      │
│  • SessionDB.list_sessions() — returns recent sessions          │
│  • profiles.py / get_profile_dir() — profile resolution         │
│  • cli_ui/empty_state.py — Mechanism D teaching surface         │
│  • doctor.py — health check framework                           │
└──────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────┐
│           opencomputer/profile_analysis.py (NEW)                 │
│  • compute_profile_suggestions(home, db) -> ProfileReport        │
│    Reads recent sessions + persona log + available profiles,    │
│    returns: (current, persona_breakdown, suggestions)            │
│  • Pure function — no I/O side effects, no LLM calls.            │
│  • Reused by all 4 surfaces below.                               │
└──────────────────────────────────────────────────────────────────┘
                                │
            ┌───────────────────┼───────────────────┐
            ▼                   ▼                   ▼
   ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
   │ /profile-      │  │ Learning       │  │ oc doctor      │
   │ suggest        │  │ Moment v3.1    │  │ check          │
   │ (new slash)    │  │ (one new entry │  │ (new fn in     │
   │                │  │ in registry)   │  │ doctor.py)     │
   └────────────────┘  └────────────────┘  └────────────────┘
                                │
                                ▼
                       ┌────────────────┐
                       │ Empty-state    │
                       │ teaching       │
                       │ (extends 2     │
                       │ existing paths)│
                       └────────────────┘
```

## Data Sources

The analysis function reads **only existing data** — no new persistence:

1. **Persona log per session.** Already persisted: `sessions.set_session_vibe` exists; we extend `sessions` table (or use `vibe_log` table) to store `active_persona_id` at session-end. *Or* we walk recent sessions and re-classify their first user message synchronously (slower but no schema change).
2. **Available profiles.** `Path("~/.opencomputer/profiles/").iterdir()`.
3. **Current profile.** From `OPENCOMPUTER_HOME` env or default.
4. **Recent session count per profile.** `SessionDB.list_sessions(limit=30)`.

**Decision:** Use the simpler "re-classify recent sessions" approach. Avoids schema migration. Cost: O(N) classifier calls per `/profile-suggest` invocation, where N=30. Classifier is regex-based + cheap (PR #271 reports <5ms per call). Total <150ms per invocation.

## Component Design

### `compute_profile_suggestions(home, db) -> ProfileReport`

```python
@dataclass(frozen=True, slots=True)
class PersonaSessionCount:
    persona_id: str
    count: int

@dataclass(frozen=True, slots=True)
class ProfileSuggestion:
    kind: Literal["create", "switch", "stay"]
    profile_name: str | None  # None for "stay"
    persona: str
    rationale: str            # human-readable reason
    command: str              # the actual command to run

@dataclass(frozen=True, slots=True)
class ProfileReport:
    current_profile: str
    available_profiles: tuple[str, ...]
    persona_breakdown: tuple[PersonaSessionCount, ...]
    suggestions: tuple[ProfileSuggestion, ...]
    sessions_analyzed: int
```

### Suggestion logic

For each persona that appeared in ≥3 of last 30 sessions:
- If a profile name fuzzy-matches the persona (substring or shared 3+ char prefix): suggest **switch** if not active, suggest **stay** if active.
- If no profile matches: suggest **create**.

### Persona ↔ profile fuzzy match

```python
PERSONA_PROFILE_MAP = {
    "trading": ("stock", "trade", "trading", "finance", "market", "invest"),
    "coding":  ("work", "code", "dev", "project", "engineering"),
    "companion": ("personal", "life", "journal", "diary"),
    "relaxed": ("chill", "casual", "leisure"),
}

def _persona_matches_profile(persona: str, profile_name: str) -> bool:
    profile_lower = profile_name.lower()
    candidates = PERSONA_PROFILE_MAP.get(persona, ())
    for c in candidates:
        if c in profile_lower or profile_lower in c:
            return True
    return False
```

If neither side maps cleanly, no suggestion.

## Surface 1: `/profile-suggest` slash command

```
$ /profile-suggest
─────────────────────────────────────────────────────
Active profile: default

Recent persona breakdown (last 30 sessions):
  coding:    18 sessions  (60%)
  trading:    7 sessions  (23%)
  companion:  3 sessions  (10%)
  default:    2 sessions  ( 7%)

Suggestions:
  ✦ Create profile 'work' (or similar) — 18 of last 30 sessions
    were coding-mode and no specialized profile matches.
       oc profile create work && oc -p work

  ✦ Try `oc -p stock` for trading sessions — your stock profile
    exists but is rarely used (you're in default for these).
       oc -p stock

  ✓ Companion sessions (3) — too few for a separate profile yet.
─────────────────────────────────────────────────────
```

**Output rendering:** plain text via Rich console. No interactivity (no pickers).

## Surface 2: Empty-state teaching

Two paths in `cli_ui/empty_state.py`:

1. After `oc profile list` when only `default` exists:
   ```
   Profiles:
     * default   <-- active

   Only one profile. Tip: `/profile-suggest` analyzes your usage
   and recommends when a specialized profile would help.
   ```

2. After `oc profile create <name>` succeeds:
   ```
   Created profile '<name>'.
   Tip: `/profile-suggest` shows when this profile makes sense to
   switch into based on your conversation pattern.
   ```

## Surface 3: Learning Moment v3.1

```python
LearningMoment(
    id="suggest_profile_suggest_command",
    predicate=suggest_profile_suggest_command,
    reveal=(
        "(You've been switching contexts a lot this session — "
        "`/profile-suggest` analyzes your usage and tells you if a "
        "specialized profile would help.)"
    ),
    surface=Surface.INLINE_TAIL,
    priority=200,
)
```

**Predicate:**
```python
def suggest_profile_suggest_command(ctx: Context) -> bool:
    return (
        ctx.persona_flips_in_session >= 3
        and ctx.current_profile_name == "default"
    )
```

**New Context field:** `persona_flips_in_session: int = 0` — incremented in `_maybe_reclassify_persona` each time the persona changes.

## Surface 4: `oc doctor` check

```python
def check_profile_usage(home: Path, db: SessionDB) -> HealthContribution:
    report = compute_profile_suggestions(home, db)
    create_suggestions = [s for s in report.suggestions if s.kind == "create"]
    if not create_suggestions:
        return HealthContribution(status=HealthStatus.OK, ...)

    # Multi-context usage detected, no matching profile
    msg = (
        f"Profile usage: {create_suggestions[0].rationale}. "
        "Run /profile-suggest for details."
    )
    return HealthContribution(status=HealthStatus.WARN, message=msg, ...)
```

Surfaces in `oc doctor` output as a yellow warning.

## Error Handling

All paths degrade gracefully:
- Missing `sessions.db` → empty `ProfileReport`, slash command says "no session history yet — use OC for a few sessions and try again"
- Persona classifier fails → skip that session, count as `default`
- Profile dir scan fails → `available_profiles=()`, suggestions limited to "create" only

## Testing Strategy

- **Unit tests** for `compute_profile_suggestions` with synthetic SessionDB (in-memory SQLite) — fixture data covers: only-default profile, multiple profiles, mismatch, empty history.
- **Unit tests** for `_persona_matches_profile` — covers each PERSONA_PROFILE_MAP case + non-matches.
- **CLI integration test** for `/profile-suggest` invocation — assert output table contains expected rows.
- **Empty-state test** for `oc profile list` and `oc profile create` — verify tips appear.
- **Learning Moment test** for `suggest_profile_suggest_command` predicate — covers ≥3 flips fires + <3 doesn't + non-default profile doesn't.
- **Doctor test** — covers WARN when create suggestion exists, OK when not.

Total ~25 new tests.

## Out of Scope (for this PR)

- Auto-creating profiles based on persona patterns (presumptuous; one-shot suggestion is enough)
- Persisting persona-flip count across sessions (only intra-session counts the flips for this LM)
- Per-profile keyword maps (Variant B from earlier brainstorm — defer until A1 gaps surface)
- Topic-similarity scoring (Variant C — over-engineered for current scale)

## Migration / Backwards Compat

- No schema changes (uses existing `sessions` table + persona classifier output)
- New Context field `persona_flips_in_session: int = 0` — defaulted, BC-safe
- New Context field `current_profile_name: str = "default"` — defaulted, BC-safe
- New Context field `available_profiles: tuple[str, ...] = ()` — defaulted, BC-safe
- New module `opencomputer/profile_analysis.py` — additive
- New slash command `/profile-suggest` — additive
- New doctor check — additive

Total LOC estimate: ~350 (90 analysis + 80 slash + 50 LM + 30 doctor + 20 empty-state + 80 tests).
