# Profile-Suggest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `/profile-suggest` slash command + 4 discoverability layers (help-listing, empty-state teaching, Learning Moment, doctor check) so users discover when a specialized profile would help.

**Architecture:** New `profile_analysis.py` module with pure-function `compute_profile_suggestions()` reused by all 4 surfaces. No schema migration. Persona classifier output is re-derived from existing session data on demand.

**Tech Stack:** Python 3.12+ stdlib + existing OC modules (no new deps).

---

## File Structure

| File | Action |
|------|--------|
| `opencomputer/profile_analysis.py` | Create — analysis function + dataclasses |
| `opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py` | Create — `/profile-suggest` slash command |
| `opencomputer/agent/slash_commands.py` | Modify — register new command |
| `opencomputer/cli_ui/empty_state.py` | Modify — add 2 teaching paths |
| `opencomputer/cli_profile.py` | Modify — wire empty-state teaching into `list` and `create` |
| `opencomputer/awareness/learning_moments/registry.py` | Modify — add 3 Context fields + 1 LearningMoment |
| `opencomputer/awareness/learning_moments/predicates.py` | Modify — add 1 predicate |
| `opencomputer/agent/loop.py` | Modify — populate 3 new Context fields + count persona flips |
| `opencomputer/doctor.py` | Modify — add `check_profile_usage` health check |
| `tests/test_profile_analysis.py` | Create — analysis function tests |
| `tests/test_profile_suggest_cli.py` | Create — slash command integration test |
| `tests/test_profile_empty_state.py` | Create — empty-state teaching tests |
| `tests/test_learning_moments.py` | Modify — add v3.1 predicate test + LM integration test |
| `tests/test_doctor_profile_usage.py` | Create — doctor check test |

---

## Task 1: Build the analysis core (pure-function module)

**Files:**
- Create: `opencomputer/profile_analysis.py`
- Create: `tests/test_profile_analysis.py`

- [ ] **Step 1: Write failing tests first**

```python
# tests/test_profile_analysis.py
from __future__ import annotations

from pathlib import Path
import pytest

from opencomputer.profile_analysis import (
    compute_profile_suggestions,
    PersonaSessionCount,
    ProfileSuggestion,
    ProfileReport,
    _persona_matches_profile,
)
from opencomputer.agent.state import SessionDB


def _make_db_with_sessions(tmp_path: Path, persona_counts: dict[str, int]) -> SessionDB:
    """Build a SessionDB with N sessions per persona via insert_session + set_session_vibe."""
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)
    for persona, count in persona_counts.items():
        for i in range(count):
            sid = f"{persona}-{i}"
            db.insert_session(sid, persona=persona)  # see Task 1.5 for this method
    return db


def test_persona_matches_profile_substring():
    assert _persona_matches_profile("trading", "stock") is True
    assert _persona_matches_profile("trading", "trade") is True
    assert _persona_matches_profile("coding", "work") is True
    assert _persona_matches_profile("coding", "dev") is True


def test_persona_matches_profile_negative():
    assert _persona_matches_profile("trading", "personal") is False
    assert _persona_matches_profile("companion", "code") is False


def test_compute_returns_create_suggestion_for_unmatched_dominant_persona(tmp_path):
    db = _make_db_with_sessions(tmp_path, {"coding": 18, "default": 12})
    report = compute_profile_suggestions(
        home=tmp_path, db=db, current_profile="default", available_profiles=("default",),
    )
    assert any(s.kind == "create" for s in report.suggestions)
    create = next(s for s in report.suggestions if s.kind == "create")
    assert create.persona == "coding"


def test_compute_returns_switch_suggestion_when_matching_profile_exists(tmp_path):
    db = _make_db_with_sessions(tmp_path, {"trading": 7, "default": 23})
    report = compute_profile_suggestions(
        home=tmp_path, db=db, current_profile="default",
        available_profiles=("default", "stock"),
    )
    assert any(s.kind == "switch" and s.profile_name == "stock" for s in report.suggestions)


def test_compute_skips_minor_personas(tmp_path):
    db = _make_db_with_sessions(tmp_path, {"companion": 2, "default": 28})
    report = compute_profile_suggestions(
        home=tmp_path, db=db, current_profile="default", available_profiles=("default",),
    )
    # 2 sessions < threshold of 3 — no suggestion
    assert all(s.persona != "companion" for s in report.suggestions)


def test_compute_handles_empty_history(tmp_path):
    db_path = tmp_path / "sessions.db"
    db = SessionDB(db_path)  # no sessions inserted
    report = compute_profile_suggestions(
        home=tmp_path, db=db, current_profile="default", available_profiles=("default",),
    )
    assert report.sessions_analyzed == 0
    assert report.suggestions == ()
```

- [ ] **Step 2: Run tests — confirm they fail with ImportError**

```bash
cd /tmp/oc-profile-suggest/OpenComputer && pytest tests/test_profile_analysis.py -x -q 2>&1 | tail -5
```
Expected: ImportError or "module not found" — module doesn't exist yet.

- [ ] **Step 3: Implement `profile_analysis.py`**

```python
# opencomputer/profile_analysis.py
"""Profile usage analysis — single source of truth for profile suggestions.

Pure-function module reused by:
1. ``/profile-suggest`` slash command
2. ``oc doctor`` profile-usage check
3. Future surfaces (empty-state hints, etc.)

No I/O side effects. Reads existing data: SessionDB rows + filesystem
profiles dir. The persona classifier is invoked on demand for sessions
that don't have a recorded persona — keeps schema migration off the
critical path.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opencomputer.agent.state import SessionDB

# Persona name → list of profile-name hints. If a persona's id appears
# alongside (or is) a profile name in any of these lists, we treat them
# as a match for the suggest-switch / suggest-stay logic.
PERSONA_PROFILE_MAP: dict[str, tuple[str, ...]] = {
    "trading":   ("stock", "trade", "trading", "finance", "market", "invest"),
    "coding":    ("work", "code", "dev", "project", "engineering"),
    "companion": ("personal", "life", "journal", "diary"),
    "relaxed":   ("chill", "casual", "leisure"),
}

# Minimum sessions of a persona before it qualifies for a suggestion.
# 3 keeps the floor low enough to fire on real signal, high enough to
# skip drive-by classifications.
_MIN_SESSIONS_FOR_SUGGESTION = 3

# How many recent sessions to analyze.
_LOOKBACK_SESSIONS = 30


@dataclass(frozen=True, slots=True)
class PersonaSessionCount:
    persona_id: str
    count: int


@dataclass(frozen=True, slots=True)
class ProfileSuggestion:
    kind: Literal["create", "switch", "stay"]
    profile_name: str | None
    persona: str
    rationale: str
    command: str


@dataclass(frozen=True, slots=True)
class ProfileReport:
    current_profile: str
    available_profiles: tuple[str, ...]
    persona_breakdown: tuple[PersonaSessionCount, ...]
    suggestions: tuple[ProfileSuggestion, ...]
    sessions_analyzed: int


def _persona_matches_profile(persona: str, profile_name: str) -> bool:
    """Fuzzy match: profile name shares ≥3 chars with persona-hint list."""
    profile_lower = profile_name.lower()
    candidates = PERSONA_PROFILE_MAP.get(persona, ())
    for c in candidates:
        if c in profile_lower or profile_lower in c:
            return True
    return False


def compute_profile_suggestions(
    *,
    home: Path,
    db: SessionDB,
    current_profile: str,
    available_profiles: tuple[str, ...],
) -> ProfileReport:
    """Analyze recent sessions + available profiles, return ProfileReport."""
    rows = db.list_sessions(limit=_LOOKBACK_SESSIONS)
    persona_counter: Counter[str] = Counter()
    for row in rows:
        persona = (row.get("persona") or row.get("active_persona_id") or "default")
        persona_counter[persona] += 1

    breakdown = tuple(
        PersonaSessionCount(persona_id=p, count=c)
        for p, c in persona_counter.most_common()
    )

    suggestions: list[ProfileSuggestion] = []
    for persona, count in persona_counter.most_common():
        if persona == "default":
            continue
        if count < _MIN_SESSIONS_FOR_SUGGESTION:
            continue
        # Find a matching profile, if any.
        matching = next(
            (p for p in available_profiles if _persona_matches_profile(persona, p)),
            None,
        )
        if matching is None:
            suggestions.append(ProfileSuggestion(
                kind="create",
                profile_name=None,
                persona=persona,
                rationale=(
                    f"{count} of last {len(rows)} sessions were {persona}-mode "
                    "and no specialized profile matches"
                ),
                command=f"oc profile create <name> && oc -p <name>",
            ))
        elif matching != current_profile:
            suggestions.append(ProfileSuggestion(
                kind="switch",
                profile_name=matching,
                persona=persona,
                rationale=(
                    f"{count} {persona}-mode sessions but you're in '{current_profile}' "
                    f"— '{matching}' profile exists"
                ),
                command=f"oc -p {matching}",
            ))
        else:
            suggestions.append(ProfileSuggestion(
                kind="stay",
                profile_name=matching,
                persona=persona,
                rationale=(
                    f"{count} {persona}-mode sessions, you're already in '{matching}'"
                ),
                command="",
            ))

    return ProfileReport(
        current_profile=current_profile,
        available_profiles=available_profiles,
        persona_breakdown=breakdown,
        suggestions=tuple(suggestions),
        sessions_analyzed=len(rows),
    )


__all__ = [
    "compute_profile_suggestions",
    "PersonaSessionCount",
    "ProfileSuggestion",
    "ProfileReport",
    "PERSONA_PROFILE_MAP",
    "_persona_matches_profile",
]
```

- [ ] **Step 4: Run tests — confirm pass**

```bash
cd /tmp/oc-profile-suggest/OpenComputer && pytest tests/test_profile_analysis.py -x -q 2>&1 | tail -5
```
Expected: 5 passed (or refer to actual SessionDB API for fixture).

- [ ] **Step 5: Commit**

```bash
git add opencomputer/profile_analysis.py tests/test_profile_analysis.py
git commit -m "feat(profile-analysis): pure-function profile suggestion analyzer"
```

---

## Task 1.5: Verify SessionDB supports persona-bearing sessions

**Files:**
- Read: `opencomputer/agent/state.py`

- [ ] **Step 1: Check whether `sessions` table has `persona` or `active_persona_id` column**

```bash
cd /tmp/oc-profile-suggest/OpenComputer && grep -n "persona\|CREATE TABLE sessions\|insert_session" opencomputer/agent/state.py | head -20
```

- [ ] **Step 2a: If persona column exists, use it directly.**

- [ ] **Step 2b: If NOT, derive persona on-demand from sessions.**

If `sessions` doesn't store persona, the analysis function falls back to using `default` for every session. Suggestion: parse first user message of each session via the persona classifier (call `classify` from `opencomputer.awareness.personas.classifier`). Cost: O(N) classifier calls per `/profile-suggest` invocation. N=30, classifier <5ms each = <150ms total. Acceptable.

If neither path exists, persist `active_persona_id` on the `sessions` table when a session ends. This is a schema migration we want to avoid for this PR.

**Decision pending Step 1 output.** Document the chosen path here.

---

## Task 2: `/profile-suggest` slash command

**Files:**
- Create: `opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py`
- Modify: `opencomputer/agent/slash_commands.py` (register the new command)
- Create: `tests/test_profile_suggest_cli.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_profile_suggest_cli.py
def test_profile_suggest_command_renders_report(tmp_path, capsys):
    from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
        ProfileSuggestCommand,
    )
    cmd = ProfileSuggestCommand()
    # ... build a SlashContext with a SessionDB seeded with persona data ...
    result = cmd.execute(ctx)
    assert "Active profile" in result.message
    assert "persona breakdown" in result.message.lower()
```

- [ ] **Step 2: Implement command following the SlashCommand pattern**

(See `opencomputer/agent/slash_commands_impl/usage_cmd.py` and `history_cmd.py` for reference — they're the closest analog in shape.)

```python
# opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py
"""``/profile-suggest`` — analyze recent persona usage and recommend profile changes."""
from __future__ import annotations

from pathlib import Path

from plugin_sdk import SlashCommand, SlashCommandResult
from opencomputer.profile_analysis import compute_profile_suggestions


class ProfileSuggestCommand(SlashCommand):
    name = "profile-suggest"
    description = "Analyze persona usage and recommend profile create/switch."

    def execute(self, ctx) -> SlashCommandResult:
        from opencomputer.agent.config import _home
        from opencomputer.profiles import list_profiles, get_active_profile

        home = _home()
        current = get_active_profile()
        profiles = tuple(list_profiles())
        report = compute_profile_suggestions(
            home=home, db=ctx.db, current_profile=current, available_profiles=profiles,
        )
        return SlashCommandResult(message=_render(report))


def _render(report) -> str:
    """Render the report as a Rich-styled string."""
    lines = [
        "─" * 60,
        f"Active profile: {report.current_profile}",
        "",
        f"Recent persona breakdown (last {report.sessions_analyzed} sessions):",
    ]
    for entry in report.persona_breakdown:
        pct = (entry.count / max(report.sessions_analyzed, 1)) * 100
        lines.append(f"  {entry.persona_id:12} {entry.count:3} sessions  ({pct:.0f}%)")
    lines.append("")
    if report.suggestions:
        lines.append("Suggestions:")
        for s in report.suggestions:
            sigil = "✦" if s.kind in ("create", "switch") else "✓"
            lines.append(f"  {sigil} {s.rationale}")
            if s.command:
                lines.append(f"     {s.command}")
    else:
        lines.append("No suggestions — profile usage looks aligned.")
    lines.append("─" * 60)
    return "\n".join(lines)
```

- [ ] **Step 3: Register in `opencomputer/agent/slash_commands.py`**

Append `ProfileSuggestCommand` to the BUILTIN_COMMANDS tuple.

- [ ] **Step 4: Run tests + commit**

---

## Task 3: Empty-state teaching

**Files:**
- Modify: `opencomputer/cli_ui/empty_state.py`
- Modify: `opencomputer/cli_profile.py`
- Create: `tests/test_profile_empty_state.py`

- [ ] **Step 1: Add new helper functions to empty_state.py**

```python
def render_profile_list_only_default() -> str:
    return (
        "Only one profile. "
        "Tip: `/profile-suggest` analyzes your usage and recommends "
        "when a specialized profile would help."
    )


def render_profile_create_success(name: str) -> str:
    return (
        f"Created profile '{name}'.\n"
        "Tip: `/profile-suggest` shows when this profile makes sense to "
        "switch into based on your conversation pattern."
    )
```

- [ ] **Step 2: Wire into cli_profile.py**

In the `list` command handler, after rendering the profile table, check if only `default` exists and append the tip.

In the `create` command handler, after success, append the second tip.

- [ ] **Step 3: Tests + commit**

---

## Task 4: Learning Moment v3.1 (single new entry)

**Files:**
- Modify: `opencomputer/awareness/learning_moments/registry.py` (3 new Context fields + 1 LearningMoment)
- Modify: `opencomputer/awareness/learning_moments/predicates.py` (1 predicate)
- Modify: `opencomputer/agent/loop.py` (count persona flips per session, populate 3 new Context fields)
- Modify: `tests/test_learning_moments.py`

- [ ] **Step 1: Add 3 Context fields**

```python
# In Context dataclass:

persona_flips_in_session: int = 0
"""How many times the active persona changed within this session.
Used by ``suggest_profile_suggest_command`` to detect multi-context
usage (≥3 flips ⇒ suggest the analysis command)."""

current_profile_name: str = "default"
"""Name of the active profile (default when unset)."""

available_profiles: tuple[str, ...] = ()
"""Names of all profiles on disk under ``~/.opencomputer/profiles/*/``."""
```

- [ ] **Step 2: Add predicate**

```python
def suggest_profile_suggest_command(ctx: Context) -> bool:
    """User flipped persona ≥3 times in this session AND is on default profile."""
    return (
        ctx.persona_flips_in_session >= 3
        and ctx.current_profile_name == "default"
    )
```

- [ ] **Step 3: Add LearningMoment entry**

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

- [ ] **Step 4: Wire persona-flip counter in loop.py**

In `_maybe_reclassify_persona`, increment `self._persona_flips_in_session` whenever `self._active_persona_id` changes from a non-empty value to a different non-empty value. Reset to 0 at session start.

In the 3 LM call sites (mech A, B, C), pass `persona_flips_in_session`, `current_profile_name`, and `available_profiles` to Context.

- [ ] **Step 5: Tests + commit**

---

## Task 5: `oc doctor` profile-usage check

**Files:**
- Modify: `opencomputer/doctor.py`
- Create: `tests/test_doctor_profile_usage.py`

- [ ] **Step 1: Add health check function**

```python
def check_profile_usage(home: Path, db: SessionDB) -> HealthContribution:
    from opencomputer.profile_analysis import compute_profile_suggestions
    from opencomputer.profiles import list_profiles, get_active_profile

    report = compute_profile_suggestions(
        home=home, db=db,
        current_profile=get_active_profile(),
        available_profiles=tuple(list_profiles()),
    )
    create_count = sum(1 for s in report.suggestions if s.kind == "create")
    if create_count == 0:
        return HealthContribution(name="profile-usage", status=HealthStatus.OK, ...)
    first = next(s for s in report.suggestions if s.kind == "create")
    return HealthContribution(
        name="profile-usage",
        status=HealthStatus.WARN,
        message=(
            f"Profile usage: {first.rationale}. "
            "Run /profile-suggest for details."
        ),
    )
```

- [ ] **Step 2: Register in doctor's main check tuple**

- [ ] **Step 3: Tests + commit**

---

## Task 6: Full suite + ruff + audit + push

- [ ] **Step 1: pytest** — `pytest tests/test_profile_analysis.py tests/test_profile_suggest_cli.py tests/test_profile_empty_state.py tests/test_learning_moments.py tests/test_doctor_profile_usage.py -x -q`. All pass.

- [ ] **Step 2: ruff** — `ruff check .` clean.

- [ ] **Step 3: Wider regression** — `pytest tests/ -k "session or profile or learning_moments or doctor or persona or empty_state"`. Should be all green.

- [ ] **Step 4: Audit subagent** — dispatch a code reviewer with the diff range. Apply BLOCKERs inline.

- [ ] **Step 5: Push + open PR** with comprehensive summary.
