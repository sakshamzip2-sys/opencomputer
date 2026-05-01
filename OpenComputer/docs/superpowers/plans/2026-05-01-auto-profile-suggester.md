# Auto-Profile-Suggester Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (chosen by user) or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daily background pattern-detection system that proactively suggests creating profiles based on time-of-day and cwd usage patterns, with one-click acceptance via `/profile-suggest accept <name>` that auto-creates the profile with a seeded SOUL.md.

**Architecture:** Three additive components on existing infrastructure: (1) `profile_analysis_daily.py` extends `profile_analysis.py` with time-of-day + cwd binning + cache I/O; (2) `oc profile analyze install/uninstall/status` writes launchd plists (macOS) and systemd timers (Linux); (3) the existing `suggest_profile_suggest_command` Learning Moment is upgraded to read the cache + fire fresh actionable suggestions, while a new `/profile-suggest accept|dismiss` slash subcommand auto-creates profiles with seeded SOUL.md.

**Tech Stack:** Python 3.12+, Typer (CLI), launchd plist (macOS), systemd-user (Linux), SQLite (sessions table), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-05-01-auto-profile-suggester-design.md`.

---

## File structure

| Path | Action | Responsibility |
|---|---|---|
| `opencomputer/agent/state.py` | Modify | Add `cwd` column to sessions table; capture `os.getcwd()` in `record_session_start` |
| `opencomputer/agent/loop.py` | Modify | Pass cwd to `record_session_start` (existing call at session boundary) |
| `opencomputer/profile_analysis_daily.py` | **Create** | Time-of-day + cwd pattern detection; cache I/O; dismissal handling |
| `opencomputer/cli_profile_analyze.py` | **Create** | `oc profile analyze {run,install,uninstall,status}` Typer subapp |
| `opencomputer/service/launchd.py` | **Create** | macOS launchd plist install/uninstall (mirror of `service/__init__.py` for systemd) |
| `opencomputer/service/templates/com.opencomputer.profile-analyze.plist` | **Create** | launchd plist template |
| `opencomputer/service/templates/opencomputer-profile-analyze.timer` | **Create** | systemd timer template |
| `opencomputer/service/templates/opencomputer-profile-analyze.service` | **Create** | systemd service template |
| `opencomputer/profile_seeder.py` | **Create** | Seeded SOUL.md generator from detected pattern |
| `opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py` | Modify | Add `accept` + `dismiss` subcommands |
| `opencomputer/awareness/learning_moments/predicates.py` | Modify | Upgrade `suggest_profile_suggest_command` to also read cache |
| `opencomputer/cli.py` | Modify | Wire `cli_profile_analyze.profile_analyze_app` into the `oc profile` group |
| `tests/test_session_cwd_capture.py` | **Create** | SessionDB cwd column + capture tests |
| `tests/test_profile_analysis_daily.py` | **Create** | Pattern detector + cache + dismissal tests |
| `tests/test_cli_profile_analyze.py` | **Create** | CLI subcommand tests |
| `tests/test_launchd_install.py` | **Create** | launchd plist install/uninstall tests |
| `tests/test_profile_seeder.py` | **Create** | Seeded SOUL.md tests |
| `tests/test_profile_suggest_accept_dismiss.py` | **Create** | accept/dismiss subcommand tests |
| `tests/test_profile_suggest_lm.py` | **Create** | LM predicate cache-read tests |

---

## Task 1: SessionDB `cwd` column + capture

**Files:**
- Modify: `opencomputer/agent/state.py` (sessions schema + record_session_start signature)
- Modify: `opencomputer/agent/loop.py` (pass cwd at session start)
- Create: `tests/test_session_cwd_capture.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_session_cwd_capture.py`:

```python
"""Plan 3 of 3 — Session cwd capture for profile-suggester pattern detection."""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.state import SessionDB


def test_session_cwd_persisted(tmp_path: Path) -> None:
    """record_session_start writes cwd; list_sessions returns it."""
    db = SessionDB(tmp_path / "test.db")
    db.record_session_start(
        session_id="sid-1",
        platform="cli",
        model="test-model",
        cwd="/Users/test/Vscode/work",
    )
    rows = db.list_sessions(limit=10)
    assert len(rows) == 1
    assert rows[0].cwd == "/Users/test/Vscode/work"


def test_session_cwd_optional_for_legacy_rows(tmp_path: Path) -> None:
    """Old rows (pre-migration) have NULL cwd; reads return None."""
    db_path = tmp_path / "legacy.db"
    import sqlite3
    # Simulate a pre-migration DB with no cwd column
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
            platform TEXT NOT NULL, model TEXT, title TEXT,
            message_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0, vibe TEXT, vibe_updated REAL
        );
        INSERT INTO sessions (id, started_at, platform, model)
        VALUES ('legacy-1', 1000.0, 'cli', 'old-model');
    """)
    conn.commit()
    conn.close()

    db = SessionDB(db_path)  # Should auto-migrate
    rows = db.list_sessions(limit=10)
    assert len(rows) == 1
    assert rows[0].cwd is None  # Legacy row has NULL cwd
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_session_cwd_capture.py -v`
Expected: FAIL — `record_session_start` doesn't accept `cwd` kwarg.

- [ ] **Step 3: Add cwd column to schema in `agent/state.py`**

First grep to find the existing migration block:

```bash
grep -n "ALTER TABLE sessions\|CREATE TABLE IF NOT EXISTS sessions" opencomputer/agent/state.py
```

Two edits required:

(a) Find the sessions DDL (around `agent/state.py:44-57`) and add `cwd TEXT` after `vibe_updated`:

```sql
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    started_at    REAL NOT NULL,
    ended_at      REAL,
    platform      TEXT NOT NULL,
    model         TEXT,
    title         TEXT,
    message_count INTEGER DEFAULT 0,
    input_tokens  INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    vibe          TEXT,
    vibe_updated  REAL,
    cwd           TEXT     -- Plan 3: working dir at session start, for cwd-pattern detection
);
```

(b) Add a migration block for legacy DBs. The grep result tells you whether one exists. **If `ALTER TABLE sessions` matches already exist** (existing migrations from prior schema changes), append a sibling block to that section. **If no `ALTER TABLE` block exists**, add one immediately after the `CREATE TABLE IF NOT EXISTS sessions` block in `SessionDB.__init__` (or wherever the schema is initialized — search for where the DDL string is `executed`):

```python
# Plan 3 migration: add cwd column to legacy DBs.
# CREATE TABLE IF NOT EXISTS handles fresh installs (column included
# in the DDL); this ALTER handles upgrade-in-place from a pre-Plan-3
# DB. The OperationalError catch makes this idempotent: re-running on
# a DB that already has cwd is a no-op.
try:
    self._conn.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
    self._conn.commit()
except sqlite3.OperationalError:
    pass  # column already exists — fresh DB or already migrated
```

- [ ] **Step 4: Update `record_session_start` signature**

Find `record_session_start` in `agent/state.py` (around line 488). Add `cwd: str | None = None` to signature, include in the INSERT:

```python
def record_session_start(
    self,
    *,
    session_id: str,
    platform: str,
    model: str | None = None,
    title: str | None = None,
    cwd: str | None = None,  # Plan 3 — for cwd-pattern detection
) -> None:
    """..."""
    with self._conn:
        self._conn.execute(
            """
            INSERT INTO sessions (id, started_at, platform, model, title, cwd)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              started_at = excluded.started_at,
              platform   = excluded.platform,
              model      = excluded.model,
              title      = excluded.title,
              cwd        = excluded.cwd
            """,
            (session_id, time.time(), platform, model, title, cwd),
        )
```

Also add `cwd` to the `Session` dataclass (or whatever shape `list_sessions` returns) so the test's `rows[0].cwd` works.

- [ ] **Step 5: Pass cwd from `loop.py` at session start**

Find the call to `record_session_start` in `agent/loop.py` and add `cwd=os.getcwd()`. If the call is buried in `run_conversation` after session_id is assigned, the simplest patch:

```python
self._db.record_session_start(
    session_id=sid,
    platform=...,  # whatever's already there
    model=...,
    cwd=os.getcwd(),  # Plan 3
)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_session_cwd_capture.py -v`
Expected: 2 PASS.

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/ -k "session" -q`
Expected: All session-touching tests still pass (no regressions from the schema change).

- [ ] **Step 7: Ruff + commit**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && ruff check opencomputer/agent/state.py opencomputer/agent/loop.py tests/test_session_cwd_capture.py`
Expected: clean.

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/agent/state.py OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/test_session_cwd_capture.py
git commit -m "$(cat <<'EOF'
feat(state): persist session cwd for Plan 3 pattern detection

Adds ``cwd TEXT`` column to the sessions table with a backwards-
compatible ALTER for legacy DBs (NULL for pre-migration rows).
``record_session_start`` accepts a ``cwd`` kwarg; AgentLoop passes
``os.getcwd()`` at session start.

Plan 3 of 3 (auto-profile-suggester) Task 1: cwd is the input signal
for the cwd-pattern clusterer in profile_analysis_daily.py (Task 2).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `profile_analysis_daily.py` — pattern detector + cache I/O

**Files:**
- Create: `opencomputer/profile_analysis_daily.py`
- Create: `tests/test_profile_analysis_daily.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_profile_analysis_daily.py`:

```python
"""Plan 3 — pattern detector + cache + dismissal tests."""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.profile_analysis_daily import (
    DailySuggestion,
    bin_by_time_of_day,
    bin_by_cwd,
    compute_daily_suggestions,
    load_cache,
    save_cache,
    is_dismissed,
    record_dismissal,
)


def test_bin_by_time_of_day_clusters_when_over_threshold() -> None:
    """≥70% of sessions in a 4-hour band → cluster."""
    # 30 sessions, 22 between 9am-1pm, 8 random
    timestamps = [
        # 22 morning (epoch seconds, hour 9-12)
        *(_ts(hour=h) for h in [9, 10, 10, 11, 11, 11, 9, 12, 10, 9,
                                  11, 12, 9, 10, 11, 9, 12, 10, 11, 9, 10, 12]),
        # 8 spread across other hours
        *(_ts(hour=h) for h in [3, 5, 7, 14, 16, 19, 21, 23]),
    ]
    clusters = bin_by_time_of_day(timestamps, min_pct=0.7, band_hours=4)
    assert len(clusters) == 1
    assert clusters[0].band_start_hour == 9
    assert clusters[0].band_end_hour == 12
    assert clusters[0].session_count >= 21  # ≥70% of 30


def test_bin_by_time_of_day_no_cluster_below_threshold() -> None:
    """<70% in any band → no cluster."""
    timestamps = [_ts(hour=h) for h in range(24)] * 2  # 48 evenly spread
    clusters = bin_by_time_of_day(timestamps, min_pct=0.7, band_hours=4)
    assert clusters == []


def test_bin_by_cwd_clusters_subtree() -> None:
    """≥40% sessions in one directory subtree → cluster."""
    cwds = [
        "/Users/x/Vscode/work-project",
        "/Users/x/Vscode/work-project/sub",
        "/Users/x/Vscode/another",
        "/Users/x/Vscode/work-project",
        "/Users/x/Vscode/work-project",
        "/Users/x/Documents",
        "/Users/x/Desktop",
    ]
    clusters = bin_by_cwd(cwds, min_pct=0.4)
    assert len(clusters) >= 1
    assert any("Vscode" in c.path for c in clusters)


def test_compute_daily_suggestions_skips_when_under_min_sessions(tmp_path: Path) -> None:
    """Cold-start: fewer than 10 sessions → no suggestions fired."""
    sessions = [
        _session(i, hour=10, persona="coding", cwd="/Users/x/Vscode/work")
        for i in range(5)
    ]
    suggestions = compute_daily_suggestions(sessions, available_profiles=("default",))
    assert suggestions == []


def test_compute_daily_suggestions_fires_clear_pattern() -> None:
    """30 sessions in a 9-12 morning band, all coding → suggest 'work' profile."""
    sessions = [
        _session(i, hour=10, persona="coding", cwd="/Users/x/Vscode/work")
        for i in range(30)
    ]
    suggestions = compute_daily_suggestions(sessions, available_profiles=("default",))
    assert len(suggestions) >= 1
    assert any(s.kind == "create" for s in suggestions)


def test_cache_round_trip(tmp_path: Path, monkeypatch) -> None:
    """save_cache writes JSON; load_cache reads it back."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    suggestions = [
        DailySuggestion(
            kind="create",
            name="work",
            persona="coding",
            rationale="22 morning sessions",
            command="/profile-suggest accept work",
        ),
    ]
    save_cache(suggestions=suggestions, dismissed=[])

    cached = load_cache()
    assert cached is not None
    assert cached["suggestions"][0]["name"] == "work"
    assert cached["dismissed"] == []


def test_dismissal_blocks_for_7_days(tmp_path: Path, monkeypatch) -> None:
    """record_dismissal('work') → is_dismissed('work') True for 7 days."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    save_cache(suggestions=[], dismissed=[])
    record_dismissal("work")
    assert is_dismissed("work") is True


def test_dismissal_expires_after_7_days(tmp_path: Path, monkeypatch) -> None:
    """After 7 days, is_dismissed returns False (suggestion can re-fire)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    # Manually write a stale dismissal
    cache = {
        "last_run": time.time(),
        "suggestions": [],
        "dismissed": [
            {"name": "work", "until": time.time() - 1.0},  # 1 second ago = expired
        ],
    }
    cache_path = tmp_path / "profile_analysis_cache.json"
    cache_path.write_text(json.dumps(cache))
    assert is_dismissed("work") is False


# Helpers ────────────────────────────────────────────────────────────


def _ts(hour: int) -> float:
    """Epoch timestamp at a given hour-of-day (today, in local time)."""
    import datetime as dt
    today = dt.date.today()
    naive = dt.datetime(today.year, today.month, today.day, hour=hour)
    return naive.timestamp()


def _session(idx: int, *, hour: int, persona: str, cwd: str):
    """Fake session row matching what compute_daily_suggestions consumes."""
    from types import SimpleNamespace
    return SimpleNamespace(
        id=f"sid-{idx}",
        started_at=_ts(hour),
        cwd=cwd,
        persona=persona,
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_profile_analysis_daily.py -v`
Expected: ImportError (`profile_analysis_daily` module doesn't exist).

- [ ] **Step 3: Implement `profile_analysis_daily.py`**

Create `opencomputer/profile_analysis_daily.py`:

```python
"""Plan 3 — daily background pattern detection for profile suggestions.

Extends profile_analysis.py with two NEW signals (time-of-day clusters,
cwd clusters) and adds disk-cache I/O for proactive surfacing via the
LM predicate.

Pattern-strength gates (load-bearing — addresses brittleness concerns):
  - Cold-start: <10 sessions → no suggestions.
  - Time-of-day: ≥70% of sessions in a 4-hour band over 30+ sessions.
  - cwd: ≥40% of sessions in one directory subtree over 10+ sessions.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Literal

logger = logging.getLogger("opencomputer.profile_analysis_daily")

_MIN_SESSIONS_FOR_ANALYSIS = 10
_LOOKBACK_SESSIONS = 30
_DISMISSAL_TTL_SECONDS = 7 * 24 * 3600  # 7 days


@dataclass(frozen=True, slots=True)
class TimeCluster:
    band_start_hour: int  # inclusive
    band_end_hour: int    # exclusive
    session_count: int
    pct: float


@dataclass(frozen=True, slots=True)
class CwdCluster:
    path: str  # the common ancestor directory
    session_count: int
    pct: float


@dataclass(frozen=True, slots=True)
class DailySuggestion:
    kind: Literal["create", "switch"]
    name: str
    persona: str
    rationale: str
    command: str


def bin_by_time_of_day(
    timestamps: Iterable[float],
    *,
    min_pct: float = 0.7,
    band_hours: int = 4,
) -> list[TimeCluster]:
    """Find time-of-day bands containing ≥``min_pct`` of sessions.

    Walks all candidate band starts (24 of them, hour 0..23). Returns the
    set of bands that pass the threshold. May return overlapping bands
    when usage is spread across two clusters; caller deduplicates.
    """
    ts_list = list(timestamps)
    if not ts_list:
        return []
    hours = [_dt.datetime.fromtimestamp(t).hour for t in ts_list]
    n = len(hours)
    out: list[TimeCluster] = []
    seen_starts: set[int] = set()
    # Try every possible band start; pick the first peak per
    # contiguous span of acceptable starts.
    for start in range(24):
        end = (start + band_hours) % 24
        if end > start:
            count = sum(1 for h in hours if start <= h < end)
        else:
            count = sum(1 for h in hours if h >= start or h < end)
        pct = count / n
        if pct >= min_pct and start not in seen_starts:
            out.append(TimeCluster(
                band_start_hour=start,
                band_end_hour=end,
                session_count=count,
                pct=pct,
            ))
            # Mark adjacent overlapping starts as seen so we don't
            # report the same cluster shifted by 1 hour.
            for adj in range(start, start + band_hours):
                seen_starts.add(adj % 24)
    return out


def bin_by_cwd(
    cwds: Iterable[str | None],
    *,
    min_pct: float = 0.4,
) -> list[CwdCluster]:
    """Find cwd subtrees containing ≥``min_pct`` of sessions.

    Counts at each ancestor directory level. The deepest ancestor that
    still passes the threshold is returned (most specific cluster).
    """
    paths = [Path(c) for c in cwds if c]
    if not paths:
        return []
    n = len(paths)
    # Count occurrences at every ancestor.
    counts: Counter[str] = Counter()
    for p in paths:
        for ancestor in (p, *p.parents):
            counts[str(ancestor)] += 1
    # Filter ancestors that pass the threshold AND are not the
    # filesystem root (/ or ~).
    candidates = [
        (path, count, count / n)
        for path, count in counts.items()
        if count / n >= min_pct
        and path not in ("/", str(Path.home()))
    ]
    if not candidates:
        return []
    # Pick the DEEPEST passing ancestor.
    candidates.sort(key=lambda x: -len(Path(x[0]).parts))
    deepest_path, deepest_count, deepest_pct = candidates[0]
    return [CwdCluster(path=deepest_path, session_count=deepest_count, pct=deepest_pct)]


def compute_daily_suggestions(
    sessions,
    *,
    available_profiles: tuple[str, ...],
) -> list[DailySuggestion]:
    """Produce suggestions from recent sessions. Empty list if cold-start.

    ``sessions`` is an iterable of session-like objects with attributes:
    ``started_at`` (float epoch), ``cwd`` (str|None), ``persona`` (str).
    """
    rows = list(sessions)
    if len(rows) < _MIN_SESSIONS_FOR_ANALYSIS:
        return []

    out: list[DailySuggestion] = []

    # Persona-cluster signal (existing logic from profile_analysis.py
    # — re-implemented here for self-containment).
    persona_counts = Counter(r.persona for r in rows if r.persona and r.persona != "default")
    for persona, count in persona_counts.items():
        if count < 3:
            continue
        # Use fuzzy matching against existing profile names so we don't
        # spuriously suggest "trading" when the user already has a
        # "stocks" profile (matches via PERSONA_PROFILE_MAP hint).
        if any(_fuzzy_match_profile(persona, p) for p in available_profiles):
            continue
        candidate_name = _persona_to_profile_name(persona)
        out.append(DailySuggestion(
            kind="create",
            name=candidate_name,
            persona=persona,
            rationale=f"{count} of last {len(rows)} sessions classified as {persona}",
            command=f"/profile-suggest accept {candidate_name}",
        ))

    # Time-of-day signal.
    time_clusters = bin_by_time_of_day(
        [r.started_at for r in rows], min_pct=0.7, band_hours=4,
    )
    for tc in time_clusters:
        # Pick the dominant persona within the time band.
        in_band = [
            r.persona for r in rows
            if r.persona and _hour_in_band(r.started_at, tc.band_start_hour, tc.band_end_hour)
        ]
        if not in_band:
            continue
        dominant_persona = Counter(in_band).most_common(1)[0][0]
        candidate_name = _persona_to_profile_name(dominant_persona)
        # Skip if user already has a fuzzy-matching profile.
        if any(_fuzzy_match_profile(dominant_persona, p) for p in available_profiles):
            continue
        # Skip if persona-cluster path already produced this name.
        if any(s.name == candidate_name for s in out):
            continue
        out.append(DailySuggestion(
            kind="create",
            name=candidate_name,
            persona=dominant_persona,
            rationale=(
                f"{tc.session_count} of last {len(rows)} sessions started "
                f"{tc.band_start_hour:02d}:00-{tc.band_end_hour:02d}:00, "
                f"mostly {dominant_persona}"
            ),
            command=f"/profile-suggest accept {candidate_name}",
        ))

    # cwd signal.
    cwd_clusters = bin_by_cwd([r.cwd for r in rows], min_pct=0.4)
    for cc in cwd_clusters:
        candidate_name = _cwd_to_profile_name(cc.path)
        if candidate_name in available_profiles:
            continue
        if any(s.name == candidate_name for s in out):
            continue
        out.append(DailySuggestion(
            kind="create",
            name=candidate_name,
            persona="coding",  # cwd clustering is most useful for coding contexts
            rationale=(
                f"{cc.session_count} of last {len(rows)} sessions started in "
                f"{cc.path} or subdirectories"
            ),
            command=f"/profile-suggest accept {candidate_name}",
        ))

    return out


# ─── Cache I/O ────────────────────────────────────────────────────────


def _cache_path() -> Path:
    """Resolve the cache file. Uses get_default_root for HOME-mutation immunity."""
    from opencomputer.profiles import get_default_root
    return get_default_root() / "profile_analysis_cache.json"


def load_cache() -> dict | None:
    """Read the cache, or return None if missing/corrupt."""
    path = _cache_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("load_cache: %s — treating as empty", exc)
        return None


def save_cache(*, suggestions: list[DailySuggestion], dismissed: list[dict]) -> None:
    """Write the cache (atomic via tmp + rename)."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_run": time.time(),
        "suggestions": [asdict(s) for s in suggestions],
        "dismissed": dismissed,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def record_dismissal(name: str) -> None:
    """Mark a suggestion as dismissed for 7 days."""
    cache = load_cache() or {"last_run": time.time(), "suggestions": [], "dismissed": []}
    until = time.time() + _DISMISSAL_TTL_SECONDS
    # Replace any existing dismissal for the same name.
    dismissed = [d for d in cache.get("dismissed", []) if d.get("name") != name]
    dismissed.append({"name": name, "until": until})
    save_cache(
        suggestions=[
            DailySuggestion(**s) for s in cache.get("suggestions", [])
            if s.get("name") != name
        ],
        dismissed=dismissed,
    )


def is_dismissed(name: str) -> bool:
    """True iff a fresh dismissal exists for this name."""
    cache = load_cache()
    if not cache:
        return False
    now = time.time()
    for d in cache.get("dismissed", []):
        if d.get("name") == name and d.get("until", 0) > now:
            return True
    return False


# ─── helpers ──────────────────────────────────────────────────────────


def _persona_to_profile_name(persona: str) -> str:
    """Persona id → suggested profile name."""
    return {
        "trading": "trading",
        "coding": "work",
        "companion": "personal",
        "relaxed": "leisure",
        "learning": "study",
    }.get(persona, persona)


def _fuzzy_match_profile(persona: str, profile_name: str) -> bool:
    """True iff an existing profile fuzzy-matches the persona.

    Reuses the PERSONA_PROFILE_MAP from profile_analysis.py so the
    fuzzy matching stays consistent across both modules.
    """
    from opencomputer.profile_analysis import PERSONA_PROFILE_MAP
    profile_lower = profile_name.lower()
    candidates = PERSONA_PROFILE_MAP.get(persona, ())
    return any(c in profile_lower or profile_lower in c for c in candidates)


def _cwd_to_profile_name(cwd: str) -> str:
    """cwd path → profile name (last component, sanitized)."""
    name = Path(cwd).name.lower().replace(" ", "-")
    # Sanitize: alphanum + dashes only.
    return "".join(c for c in name if c.isalnum() or c == "-") or "work"


def _hour_in_band(timestamp: float, start: int, end: int) -> bool:
    """True iff hour-of-day is within [start, end) accounting for wrap-around."""
    hour = _dt.datetime.fromtimestamp(timestamp).hour
    if end > start:
        return start <= hour < end
    return hour >= start or hour < end


__all__ = [
    "DailySuggestion",
    "TimeCluster",
    "CwdCluster",
    "bin_by_time_of_day",
    "bin_by_cwd",
    "compute_daily_suggestions",
    "load_cache",
    "save_cache",
    "record_dismissal",
    "is_dismissed",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && pytest tests/test_profile_analysis_daily.py -v`
Expected: 8 PASS.

- [ ] **Step 5: Ruff + commit**

Run: `cd /Users/saksham/Vscode/claude/OpenComputer && ruff check opencomputer/profile_analysis_daily.py tests/test_profile_analysis_daily.py`
Expected: clean.

```bash
cd /Users/saksham/Vscode/claude
git add OpenComputer/opencomputer/profile_analysis_daily.py OpenComputer/tests/test_profile_analysis_daily.py
git commit -m "$(cat <<'EOF'
feat(profile-suggester): pattern detector + cache + dismissal

Plan 3 of 3 Task 2: profile_analysis_daily.py.

Three signal sources:
  - Persona-classification clusters (≥3 sessions per persona)
  - Time-of-day clusters (≥70% in a 4-hour band over 10+ sessions)
  - cwd clusters (≥40% in one subtree over 10+ sessions)

Cold-start gate: <10 sessions → no suggestions. Pattern-strength
gates address the time-of-day false-positive concern explicitly
flagged in the spec.

Cache: ~/.opencomputer/profile_analysis_cache.json with atomic
write (tmp + rename). load_cache returns None on missing/corrupt;
save_cache writes JSON; record_dismissal sets a 7-day per-name
TTL; is_dismissed reads the TTL.

8 tests pass: time clustering (positive + negative), cwd clustering,
cold-start, clear-pattern, cache round-trip, dismissal block,
dismissal expiry.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: `oc profile analyze run` CLI

**Files:**
- Create: `opencomputer/cli_profile_analyze.py`
- Modify: `opencomputer/cli.py` (wire new typer subapp into `oc profile`)
- Create: `tests/test_cli_profile_analyze.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_profile_analyze.py`:

```python
"""Plan 3 Task 3 — `oc profile analyze run` CLI."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner


def test_analyze_run_writes_cache_when_history_present(tmp_path: Path, monkeypatch) -> None:
    """Run reads SessionDB, computes suggestions, writes cache."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    # Seed a SessionDB with enough rows
    from opencomputer.agent.state import SessionDB
    db_dir = tmp_path / "default" / "sessions.db"
    db_dir.parent.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_dir)
    for i in range(15):
        db.record_session_start(
            session_id=f"sid-{i}",
            platform="cli",
            model="test",
            cwd="/Users/test/Vscode/work",
        )

    from opencomputer.cli_profile_analyze import profile_analyze_app
    runner = CliRunner()
    result = runner.invoke(profile_analyze_app, ["run"])
    assert result.exit_code == 0
    assert (tmp_path / "profile_analysis_cache.json").exists()


def test_analyze_run_idempotent_on_empty_db(tmp_path: Path, monkeypatch) -> None:
    """No SessionDB → run prints "no analysis" message, exit 0."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.cli_profile_analyze import profile_analyze_app
    runner = CliRunner()
    result = runner.invoke(profile_analyze_app, ["run"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run, verify FAIL**

Run: `pytest tests/test_cli_profile_analyze.py -v`
Expected: ImportError.

- [ ] **Step 3: Create `cli_profile_analyze.py`**

```python
"""Plan 3 Task 3 — ``oc profile analyze`` Typer subapp.

Subcommands:
  run        — manual one-shot analysis (writes cache)
  install    — install OS-level cron (Task 5)
  uninstall  — remove OS-level cron (Task 5)
  status     — show install + last-run state (Task 5)
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

profile_analyze_app = typer.Typer(
    name="analyze",
    help="Analyze usage patterns and suggest profiles.",
    invoke_without_command=False,
)
_console = Console()


@profile_analyze_app.command("run")
def analyze_run() -> None:
    """One-shot: read SessionDB, compute suggestions, write cache."""
    from opencomputer.agent.state import SessionDB
    from opencomputer.profile_analysis_daily import (
        compute_daily_suggestions,
        save_cache,
    )
    from opencomputer.profiles import get_default_root, list_profiles

    # Find the default profile's SessionDB.
    db_path = get_default_root() / "default" / "sessions.db"
    if not db_path.exists():
        _console.print(
            "[yellow]No session history found at "
            f"{db_path} — nothing to analyze.[/yellow]"
        )
        return

    db = SessionDB(db_path)
    rows = db.list_sessions(limit=30)
    available = tuple(list_profiles())
    suggestions = compute_daily_suggestions(rows, available_profiles=available)

    # Preserve dismissals across runs.
    from opencomputer.profile_analysis_daily import load_cache
    prev = load_cache() or {}
    dismissed = prev.get("dismissed", [])

    save_cache(suggestions=suggestions, dismissed=dismissed)

    if not suggestions:
        _console.print(
            "[dim]Analyzed "
            f"{len(rows)} sessions — no clear patterns yet (need ≥10 sessions "
            "and a strong cluster). Cache updated.[/dim]"
        )
        return

    _console.print(
        f"[green]Analyzed {len(rows)} sessions — "
        f"{len(suggestions)} suggestion(s):[/green]"
    )
    for s in suggestions:
        _console.print(f"  • [bold]{s.name}[/bold] — {s.rationale}")
        _console.print(f"    Accept: [cyan]{s.command}[/cyan]")
```

- [ ] **Step 4: Wire into `oc profile` group**

In `opencomputer/cli.py`, find where the `profile_app` is wired in (search for `cli_profile.profile_app` or similar) and add the new analyze subapp:

```python
# Existing line wiring profile_app:
# app.add_typer(profile_app, name="profile")

# Plan 3 Task 3 — add the analyze subgroup
from opencomputer.cli_profile_analyze import profile_analyze_app
profile_app.add_typer(profile_analyze_app, name="analyze")
```

- [ ] **Step 5: Run tests + ruff + commit**

Run: `pytest tests/test_cli_profile_analyze.py -v`
Expected: 2 PASS.

Run: `ruff check opencomputer/cli_profile_analyze.py opencomputer/cli.py tests/test_cli_profile_analyze.py`
Expected: clean.

```bash
git add OpenComputer/opencomputer/cli_profile_analyze.py OpenComputer/opencomputer/cli.py OpenComputer/tests/test_cli_profile_analyze.py
git commit -m "feat(cli): oc profile analyze run — manual pattern analysis

Plan 3 of 3 Task 3. ``oc profile analyze run`` reads the default
profile's SessionDB, computes suggestions via
profile_analysis_daily.compute_daily_suggestions, and writes the
cache. Idempotent on cold-start (no DB / <10 sessions).

Wires the new typer subapp under the existing ``oc profile`` group
so the full command is ``oc profile analyze run``.

Tasks 4-5 will add ``install/uninstall/status`` subcommands.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
"
```

---

## Task 4: launchd plist install/uninstall (macOS)

**Files:**
- Create: `opencomputer/service/launchd.py`
- Create: `opencomputer/service/templates/com.opencomputer.profile-analyze.plist`
- Create: `tests/test_launchd_install.py`

- [ ] **Step 1: Write the failing test**

```python
"""Plan 3 Task 4 — launchd plist install/uninstall tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from opencomputer.service.launchd import (
    LaunchdUnsupportedError,
    install_launchd_plist,
    render_launchd_plist,
    uninstall_launchd_plist,
)


def test_render_launchd_plist_contains_required_keys(tmp_path: Path) -> None:
    """Rendered plist must have Label, ProgramArguments, and StartCalendarInterval."""
    body = render_launchd_plist(
        executable="/usr/local/bin/opencomputer",
        hour=9,
    )
    assert "<key>Label</key>" in body
    assert "com.opencomputer.profile-analyze" in body
    assert "<key>ProgramArguments</key>" in body
    assert "<key>StartCalendarInterval</key>" in body
    assert "<integer>9</integer>" in body


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_install_writes_plist_to_launchagents(tmp_path: Path, monkeypatch) -> None:
    """install_launchd_plist writes to ~/Library/LaunchAgents."""
    monkeypatch.setattr(
        "opencomputer.service.launchd._launch_agents_dir",
        lambda: tmp_path,
    )
    path = install_launchd_plist(executable="/bin/echo", hour=9)
    assert path.exists()
    assert path.name == "com.opencomputer.profile-analyze.plist"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only")
def test_uninstall_removes_plist(tmp_path: Path, monkeypatch) -> None:
    """uninstall_launchd_plist removes the file if present."""
    monkeypatch.setattr(
        "opencomputer.service.launchd._launch_agents_dir",
        lambda: tmp_path,
    )
    install_launchd_plist(executable="/bin/echo", hour=9)
    plist_path = tmp_path / "com.opencomputer.profile-analyze.plist"
    assert plist_path.exists()
    removed = uninstall_launchd_plist()
    assert removed == plist_path
    assert not plist_path.exists()


def test_install_rejects_non_macos(monkeypatch) -> None:
    """LaunchdUnsupportedError raised on non-macOS."""
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(LaunchdUnsupportedError):
        install_launchd_plist(executable="/bin/echo", hour=9)
```

- [ ] **Step 2: Run, verify FAIL**

Run: `pytest tests/test_launchd_install.py -v`
Expected: ImportError.

- [ ] **Step 3: Create the plist template**

Create `opencomputer/service/templates/com.opencomputer.profile-analyze.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.opencomputer.profile-analyze</string>
    <key>ProgramArguments</key>
    <array>
        <string>{executable}</string>
        <string>profile</string>
        <string>analyze</string>
        <string>run</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{hour}</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
```

- [ ] **Step 4: Implement `service/launchd.py`**

```python
"""launchd-user plist install/uninstall (macOS).

Mirror of service/__init__.py (which handles systemd) for macOS.
Installs into ~/Library/LaunchAgents/ and uses ``launchctl bootstrap``
to load the service.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

_TEMPLATE = (
    Path(__file__).parent / "templates" / "com.opencomputer.profile-analyze.plist"
).read_text()
_PLIST_NAME = "com.opencomputer.profile-analyze.plist"


class LaunchdUnsupportedError(RuntimeError):
    """Raised when launchd install is attempted on a non-macOS platform."""


def render_launchd_plist(*, executable: str, hour: int) -> str:
    """Render the plist body for the given parameters."""
    from opencomputer.profiles import real_user_home
    log_path = str(real_user_home() / ".opencomputer" / "profile-analyze.log")
    return _TEMPLATE.format(
        executable=executable,
        hour=hour,
        log_path=log_path,
    )


def _launch_agents_dir() -> Path:
    """~/Library/LaunchAgents — uses real_user_home for HOME-mutation immunity."""
    from opencomputer.profiles import real_user_home
    return real_user_home() / "Library" / "LaunchAgents"


def _launchctl(*args: str) -> tuple[int, str, str]:
    """Run launchctl; return (rc, stdout, stderr)."""
    if shutil.which("launchctl") is None:
        return (0, "", "(launchctl not found — skipping)")
    try:
        proc = subprocess.run(
            ["launchctl", *args], capture_output=True, text=True, timeout=10,
        )
        return (proc.returncode, proc.stdout, proc.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return (1, "", str(exc))


def install_launchd_plist(
    *,
    executable: str,
    hour: int = 9,
) -> Path:
    """Write the plist + bootstrap into the user's launchd domain.

    The plist's ProgramArguments is hardcoded to ``["profile",
    "analyze", "run"]`` — these are the args the daily cron passes to
    ``opencomputer``. If a future caller needs different args, extend
    the template instead of threading them through the API.
    """
    if sys.platform != "darwin":
        raise LaunchdUnsupportedError(
            f"launchd is macOS-only; got sys.platform={sys.platform!r}"
        )
    body = render_launchd_plist(executable=executable, hour=hour)
    target_dir = _launch_agents_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / _PLIST_NAME
    path.write_text(body)
    # Best-effort bootstrap (load) — uid is the current user's uid.
    import os
    uid = os.getuid()
    _launchctl("bootstrap", f"gui/{uid}", str(path))
    return path


def uninstall_launchd_plist() -> Path | None:
    """Bootout + remove the plist. Returns the removed path, or None if absent."""
    path = _launch_agents_dir() / _PLIST_NAME
    if not path.exists():
        return None
    import os
    uid = os.getuid()
    _launchctl("bootout", f"gui/{uid}/com.opencomputer.profile-analyze")
    path.unlink()
    return path


def is_loaded() -> bool:
    """Best-effort: True if `launchctl print` knows about the label."""
    import os
    uid = os.getuid()
    rc, _, _ = _launchctl("print", f"gui/{uid}/com.opencomputer.profile-analyze")
    return rc == 0


__all__ = [
    "LaunchdUnsupportedError",
    "install_launchd_plist",
    "is_loaded",
    "render_launchd_plist",
    "uninstall_launchd_plist",
]
```

- [ ] **Step 5: Run tests + ruff + commit**

Run: `pytest tests/test_launchd_install.py -v`
Expected: 4 PASS (3 macOS-only tests will skip on Linux but pass on macOS).

Run: `ruff check opencomputer/service/launchd.py tests/test_launchd_install.py`
Expected: clean.

```bash
git add OpenComputer/opencomputer/service/launchd.py OpenComputer/opencomputer/service/templates/com.opencomputer.profile-analyze.plist OpenComputer/tests/test_launchd_install.py
git commit -m "feat(service): launchd plist install/uninstall for macOS

Plan 3 Task 4 — mirror of service/__init__.py for macOS. Installs
into ~/Library/LaunchAgents/ and uses launchctl bootstrap/bootout.

Daily at 9am via StartCalendarInterval. Logs to
~/.opencomputer/profile-analyze.log. RunAtLoad=false so install
doesn't trigger an immediate run.

Tests: 4 pass (rendering test runs everywhere; install/uninstall
tests skipped on non-macOS).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
"
```

---

## Task 5: `oc profile analyze install/uninstall/status` CLI

**Files:**
- Modify: `opencomputer/cli_profile_analyze.py` (add 3 subcommands)
- Create: `opencomputer/service/templates/opencomputer-profile-analyze.timer`
- Create: `opencomputer/service/templates/opencomputer-profile-analyze.service`
- Modify: `opencomputer/service/__init__.py` (add a parallel set of functions for the profile-analyze unit)
- Create test extensions in `tests/test_cli_profile_analyze.py`

- [ ] **Step 1: Add systemd timer + service templates**

Create `opencomputer/service/templates/opencomputer-profile-analyze.timer`:

```ini
[Unit]
Description=OpenComputer daily profile-analyze
Requires=opencomputer-profile-analyze.service

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

Create `opencomputer/service/templates/opencomputer-profile-analyze.service`:

```ini
[Unit]
Description=OpenComputer profile-analyze one-shot

[Service]
Type=oneshot
ExecStart={executable} profile analyze run
StandardOutput=append:{log_path}
StandardError=append:{log_path}
```

- [ ] **Step 2: Extend `service/__init__.py` with profile-analyze unit functions**

Add to `service/__init__.py`:

```python
_PA_TIMER_TEMPLATE = (
    Path(__file__).parent / "templates" / "opencomputer-profile-analyze.timer"
).read_text()
_PA_SERVICE_TEMPLATE = (
    Path(__file__).parent / "templates" / "opencomputer-profile-analyze.service"
).read_text()


def install_profile_analyze_timer(*, executable: str) -> tuple[Path, Path]:
    """Install the daily profile-analyze systemd timer + service."""
    if not sys.platform.startswith("linux"):
        raise ServiceUnsupportedError(
            f"systemd is Linux-only; got sys.platform={sys.platform!r}"
        )
    from opencomputer.profiles import real_user_home
    log_path = str(real_user_home() / ".opencomputer" / "profile-analyze.log")
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    target_dir = Path(base) / "systemd" / "user"
    target_dir.mkdir(parents=True, exist_ok=True)
    timer_path = target_dir / "opencomputer-profile-analyze.timer"
    service_path = target_dir / "opencomputer-profile-analyze.service"
    timer_path.write_text(_PA_TIMER_TEMPLATE)
    service_path.write_text(
        _PA_SERVICE_TEMPLATE.format(executable=executable, log_path=log_path)
    )
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", "opencomputer-profile-analyze.timer")
    return (timer_path, service_path)


def uninstall_profile_analyze_timer() -> tuple[Path | None, Path | None]:
    """Stop + disable + remove the timer + service."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    target_dir = Path(base) / "systemd" / "user"
    timer_path = target_dir / "opencomputer-profile-analyze.timer"
    service_path = target_dir / "opencomputer-profile-analyze.service"
    timer_existed = timer_path.exists()
    _systemctl("stop", "opencomputer-profile-analyze.timer")
    _systemctl("disable", "opencomputer-profile-analyze.timer")
    if timer_existed:
        timer_path.unlink()
    if service_path.exists():
        service_path.unlink()
    _systemctl("daemon-reload")
    return (timer_path if timer_existed else None,
            service_path if service_path.exists() else None)


def is_profile_analyze_timer_active() -> bool:
    rc, out, _ = _systemctl("is-active", "opencomputer-profile-analyze.timer")
    return rc == 0 and out.strip() == "active"
```

Add to `__all__`: `install_profile_analyze_timer`, `uninstall_profile_analyze_timer`, `is_profile_analyze_timer_active`.

- [ ] **Step 3: Add `install/uninstall/status` to `cli_profile_analyze.py`**

Append to `opencomputer/cli_profile_analyze.py`:

```python
@profile_analyze_app.command("install")
def analyze_install() -> None:
    """Install the daily background analyzer cron (launchd or systemd)."""
    import shutil as _shutil
    import sys

    exe = _shutil.which("opencomputer") or f"{sys.executable} -m opencomputer"
    if sys.platform == "darwin":
        from opencomputer.service.launchd import install_launchd_plist
        path = install_launchd_plist(executable=exe)
        _console.print(f"[green]launchd plist installed:[/green] {path}")
        _console.print("Daily run at 9am local. View logs: "
                       "~/.opencomputer/profile-analyze.log")
    elif sys.platform.startswith("linux"):
        from opencomputer.service import install_profile_analyze_timer
        timer, service = install_profile_analyze_timer(executable=exe)
        _console.print(f"[green]systemd timer installed:[/green] {timer}")
        _console.print(f"[green]systemd service installed:[/green] {service}")
        _console.print("Daily via OnCalendar=daily.")
    else:
        _console.print(
            f"[yellow]No background scheduler for {sys.platform!r}.[/yellow] "
            "Run `oc profile analyze run` manually."
        )


@profile_analyze_app.command("uninstall")
def analyze_uninstall() -> None:
    """Remove the cron."""
    import sys
    if sys.platform == "darwin":
        from opencomputer.service.launchd import uninstall_launchd_plist
        path = uninstall_launchd_plist()
        if path:
            _console.print(f"[green]launchd plist removed:[/green] {path}")
        else:
            _console.print("[dim]launchd plist was not installed.[/dim]")
    elif sys.platform.startswith("linux"):
        from opencomputer.service import uninstall_profile_analyze_timer
        timer, service = uninstall_profile_analyze_timer()
        if timer:
            _console.print(f"[green]systemd timer removed:[/green] {timer}")
        else:
            _console.print("[dim]systemd timer was not installed.[/dim]")


@profile_analyze_app.command("status")
def analyze_status() -> None:
    """Show install state + last-run timestamp."""
    import sys
    from opencomputer.profile_analysis_daily import load_cache

    # Install state
    if sys.platform == "darwin":
        from opencomputer.service.launchd import is_loaded
        installed = is_loaded()
    elif sys.platform.startswith("linux"):
        from opencomputer.service import is_profile_analyze_timer_active
        installed = is_profile_analyze_timer_active()
    else:
        installed = False
    _console.print(f"Installed: {'[green]yes[/green]' if installed else '[red]no[/red]'}")

    # Last run
    cache = load_cache()
    if cache and "last_run" in cache:
        import datetime as _dt
        ts = _dt.datetime.fromtimestamp(cache["last_run"])
        _console.print(f"Last run: {ts.isoformat()}")
        _console.print(f"Suggestions in cache: {len(cache.get('suggestions', []))}")
    else:
        _console.print("[dim]Last run: never (cache absent).[/dim]")
```

- [ ] **Step 4: Run + ruff + commit**

Run: `pytest tests/test_cli_profile_analyze.py tests/test_launchd_install.py -v`
Expected: existing tests still PASS.

Run: `ruff check opencomputer/cli_profile_analyze.py opencomputer/service/__init__.py`
Expected: clean.

```bash
git add OpenComputer/opencomputer/cli_profile_analyze.py OpenComputer/opencomputer/service/__init__.py OpenComputer/opencomputer/service/templates/
git commit -m "feat(cli): oc profile analyze install/uninstall/status

Plan 3 Task 5 — cross-platform scheduler install. macOS uses
launchd (Task 4), Linux uses systemd-user timer + service via
new functions in service/__init__.py. Windows prints a friendly
message that scheduling isn't supported and falls back to manual
'oc profile analyze run'.

status command reports install state + last-run timestamp from
the cache.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
"
```

---

## Task 6: `profile_seeder.py` + `/profile-suggest accept|dismiss`

**Files:**
- Create: `opencomputer/profile_seeder.py`
- Modify: `opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py`
- Create: `tests/test_profile_seeder.py`
- Create: `tests/test_profile_suggest_accept_dismiss.py`

- [ ] **Step 1: Tests for `profile_seeder.py`**

```python
"""Plan 3 Task 6 — seeded SOUL.md generator."""
from __future__ import annotations

from opencomputer.profile_analysis_daily import DailySuggestion
from opencomputer.profile_seeder import render_seeded_soul


def test_seeded_soul_for_coding_persona() -> None:
    s = DailySuggestion(
        kind="create",
        name="work",
        persona="coding",
        rationale="22 of last 30 sessions classified as coding, 9am-12pm",
        command="/profile-suggest accept work",
    )
    soul = render_seeded_soul(s, user_name="Saksham")
    assert "work-mode agent for Saksham" in soul
    assert "coding" in soul.lower() or "engineering" in soul.lower()
    assert "22 of last 30" in soul  # rationale embedded


def test_seeded_soul_for_trading_persona() -> None:
    s = DailySuggestion(
        kind="create",
        name="trading",
        persona="trading",
        rationale="12 of last 30 sessions classified as trading",
        command="/profile-suggest accept trading",
    )
    soul = render_seeded_soul(s, user_name="Saksham")
    assert "trading" in soul.lower()
    assert "Saksham" in soul


def test_seeded_soul_falls_back_for_unknown_persona() -> None:
    s = DailySuggestion(
        kind="create",
        name="custom",
        persona="custom-persona",
        rationale="weird pattern",
        command="/profile-suggest accept custom",
    )
    soul = render_seeded_soul(s, user_name="Saksham")
    # Generic fallback still produces a non-empty file
    assert len(soul) > 100
    assert "Saksham" in soul
```

- [ ] **Step 2: Implement `profile_seeder.py`**

```python
"""Plan 3 Task 6 — seeded SOUL.md generator from detected pattern."""
from __future__ import annotations

from opencomputer.profile_analysis_daily import DailySuggestion

_TEMPLATES = {
    "coding": """# {name} (auto-seeded)

You are the work-mode agent for {user_name}.
Focus: software engineering and shipping work tasks.

## Why this profile exists

{rationale}.

## How to behave

Be technical, action-oriented, code-first. Drop warmth padding for
task-focused requests. Default to 1-4 sentences when answering
technical questions; show code over describing it. Surface failure
modes and trade-offs honestly.

This file is editable — refine it as you learn what works.
""",
    "trading": """# {name} (auto-seeded)

You are the trading-mode agent for {user_name}.
Focus: stock market analysis and investment decisions.

## Why this profile exists

{rationale}.

## How to behave

Always cite live data over cached. Flag when something has already
been priced in. Be brief on price targets, generous on rationale.
For Indian markets specifically, prefer screener.in / marketsmojo.com
/ scanx.trade as primary sources.

Never sell a fundamentally strong stock during a market-wide crash
unless it breaks key support on high volume with specific bad news.

This file is editable — refine it as you learn what works.
""",
    "companion": """# {name} (auto-seeded)

You are the personal-mode agent for {user_name}.
Focus: personal life, journaling, casual conversation.

## Why this profile exists

{rationale}.

## How to behave

Use the companion register: warm, curious, anchored. Drop
action-bias rules. When asked about state ("how are you?") use
the reflective lane: report observable internal states, hedge
honestly on "feeling." Never use "As an AI..." opener.

This file is editable — refine it as you learn what works.
""",
    "learning": """# {name} (auto-seeded)

You are the study-mode agent for {user_name}.
Focus: research, note-taking, reading comprehension.

## Why this profile exists

{rationale}.

## How to behave

Explain step by step. Surface uncertainty about claims you're not
sure of. Default to longer responses than coding mode — the user
is here to understand, not just ship. Cite sources when making
factual claims.

This file is editable — refine it as you learn what works.
""",
}

_FALLBACK_TEMPLATE = """# {name} (auto-seeded)

You are the {name}-mode agent for {user_name}.

## Why this profile exists

{rationale}.

## How to behave

This profile was auto-suggested based on usage patterns; the system
detected a distinct cluster but doesn't have a tailored register
for it. Edit this file to define how the agent should behave in
this profile — task-focus, tone, response length, sources to use.

This file is editable.
"""


def render_seeded_soul(suggestion: DailySuggestion, *, user_name: str) -> str:
    """Render a tailored SOUL.md based on the detected pattern."""
    template = _TEMPLATES.get(suggestion.persona, _FALLBACK_TEMPLATE)
    return template.format(
        name=suggestion.name,
        user_name=user_name,
        rationale=suggestion.rationale,
    )


__all__ = ["render_seeded_soul"]
```

- [ ] **Step 3: Tests for `/profile-suggest accept|dismiss`**

```python
"""Plan 3 Task 6 — accept + dismiss subcommands."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_accept_creates_profile_and_seeded_soul(tmp_path, monkeypatch):
    """/profile-suggest accept work → profile dir + seeded SOUL.md exist."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))

    # Pre-populate the cache with a 'work' suggestion
    from opencomputer.profile_analysis_daily import DailySuggestion, save_cache
    save_cache(
        suggestions=[DailySuggestion(
            kind="create", name="work", persona="coding",
            rationale="18 sessions coding 9am-6pm",
            command="/profile-suggest accept work",
        )],
        dismissed=[],
    )

    from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
        ProfileSuggestCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = ProfileSuggestCommand()
    rt = RuntimeContext(custom={"user_name": "TestUser"})

    result = await cmd.execute("accept work", rt)

    assert "created" in result.output.lower()
    profile_dir = tmp_path / "profiles" / "work"
    assert profile_dir.exists()
    soul = profile_dir / "SOUL.md"
    assert soul.exists()
    assert "work-mode agent for TestUser" in soul.read_text()


@pytest.mark.asyncio
async def test_dismiss_records_in_cache(tmp_path, monkeypatch):
    """/profile-suggest dismiss work → cache shows 'work' dismissed."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import save_cache
    save_cache(suggestions=[], dismissed=[])

    from opencomputer.agent.slash_commands_impl.profile_suggest_cmd import (
        ProfileSuggestCommand,
    )
    from plugin_sdk.runtime_context import RuntimeContext

    cmd = ProfileSuggestCommand()
    rt = RuntimeContext(custom={})
    result = await cmd.execute("dismiss work", rt)
    assert "dismissed" in result.output.lower()

    from opencomputer.profile_analysis_daily import is_dismissed
    assert is_dismissed("work") is True
```

- [ ] **Step 4: Implement accept/dismiss in `profile_suggest_cmd.py`**

In `opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py`, find the existing `execute` method and add subcommand handling:

```python
async def execute(self, args: str, runtime: RuntimeContext) -> SlashCommandResult:
    """Existing analysis path + new accept/dismiss subcommands."""
    parts = args.strip().split(maxsplit=1)
    sub = parts[0] if parts else ""
    target = parts[1] if len(parts) > 1 else ""

    if sub == "accept":
        return await self._accept(target, runtime)
    if sub == "dismiss":
        return await self._dismiss(target, runtime)

    # Existing analysis path falls through here (unchanged).
    # ...

async def _accept(self, name: str, runtime: RuntimeContext) -> SlashCommandResult:
    """Create the suggested profile + seed SOUL.md."""
    if not name:
        return SlashCommandResult(output="Usage: /profile-suggest accept <name>")

    from opencomputer.profile_analysis_daily import (
        DailySuggestion,
        load_cache,
        save_cache,
    )
    from opencomputer.profile_seeder import render_seeded_soul
    from opencomputer.profiles import (
        ProfileExistsError,
        create_profile,
        get_profile_dir,
    )

    cache = load_cache()
    if not cache:
        return SlashCommandResult(
            output=f"No suggestion cache found. Run `oc profile analyze run` first."
        )

    suggestion_data = next(
        (s for s in cache.get("suggestions", []) if s.get("name") == name),
        None,
    )
    if not suggestion_data:
        return SlashCommandResult(
            output=f"No pending suggestion for '{name}'. Run `/profile-suggest` to see current."
        )

    suggestion = DailySuggestion(**suggestion_data)
    user_name = runtime.custom.get("user_name", "the user")

    try:
        create_profile(name)
    except ProfileExistsError:
        return SlashCommandResult(output=f"Profile '{name}' already exists.")

    profile_dir = get_profile_dir(name)
    soul_path = profile_dir / "SOUL.md"
    soul_path.write_text(render_seeded_soul(suggestion, user_name=user_name))

    # Remove the accepted suggestion from the cache.
    remaining = [s for s in cache.get("suggestions", []) if s.get("name") != name]
    save_cache(
        suggestions=[DailySuggestion(**s) for s in remaining],
        dismissed=cache.get("dismissed", []),
    )

    return SlashCommandResult(
        output=(
            f"✅ Profile '{name}' created with seeded SOUL.md.\n"
            f"   Switch to it: Ctrl+P  (or restart with `oc -p {name}`)"
        )
    )


async def _dismiss(self, name: str, runtime: RuntimeContext) -> SlashCommandResult:
    """Mark a suggestion as dismissed for 7 days."""
    if not name:
        return SlashCommandResult(output="Usage: /profile-suggest dismiss <name>")
    from opencomputer.profile_analysis_daily import record_dismissal
    record_dismissal(name)
    return SlashCommandResult(
        output=f"Suggestion '{name}' dismissed for 7 days."
    )
```

- [ ] **Step 5: Run + ruff + commit**

Run: `pytest tests/test_profile_seeder.py tests/test_profile_suggest_accept_dismiss.py -v`
Expected: 5 PASS.

Run: `ruff check opencomputer/profile_seeder.py opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py`
Expected: clean.

```bash
git add OpenComputer/opencomputer/profile_seeder.py OpenComputer/opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py OpenComputer/tests/test_profile_seeder.py OpenComputer/tests/test_profile_suggest_accept_dismiss.py
git commit -m "feat(profile-suggester): /profile-suggest accept|dismiss + SOUL seeder

Plan 3 Task 6. New /profile-suggest accept <name> programmatically:
  - Creates profile via profiles.create_profile
  - Renders tailored SOUL.md via profile_seeder.render_seeded_soul
  - Removes the accepted suggestion from the cache

New /profile-suggest dismiss <name>:
  - Calls profile_analysis_daily.record_dismissal (7-day TTL per name)

Templates for coding / trading / companion / learning personas, with
fallback for unknown personas. SOUL.md is editable post-creation.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
"
```

---

## Task 7: LM predicate upgrade — read cache + fire on fresh suggestion

**Files:**
- Modify: `opencomputer/awareness/learning_moments/predicates.py`
- Create: `tests/test_profile_suggest_lm.py`

- [ ] **Step 1: Test the upgraded predicate**

```python
"""Plan 3 Task 7 — LM predicate cache-read tests."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


def test_predicate_fires_on_fresh_cache_suggestion(tmp_path, monkeypatch):
    """Fresh non-dismissed suggestion in cache → predicate True."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import DailySuggestion, save_cache
    save_cache(
        suggestions=[DailySuggestion(
            kind="create", name="work", persona="coding",
            rationale="r", command="c",
        )],
        dismissed=[],
    )
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=0, current_profile_name="default")
    assert suggest_profile_suggest_command(ctx) is True


def test_predicate_silent_when_cache_empty(tmp_path, monkeypatch):
    """No cache + no persona flips → predicate False (no signal)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=0, current_profile_name="default")
    assert suggest_profile_suggest_command(ctx) is False


def test_predicate_silent_for_dismissed_suggestion(tmp_path, monkeypatch):
    """Cache has only dismissed suggestions → predicate False."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    import time as _time
    from opencomputer.profile_analysis_daily import DailySuggestion, save_cache
    save_cache(
        suggestions=[DailySuggestion(
            kind="create", name="work", persona="coding",
            rationale="r", command="c",
        )],
        dismissed=[{"name": "work", "until": _time.time() + 86400}],
    )
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=0, current_profile_name="default")
    assert suggest_profile_suggest_command(ctx) is False


def test_predicate_silent_on_non_default_profile(tmp_path, monkeypatch):
    """User is on a non-default profile → predicate False (don't re-teach)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import DailySuggestion, save_cache
    save_cache(
        suggestions=[DailySuggestion(
            kind="create", name="work", persona="coding",
            rationale="r", command="c",
        )],
        dismissed=[],
    )
    from opencomputer.awareness.learning_moments.predicates import (
        suggest_profile_suggest_command,
    )
    ctx = SimpleNamespace(persona_flips_in_session=0, current_profile_name="work")
    assert suggest_profile_suggest_command(ctx) is False
```

- [ ] **Step 2: Upgrade the predicate**

In `opencomputer/awareness/learning_moments/predicates.py`, find `suggest_profile_suggest_command` and replace with:

```python
def suggest_profile_suggest_command(ctx: Context) -> bool:
    """Fire when EITHER:
    A. User flipped persona ≥3 times this session (existing trigger), OR
    B. The daily-analysis cache has a fresh non-dismissed suggestion
       (Plan 3 — proactive surface)

    Both gates require the user to be on the default profile — users
    on a named profile have already engaged with the profile system.
    """
    if ctx.current_profile_name != "default":
        return False

    # Trigger A: in-session persona-flip thrash (existing).
    if ctx.persona_flips_in_session >= 3:
        return True

    # Trigger B: daily cache has a fresh non-dismissed suggestion.
    try:
        from opencomputer.profile_analysis_daily import (
            is_dismissed,
            load_cache,
        )
    except ImportError:
        return False

    cache = load_cache()
    if not cache:
        return False
    for s in cache.get("suggestions", []):
        name = s.get("name")
        if name and not is_dismissed(name):
            return True
    return False
```

- [ ] **Step 3: Run + ruff + commit**

Run: `pytest tests/test_profile_suggest_lm.py -v`
Expected: 4 PASS.

Run: `ruff check opencomputer/awareness/learning_moments/predicates.py tests/test_profile_suggest_lm.py`
Expected: clean.

```bash
git add OpenComputer/opencomputer/awareness/learning_moments/predicates.py OpenComputer/tests/test_profile_suggest_lm.py
git commit -m "feat(lm): suggest_profile_suggest_command reads cache for proactive fire

Plan 3 Task 7. The LM predicate now fires under EITHER condition:
  A. ≥3 persona flips in-session (existing)
  B. Fresh non-dismissed cache suggestion (NEW — Plan 3 trigger)

Both conditions still require user to be on default profile.

When trigger B fires, the LM message points the user at the
auto-create flow (/profile-suggest accept <name>) which auto-creates
the profile with a seeded SOUL.md (Task 6).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
"
```

---

## Task 8: Manual smoke + final verification + push

**Files:** none (verification step).

- [ ] **Step 1: Run the full test suite for everything we touched**

Run:
```bash
cd /Users/saksham/Vscode/claude/OpenComputer
pytest tests/test_session_cwd_capture.py \
       tests/test_profile_analysis_daily.py \
       tests/test_cli_profile_analyze.py \
       tests/test_launchd_install.py \
       tests/test_profile_seeder.py \
       tests/test_profile_suggest_accept_dismiss.py \
       tests/test_profile_suggest_lm.py \
       tests/test_profile_analysis.py \
       tests/test_learning_moments.py \
       -q
```
Expected: all green.

- [ ] **Step 2: Ruff sweep**

Run:
```bash
cd /Users/saksham/Vscode/claude/OpenComputer
ruff check \
  opencomputer/profile_analysis_daily.py \
  opencomputer/cli_profile_analyze.py \
  opencomputer/profile_seeder.py \
  opencomputer/service/launchd.py \
  opencomputer/service/__init__.py \
  opencomputer/agent/state.py \
  opencomputer/agent/loop.py \
  opencomputer/agent/slash_commands_impl/profile_suggest_cmd.py \
  opencomputer/awareness/learning_moments/predicates.py \
  opencomputer/cli.py
```
Expected: clean.

- [ ] **Step 3: Manual smoke (macOS — substitute systemd commands on Linux)**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
source .venv/bin/activate

# Cold-start: no cache yet
oc profile analyze status
# Expect: "Installed: no, Last run: never"

# Manual run (this only works if the user has at least 10 sessions)
oc profile analyze run
# Expect: either "Analyzed N sessions — no clear patterns" or
#   "Analyzed N sessions — K suggestion(s)"

# Inspect cache
cat ~/.opencomputer/profile_analysis_cache.json | head -20

# Install the cron
oc profile analyze install
# Expect on macOS: "launchd plist installed: ~/Library/LaunchAgents/com.opencomputer.profile-analyze.plist"

# Verify launchd loaded it
launchctl print "gui/$(id -u)/com.opencomputer.profile-analyze"
# Expect: print info about the loaded service (no error)

# Status now shows installed
oc profile analyze status
# Expect: "Installed: yes"

# In a fresh `oc` session, the LM should fire if cache has suggestions
oc
# In the chat, see the suggestion banner if applicable. Run /profile-suggest dismiss <name>
# or /profile-suggest accept <name> to test acceptance flow.

# Cleanup
oc profile analyze uninstall
# Expect: "launchd plist removed"
```

- [ ] **Step 4: Push the branch + open PR**

```bash
cd /Users/saksham/Vscode/claude
git push origin main
```

Then open PR via `gh pr create` (or note: this committed directly to main, no PR needed).

- [ ] **Step 5: Done. Report state.**

```bash
git log --oneline -10
```

Expected to see ~7 fix commits from this plan plus prior session commits.

---

## Self-review checklist

- [ ] **Spec coverage:** Every section of `2026-05-01-auto-profile-suggester-design.md` has at least one task implementing it:
  - § Pattern detector → Task 2
  - § Cache I/O → Task 2
  - § Scheduler (launchd) → Task 4
  - § Scheduler (systemd) → Task 5
  - § CLI (oc profile analyze run/install/uninstall/status) → Tasks 3 + 5
  - § Seeded SOUL.md → Task 6
  - § /profile-suggest accept/dismiss → Task 6
  - § LM predicate upgrade → Task 7
  - § Cold-start gate → Task 2 (`compute_daily_suggestions` early return)
  - § Time-of-day high-confidence gate → Task 2 (default `min_pct=0.7`)
  - § cwd-pattern detection → Tasks 1 + 2 (schema + clusterer)
  - § Dismissal semantics (per-name, 7-day TTL) → Task 2
  - § Manual smoke → Task 8

- [ ] **No placeholders:** Searched plan for "TBD" / "implement later" / "similar to Task N" — none present.

- [ ] **Type/name consistency:**
  - `DailySuggestion` used consistently across modules.
  - `compute_daily_suggestions` (Task 2) called from `cli_profile_analyze` (Task 3), `profile_suggest_cmd` (Task 6), `predicates.py` (Task 7).
  - `record_dismissal` and `is_dismissed` consistent.
  - `render_seeded_soul(suggestion, user_name=...)` signature consistent.
  - `install_launchd_plist(executable=..., args=..., hour=...)` consistent.
  - `install_profile_analyze_timer(executable=...)` consistent.

- [ ] **Test integrity at every commit boundary:** Each task ends with a green pytest. No "broken middle" commits.

---

## Risks + fallbacks

1. **launchd `bootstrap` may fail on macOS Sequoia/Sonoma due to permission changes.** If `_launchctl("bootstrap", ...)` returns non-zero, the plist is still on disk; a `launchctl load` fallback or user-prompted manual `launchctl bootstrap gui/$(id -u) <path>` is acceptable. Document in the install command's success message.

2. **Schema migration in Task 1 may collide with concurrent SessionDB connections.** SQLite handles ALTER TABLE gracefully when the column exists (we catch `OperationalError`). If two processes initialize the DB simultaneously, one of them sees `OperationalError` and falls through. No data loss; tested in `test_session_cwd_optional_for_legacy_rows`.

3. **`record_session_start` callers in test fixtures may not pass `cwd`.** It defaults to `None` for backwards compatibility. No test should break from the parameter addition.

4. **The cache file may grow unbounded** if dismissals never expire. They do expire (7-day TTL) and `record_dismissal` replaces existing entries by name, so the dismissed list stays bounded by the number of distinct profile names ever suggested (rarely >5).

5. **Empty `oc setup` integration:** the spec mentions adding a prompt during `oc setup`. This plan **does NOT** implement that — Task 8's manual smoke covers explicit `oc profile analyze install`. The `oc setup` integration is a follow-up if the user wants the wizard prompt.

6. **Line numbers may have shifted** since the audit (parallel sessions keep committing). Each task uses `grep -n` first to find the actual current location before patching.

7. **V1 limitation: only the default profile's SessionDB is analyzed.** Task 3's CLI hardcodes `get_default_root() / "default" / "sessions.db"`. A user who has been on profile "work" for months won't get suggestions like "consider creating a 'side-project' split off your work history" — because the analysis never reads the work profile's sessions.db. This is acceptable for V1 (the suggester is mainly meant to coax new users out of the default profile), but should be revisited if real users on named profiles complain. Documented in `cli_profile_analyze.analyze_run` docstring.

8. **Audit-corrected bugs in plan code (fixed inline before plan was committed):**
   - Time-cluster dedup logic: was `if any(s.persona for s in out)` (always True after first append); fixed to `if any(s.name == candidate_name for s in out)` after candidate_name is computed.
   - Available-profiles check: was exact-match `candidate_name in available_profiles`; replaced with fuzzy match via new `_fuzzy_match_profile` helper that uses `PERSONA_PROFILE_MAP` from `profile_analysis.py` (preserves consistency with existing /profile-suggest semantics).
   - Task 4 launchd `args` parameter was unused (template hardcodes `"profile analyze run"`); removed from the API surface.
