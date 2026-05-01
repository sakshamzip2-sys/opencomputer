"""Plan 3 — pattern detector + cache + dismissal tests."""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path
from types import SimpleNamespace


def _ts(hour: int) -> float:
    """Epoch timestamp at a given hour-of-day (today, in local time)."""
    today = dt.date.today()
    naive = dt.datetime(today.year, today.month, today.day, hour=hour)
    return naive.timestamp()


def _session(idx: int, *, hour: int, persona: str, cwd: str) -> SimpleNamespace:
    """Fake session row — namespace shape (also dict-shape works)."""
    return SimpleNamespace(
        id=f"sid-{idx}",
        started_at=_ts(hour),
        cwd=cwd,
        persona=persona,
    )


# ─── time-of-day clustering ───────────────────────────────────────────


def test_bin_by_time_of_day_clusters_when_over_threshold() -> None:
    from opencomputer.profile_analysis_daily import bin_by_time_of_day
    timestamps = [
        *(_ts(h) for h in [9, 10, 10, 11, 11, 11, 9, 12, 10, 9,
                            11, 12, 9, 10, 11, 9, 12, 10, 11, 9, 10, 12]),
        *(_ts(h) for h in [3, 5, 7, 14, 16, 19, 21, 23]),
    ]
    clusters = bin_by_time_of_day(timestamps, min_pct=0.7, band_hours=4)
    assert len(clusters) >= 1
    assert clusters[0].band_start_hour == 9
    assert clusters[0].session_count >= 21


def test_bin_by_time_of_day_no_cluster_below_threshold() -> None:
    from opencomputer.profile_analysis_daily import bin_by_time_of_day
    timestamps = [_ts(h) for h in range(24)] * 2
    clusters = bin_by_time_of_day(timestamps, min_pct=0.7, band_hours=4)
    assert clusters == []


# ─── cwd clustering ────────────────────────────────────────────────────


def test_bin_by_cwd_clusters_subtree() -> None:
    from opencomputer.profile_analysis_daily import bin_by_cwd
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


def test_bin_by_cwd_excludes_filesystem_root() -> None:
    """Sessions all started in / shouldn't suggest a / profile."""
    from opencomputer.profile_analysis_daily import bin_by_cwd
    cwds = ["/" for _ in range(10)]
    clusters = bin_by_cwd(cwds, min_pct=0.4)
    assert clusters == []


# ─── compute_daily_suggestions integration ────────────────────────────


def test_compute_daily_suggestions_skips_when_under_min_sessions() -> None:
    """Cold-start: fewer than 10 sessions → no suggestions fired."""
    from opencomputer.profile_analysis_daily import compute_daily_suggestions
    sessions = [
        _session(i, hour=10, persona="coding", cwd="/Users/x/Vscode/work")
        for i in range(5)
    ]
    suggestions = compute_daily_suggestions(sessions, available_profiles=("default",))
    assert suggestions == []


def test_compute_daily_suggestions_fires_clear_pattern() -> None:
    """30 sessions in a 9-12 morning band, all coding → suggest 'work' profile."""
    from opencomputer.profile_analysis_daily import compute_daily_suggestions
    sessions = [
        _session(i, hour=10, persona="coding", cwd="/Users/x/Vscode/work")
        for i in range(30)
    ]
    suggestions = compute_daily_suggestions(sessions, available_profiles=("default",))
    assert len(suggestions) >= 1
    assert any(s.kind == "create" for s in suggestions)


def test_compute_daily_suggestions_skips_when_user_has_fuzzy_profile() -> None:
    """User has a 'stocks' profile — trading suggestion shouldn't fire."""
    from opencomputer.profile_analysis_daily import compute_daily_suggestions
    sessions = [
        _session(i, hour=15, persona="trading", cwd="/Users/x/desk")
        for i in range(15)
    ]
    suggestions = compute_daily_suggestions(
        sessions, available_profiles=("default", "stocks"),
    )
    # Either no suggestions, or none with persona='trading'
    assert all(s.persona != "trading" for s in suggestions)


def test_compute_daily_suggestions_dict_shape() -> None:
    """Sessions can be dicts (matches list_sessions return type)."""
    from opencomputer.profile_analysis_daily import compute_daily_suggestions
    sessions = [
        {"id": f"sid-{i}", "started_at": _ts(10), "cwd": "/Users/x/Vscode/work",
         "persona": "coding"}
        for i in range(15)
    ]
    suggestions = compute_daily_suggestions(sessions, available_profiles=("default",))
    assert len(suggestions) >= 1


# ─── cache I/O ─────────────────────────────────────────────────────────


def test_cache_round_trip(tmp_path: Path, monkeypatch) -> None:
    """save_cache writes JSON; load_cache reads it back."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import (
        DailySuggestion,
        load_cache,
        save_cache,
    )
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


def test_load_cache_returns_none_on_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import load_cache
    assert load_cache() is None


def test_load_cache_returns_none_on_corrupt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    (tmp_path / "profile_analysis_cache.json").write_text("not json {{{")
    from opencomputer.profile_analysis_daily import load_cache
    assert load_cache() is None


# ─── dismissal ─────────────────────────────────────────────────────────


def test_dismissal_blocks_for_7_days(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import (
        is_dismissed,
        record_dismissal,
        save_cache,
    )
    save_cache(suggestions=[], dismissed=[])
    record_dismissal("work")
    assert is_dismissed("work") is True


def test_dismissal_expires_after_7_days(tmp_path: Path, monkeypatch) -> None:
    """After 7 days, is_dismissed returns False (suggestion can re-fire)."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    cache = {
        "last_run": time.time(),
        "suggestions": [],
        "dismissed": [
            {"name": "work", "until": time.time() - 1.0},
        ],
    }
    cache_path = tmp_path / "profile_analysis_cache.json"
    cache_path.write_text(json.dumps(cache))
    from opencomputer.profile_analysis_daily import is_dismissed
    assert is_dismissed("work") is False


def test_dismissal_replaces_existing_entry(tmp_path: Path, monkeypatch) -> None:
    """Re-dismissing 'work' shouldn't accumulate duplicate entries."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    from opencomputer.profile_analysis_daily import (
        load_cache,
        record_dismissal,
        save_cache,
    )
    save_cache(suggestions=[], dismissed=[])
    record_dismissal("work")
    record_dismissal("work")
    cache = load_cache()
    assert cache is not None
    work_entries = [d for d in cache["dismissed"] if d["name"] == "work"]
    assert len(work_entries) == 1
