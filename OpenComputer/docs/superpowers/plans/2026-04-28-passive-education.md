# Passive Education ("Learning Moments") — Implementation Plan v1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship the minimum viable passive-education layer — a learning-moments registry + post-turn hook + 3 hand-curated moments + persistence + opt-out CLI. Designed to leave room for v2 mechanisms (system-prompt overlay, session-end reflection) without rework.

**Architecture:** New package `opencomputer/awareness/learning_moments/`. Predicates run post-turn in `loop.py` after streaming finishes. Result is appended as italic dim "tail clause" to the assistant turn. Persisted state in `~/.opencomputer/<profile>/learning_moments.json` (file-locked).

**Tech Stack:** Python 3.12+. No new deps. Reuses `vibe_log` from PR #205 + `MemoryConfig.declarative_path` for MEMORY.md.

---

## Task 1: `LearningMoment` dataclass + Severity enum

**Files:**
- Create: `opencomputer/awareness/learning_moments/__init__.py`
- Create: `opencomputer/awareness/learning_moments/registry.py`

- [ ] **Step 1: Write registry.py**

```python
"""Hand-curated registry of learning moments + dataclass.

A LearningMoment encodes ONE behavioral trigger + ONE inline reveal.
The registry is intentionally small — bigger means more cognitive load
on the user, not more value. v1 ships 3.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """Whether a moment respects the user's learning-off flag.

    ``tip``         — informational reveals; suppressed when the user
                      runs ``oc memory learning-off``.
    ``load_bearing`` — prompts that must fire regardless (e.g. the
                      smart-fallback prompt in PR #209) because skipping
                      them produces silent failure.
    """

    TIP = "tip"
    LOAD_BEARING = "load_bearing"


class Surface(str, Enum):
    """Which mechanism delivers the reveal.

    v1 only implements INLINE_TAIL. SYSTEM_PROMPT and SESSION_END are
    declared so v2 can dispatch by surface without changing this file.
    """

    INLINE_TAIL = "inline_tail"
    SYSTEM_PROMPT = "system_prompt"      # v2
    SESSION_END = "session_end"          # v2


@dataclass(frozen=True, slots=True)
class LearningMoment:
    """One reveal definition.

    Attributes
    ----------
    id:
        Stable string used as the persistence key. Renaming an id is
        a soft-breaking change (it re-fires for users who already saw
        the previous id).
    predicate:
        Callable returning True when the moment should fire. MUST be
        cheap on the hot path (called every turn). Expensive
        predicates pre-compute or run async — we only check the cheap
        result here.
    reveal:
        The user-facing string. v1 is INLINE_TAIL — appended after the
        assistant's response, italic dim, indented two spaces.
    severity:
        TIP (suppressible) or LOAD_BEARING (always fires).
    surface:
        Which mechanism delivers it. v1 == INLINE_TAIL only.
    min_oc_version:
        Skip if running on an older version. Reserved for moments
        that reference features added in a specific release.
    priority:
        Tie-break across moments whose predicates all fire on the
        same turn. Lower = higher priority.
    """

    id: str
    predicate: Callable[["Context"], bool]
    reveal: str
    severity: Severity = Severity.TIP
    surface: Surface = Surface.INLINE_TAIL
    min_oc_version: str = "0.0.0"
    priority: int = 50


@dataclass(frozen=True, slots=True)
class Context:
    """Snapshot of state passed to predicates.

    Built once per turn by the engine. Predicates read fields off
    this — they never query the DB themselves (that's a hot-path
    expense). Engine is responsible for cheap pre-fetching.
    """

    session_id: str
    profile_home: Any  # pathlib.Path — Any to avoid typing.TYPE_CHECKING gymnastics
    user_message: str
    memory_md_text: str
    vibe_log_session_count_total: int  # how many vibe_log rows in this session
    vibe_log_session_count_noncalm: int  # ditto, non-calm only
    sessions_db_total_sessions: int  # for returning-user seed gate


def all_moments() -> tuple[LearningMoment, ...]:
    """Return the v1 registry. Stable ordering for tests."""
    from opencomputer.awareness.learning_moments.predicates import (
        memory_continuity_first_recall,
        recent_files_paste,
        vibe_first_nonneutral,
    )

    return (
        LearningMoment(
            id="memory_continuity_first_recall",
            predicate=memory_continuity_first_recall,
            reveal="(I had this noted from last time — yell if it's stale.)",
            priority=10,
        ),
        LearningMoment(
            id="vibe_first_nonneutral",
            predicate=vibe_first_nonneutral,
            reveal=(
                "(I keep a small log of how each chat feels — "
                "`oc memory show vibe` if you want to see it.)"
            ),
            priority=20,
        ),
        LearningMoment(
            id="recent_files_paste",
            predicate=recent_files_paste,
            reveal=(
                "(You can drag files in directly — "
                "or just say 'show me X.py'.)"
            ),
            priority=30,
        ),
    )
```

- [ ] **Step 2: Write `__init__.py`**

```python
"""Passive education — surfaces OC capabilities indirectly.

Public API:

    select_reveal(session_id) -> str | None
        Called from the agent loop post-turn. Returns the reveal
        clause to append to the assistant's response, or None.

Architecture: see docs/superpowers/specs/2026-04-28-passive-education-design.md
"""
from opencomputer.awareness.learning_moments.engine import select_reveal
from opencomputer.awareness.learning_moments.registry import (
    Context,
    LearningMoment,
    Severity,
    Surface,
    all_moments,
)

__all__ = [
    "Context",
    "LearningMoment",
    "Severity",
    "Surface",
    "all_moments",
    "select_reveal",
]
```

- [ ] **Step 3: Commit**

## Task 2: Predicates module

**Files:**
- Create: `opencomputer/awareness/learning_moments/predicates.py`

- [ ] **Step 1: Write predicates.py**

```python
"""Trigger predicates for the v1 learning-moments registry.

Each predicate takes a :class:`Context` and returns ``bool``. All
predicates here are O(1) given a pre-built Context — the heavy
lifting (DB queries) lives in the engine, which builds Context once
per turn.
"""
from __future__ import annotations

import re

from opencomputer.awareness.learning_moments.registry import Context


# Word-boundary path-like pattern. Catches:
#   /Users/foo/bar.py
#   ~/Documents/notes.md
#   src/auth/login.ts
# Avoids false positives on plain mentions of "src" or version numbers.
_PATH_RE = re.compile(
    r"(?:(?:[~/.]|\b[A-Za-z0-9_-]+/)[A-Za-z0-9_./-]+\.[A-Za-z0-9]+)"
)


def memory_continuity_first_recall(ctx: Context) -> bool:
    """User's latest message contains a substring also in MEMORY.md.

    Cheapest possible match — no NLP, just a substring scan against
    the entire memory text. Word-boundary check prevents tiny
    one-character matches. False positives are acceptable; the worst
    case is one harmless reveal.
    """
    if not ctx.memory_md_text or len(ctx.user_message) < 4:
        return False
    msg = ctx.user_message.lower()
    mem = ctx.memory_md_text.lower()
    # Pick the longest 3-word window from the user's message and
    # check if it appears in memory. This lets us be confident the
    # match isn't a stopword coincidence.
    words = msg.split()
    if len(words) < 3:
        return False
    for i in range(len(words) - 2):
        window = " ".join(words[i : i + 3])
        if len(window) >= 12 and window in mem:
            return True
    return False


def vibe_first_nonneutral(ctx: Context) -> bool:
    """First time this session has a vibe verdict other than ``calm``."""
    return (
        ctx.vibe_log_session_count_total > 0
        and ctx.vibe_log_session_count_noncalm == 1
    )


def recent_files_paste(ctx: Context) -> bool:
    """User's message contains a path-like string."""
    if len(ctx.user_message) > 5000:
        return False  # paste too large to scan cheaply
    return bool(_PATH_RE.search(ctx.user_message))
```

- [ ] **Step 2: Commit**

## Task 3: Persistence store

**Files:**
- Create: `opencomputer/awareness/learning_moments/store.py`

- [ ] **Step 1: Write store.py**

```python
"""``learning_moments.json`` reader/writer with file-locking.

State shape::

    {
        "version": 1,
        "moments_fired": {
            "memory_continuity_first_recall": 1714324800.0,
            "vibe_first_nonneutral": 1714411200.0
        },
        "fire_log": [
            {"id": "...", "fired_at": 1714324800.0}
        ],
        "first_reveal_appended": true
    }

``moments_fired`` is the dedup map (one fire per moment id, ever).
``fire_log`` is the rolling cap-enforcement log (only the last
14 days kept). ``first_reveal_appended`` ensures the opt-out hint is
appended exactly once.

Concurrent ``oc chat`` sessions are guarded by ``fcntl.flock`` on the
JSON file's parent directory marker (``.lock``). On platforms without
``fcntl`` (Windows) we fall through and accept best-effort writes —
the worst case is one duplicate fire on a same-day race.
"""
from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

_SCHEMA_VERSION = 1
_FIRE_LOG_RETENTION_SECONDS = 14 * 24 * 3600


@dataclass(slots=True)
class StoreState:
    moments_fired: dict[str, float] = field(default_factory=dict)
    fire_log: list[dict] = field(default_factory=list)
    first_reveal_appended: bool = False


def _path(profile_home: Path) -> Path:
    return profile_home / "learning_moments.json"


def load(profile_home: Path) -> StoreState:
    """Return the store state, or an empty state if the file is missing."""
    p = _path(profile_home)
    if not p.exists():
        return StoreState()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return StoreState()
    if not isinstance(raw, dict):
        return StoreState()
    moments_fired = raw.get("moments_fired", {})
    if not isinstance(moments_fired, dict):
        moments_fired = {}
    fire_log = raw.get("fire_log", [])
    if not isinstance(fire_log, list):
        fire_log = []
    first = bool(raw.get("first_reveal_appended", False))
    return StoreState(
        moments_fired={k: float(v) for k, v in moments_fired.items()},
        fire_log=[e for e in fire_log if isinstance(e, dict)],
        first_reveal_appended=first,
    )


def save(profile_home: Path, state: StoreState) -> None:
    """Write the state. Best-effort file lock; degrades on platforms
    without ``fcntl``."""
    p = _path(profile_home)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Trim fire_log to retention window.
    cutoff = time.time() - _FIRE_LOG_RETENTION_SECONDS
    state.fire_log = [e for e in state.fire_log if float(e.get("fired_at", 0)) >= cutoff]
    payload = {
        "version": _SCHEMA_VERSION,
        "moments_fired": state.moments_fired,
        "fire_log": state.fire_log,
        "first_reveal_appended": state.first_reveal_appended,
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    with contextlib.ExitStack() as cleanup:
        try:
            import fcntl
            lock = open(p.parent / ".learning_moments.lock", "w")
            cleanup.callback(lock.close)
            fcntl.flock(lock, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass  # best-effort
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)


def seed_returning_user(profile_home: Path, total_sessions: int) -> None:
    """If the file is absent AND the user has prior sessions, seed
    every moment as already-fired so a returning user doesn't get a
    burst of reveals when the file appears.

    Threshold: 5 prior sessions. Tuned conservatively — a user with
    5+ sessions has earned the right to silence.
    """
    if total_sessions < 5:
        return
    if _path(profile_home).exists():
        return
    from opencomputer.awareness.learning_moments.registry import all_moments

    now = time.time()
    state = StoreState(
        moments_fired={m.id: now for m in all_moments()},
        first_reveal_appended=True,  # don't surface the opt-out either
    )
    save(profile_home, state)
```

- [ ] **Step 2: Commit**

## Task 4: Engine — selection + cap enforcement

**Files:**
- Create: `opencomputer/awareness/learning_moments/engine.py`

- [ ] **Step 1: Write engine.py**

```python
"""Engine — picks at most one moment to fire per turn.

Public entry: :func:`select_reveal`. Called from the agent loop
post-turn. Returns the formatted reveal string to append, or ``None``.

Cap policy: ≤1 reveal per UTC-day; ≤3 per UTC-week. Enforced by
counting entries in ``fire_log`` whose timestamps fall in those
windows. Per-moment dedup is ``moments_fired``: once fired, never
again for that profile.

Severity: ``learning_off`` (a flag bit on disk) suppresses ``tip``
moments but never ``load_bearing``. Load-bearing reveals are immune
to caps too — a load-bearing reveal MUST fire when its predicate
matches.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from pathlib import Path
from typing import Any

from opencomputer.awareness.learning_moments.registry import (
    LearningMoment,
    Severity,
    all_moments,
)
from opencomputer.awareness.learning_moments.store import (
    StoreState,
    load,
    save,
    seed_returning_user,
)

_log = logging.getLogger("opencomputer.awareness.learning_moments")


def _is_learning_off(profile_home: Path) -> bool:
    """``oc memory learning-off`` writes a marker file. Existence of
    that file means tip-severity reveals are suppressed."""
    return (profile_home / ".learning_off").exists()


def _today_utc() -> str:
    return _dt.datetime.now(tz=_dt.UTC).strftime("%Y-%m-%d")


def _week_utc() -> str:
    return _dt.datetime.now(tz=_dt.UTC).strftime("%Y-W%V")


def _cap_hit(state: StoreState) -> bool:
    """1/day, 3/week."""
    today = _today_utc()
    week = _week_utc()
    fired_today = 0
    fired_week = 0
    now = time.time()
    for entry in state.fire_log:
        ts = float(entry.get("fired_at", 0))
        if now - ts > 7 * 24 * 3600:
            continue
        d = _dt.datetime.fromtimestamp(ts, tz=_dt.UTC)
        if d.strftime("%Y-%m-%d") == today:
            fired_today += 1
        if d.strftime("%Y-W%V") == week:
            fired_week += 1
    return fired_today >= 1 or fired_week >= 3


def select_reveal(*, ctx_builder: Any | None = None, profile_home: Path) -> str | None:
    """Return a formatted reveal clause to append, or None.

    The caller (agent loop) provides:

    * ``ctx_builder`` — a zero-arg callable that builds the
      :class:`Context`. Lazy so the build cost (DB reads) is paid
      only when caps allow firing in the first place.
    * ``profile_home`` — directory where state lives.

    If any predicate raises, that moment is skipped and the engine
    tries the next. Never raises to the caller.
    """
    state = load(profile_home)

    learning_off = _is_learning_off(profile_home)
    cap_hit = _cap_hit(state)

    # Sort moments by priority (lower = higher priority).
    moments = sorted(all_moments(), key=lambda m: m.priority)

    # Build context lazily — only if at least one moment is eligible.
    eligible: list[LearningMoment] = []
    for m in moments:
        if m.id in state.moments_fired:
            continue
        if learning_off and m.severity == Severity.TIP:
            continue
        if cap_hit and m.severity == Severity.TIP:
            continue
        eligible.append(m)
    if not eligible:
        return None

    if ctx_builder is None:
        return None
    try:
        ctx = ctx_builder()
    except Exception:  # noqa: BLE001 — never break loop on context build
        _log.debug("learning_moments: context build failed", exc_info=True)
        return None

    for m in eligible:
        try:
            if m.predicate(ctx):
                # Fire.
                state.moments_fired[m.id] = time.time()
                state.fire_log.append({"id": m.id, "fired_at": time.time()})
                reveal = m.reveal
                if not state.first_reveal_appended and m.severity == Severity.TIP:
                    reveal = (
                        reveal
                        + "\n  (turn these off: `oc memory learning-off`)"
                    )
                    state.first_reveal_appended = True
                save(profile_home, state)
                return _format_inline_tail(reveal)
        except Exception:  # noqa: BLE001
            _log.debug(
                "learning_moments: predicate failed for %s", m.id, exc_info=True,
            )
            continue
    return None


def _format_inline_tail(reveal: str) -> str:
    """Two-space indent + leading newline. Caller (loop) is responsible
    for inserting this after streaming completes."""
    indented = "\n".join("  " + line if line else "" for line in reveal.splitlines())
    return "\n" + indented


# Public entry called from CLI + loop init paths.

def maybe_seed_returning_user(profile_home: Path, total_sessions: int) -> None:
    """Idempotent seeding for users with prior sessions but no
    learning_moments.json yet. Called once at agent loop start."""
    seed_returning_user(profile_home, total_sessions)
```

- [ ] **Step 2: Commit**

## Task 5: CLI — `oc memory learning-off / learning-on / learning-status`

**Files:**
- Modify: `opencomputer/cli_memory.py`

- [ ] **Step 1: Add the three subcommands**

```python
@memory_app.command("learning-off")
def memory_learning_off() -> None:
    """Suppress tip-severity learning-moment reveals."""
    from opencomputer.agent.config import _home
    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    (home / ".learning_off").write_text("off\n")
    typer.echo("Learning-moment tips suppressed. Re-enable: oc memory learning-on")


@memory_app.command("learning-on")
def memory_learning_on() -> None:
    """Re-enable learning-moment tips."""
    from opencomputer.agent.config import _home
    marker = _home() / ".learning_off"
    if marker.exists():
        marker.unlink()
    typer.echo("Learning-moment tips re-enabled.")


@memory_app.command("learning-status")
def memory_learning_status() -> None:
    """Show whether tips are on/off + which moments have fired."""
    from opencomputer.agent.config import _home
    from opencomputer.awareness.learning_moments.store import load
    home = _home()
    off = (home / ".learning_off").exists()
    state = load(home)
    typer.echo(f"Learning tips: {'OFF' if off else 'ON'}")
    typer.echo(f"Moments fired: {len(state.moments_fired)}")
    for moment_id, fired_at in state.moments_fired.items():
        import datetime as _dt
        when = _dt.datetime.fromtimestamp(fired_at).isoformat(timespec="seconds")
        typer.echo(f"  - {moment_id} ({when})")
```

- [ ] **Step 2: Commit**

## Task 6: Hook into agent loop

**Files:**
- Modify: `opencomputer/agent/loop.py` — find the post-turn point AFTER streaming has finished and before returning to the caller

- [ ] **Step 1: Find the right hook point**

Search for where the assistant message is fully assembled and committed to the DB. Hook AFTER the DB write but BEFORE returning the message to the caller.

- [ ] **Step 2: Add the call**

```python
# Passive education — append a learning-moment reveal if any fires.
try:
    from opencomputer.agent.config import _home
    from opencomputer.awareness.learning_moments import select_reveal
    from opencomputer.awareness.learning_moments.engine import (
        maybe_seed_returning_user,
    )

    profile_home = _home()
    # First-call seeding for returning users.
    total_sessions = self.db.count_sessions()  # add helper if missing
    maybe_seed_returning_user(profile_home, total_sessions)

    def _build_ctx():
        from opencomputer.awareness.learning_moments import Context
        memory_path = self.config.memory.declarative_path
        memory_text = ""
        try:
            memory_text = memory_path.read_text(encoding="utf-8")
        except OSError:
            pass
        vibe_rows = self.db.list_vibe_log_for_session(sid)
        return Context(
            session_id=sid,
            profile_home=profile_home,
            user_message=last_user_message_content or "",
            memory_md_text=memory_text,
            vibe_log_session_count_total=len(vibe_rows),
            vibe_log_session_count_noncalm=sum(
                1 for r in vibe_rows if r["vibe"] != "calm"
            ),
            sessions_db_total_sessions=total_sessions,
        )

    reveal = select_reveal(ctx_builder=_build_ctx, profile_home=profile_home)
    if reveal:
        # Append to the assistant message text. The streaming layer
        # already finished; this is post-flush.
        assistant_message.content = (assistant_message.content or "") + reveal
except Exception:  # noqa: BLE001 — never break the turn over a tip
    _log.debug("learning_moments hook failed", exc_info=True)
```

- [ ] **Step 3: Add `count_sessions` helper to SessionDB if missing**

```python
def count_sessions(self) -> int:
    with self._connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    return int(row[0]) if row else 0
```

- [ ] **Step 4: Commit**

## Task 7: Tests

**Files:**
- Create: `tests/test_learning_moments.py`

- [ ] **Step 1: Write tests**

Test categories:
1. Predicate unit tests — each predicate fires/doesn't-fire on synthetic context
2. Store round-trip — save/load preserves state, JSON shape stable
3. Cap enforcement — 1/day, 3/week, both UTC
4. Severity — tip suppressed by learning-off; load-bearing not
5. Returning-user seed — file absent + sessions > 5 → seed; file absent + sessions ≤ 5 → no seed
6. First-reveal opt-out hint — appended once, not on subsequent reveals
7. Predicate exception → caught, next moment tried
8. Concurrent run race — two engines, second one sees fired marker, doesn't re-fire (relies on file lock; on platforms without flock, accepts best-effort)
9. CLI: `oc memory learning-off` writes marker; `learning-on` removes it
10. End-to-end: build a full Context that triggers `vibe_first_nonneutral`, call `select_reveal`, assert formatted output

Target ~25 tests.

- [ ] **Step 2: Run tests + commit**

## Task 8: CHANGELOG + PR

- [ ] **Step 1: CHANGELOG entry**

In `[Unreleased]`:

```markdown
### Added — passive education ("learning moments") v1

OpenComputer's discoverability gap closes a notch. New mechanism that
surfaces ONE inline reveal per UTC-day (max 3/week) when user behavior
matches a hand-curated trigger. v1 ships 3 moments — memory recall,
first non-calm vibe, file path paste — designed to be invisible until
relevant. Disable: `oc memory learning-off`.

Architecture leaves room for v2 mechanisms (system-prompt overlay,
session-end reflection) without rework. Spec:
docs/superpowers/specs/2026-04-28-passive-education-design.md
```

- [ ] **Step 2: Open PR with the standard template**

- [ ] **Step 3: Verify CI green**

## Definition of done

- 25+ tests pass
- `4222+` total tests pass (was 4222 before this PR)
- ruff clean
- `oc memory learning-status` runs and shows expected output
- A synthetic session that triggers `vibe_first_nonneutral` fires the reveal AT MOST once
- A returning user (5+ sessions) does NOT see a reveal burst on first run after the JSON appears
- CHANGELOG + spec + plan committed with the implementation
