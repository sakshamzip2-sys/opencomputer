"""Plan 3 — daily background pattern detection for profile suggestions.

Extends profile_analysis.py with two NEW signals (time-of-day clusters,
cwd clusters) and adds disk-cache I/O for proactive surfacing via the
LM predicate.

Pattern-strength gates (load-bearing — addresses brittleness concerns):
  - Cold-start: <10 sessions → no suggestions.
  - Time-of-day: ≥70% of sessions in a 4-hour band over 10+ sessions.
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
    """Find time-of-day bands containing >= ``min_pct`` of sessions.

    Walks all candidate band starts (24 of them, hour 0..23). For each
    qualifying band, marks the hours within the band as seen so adjacent
    overlapping shifts of the same cluster are not double-counted.
    """
    ts_list = list(timestamps)
    if not ts_list:
        return []
    hours = [_dt.datetime.fromtimestamp(t).hour for t in ts_list]
    n = len(hours)
    out: list[TimeCluster] = []
    seen_starts: set[int] = set()
    for start in range(24):
        if start in seen_starts:
            continue
        end = (start + band_hours) % 24
        if end > start:
            count = sum(1 for h in hours if start <= h < end)
        else:
            count = sum(1 for h in hours if h >= start or h < end)
        pct = count / n
        if pct >= min_pct:
            out.append(TimeCluster(
                band_start_hour=start,
                band_end_hour=end,
                session_count=count,
                pct=pct,
            ))
            for adj in range(start, start + band_hours):
                seen_starts.add(adj % 24)
    return out


def bin_by_cwd(
    cwds: Iterable[str | None],
    *,
    min_pct: float = 0.4,
) -> list[CwdCluster]:
    """Find cwd subtrees containing >= ``min_pct`` of sessions.

    Counts at each ancestor directory level. The deepest ancestor that
    still passes the threshold is returned (most specific cluster).
    Excludes filesystem root and the user's home directory to avoid
    suggesting profiles for "you live in your home directory."
    """
    paths = [Path(c) for c in cwds if c]
    if not paths:
        return []
    n = len(paths)
    counts: Counter[str] = Counter()
    for p in paths:
        for ancestor in (p, *p.parents):
            counts[str(ancestor)] += 1
    candidates = [
        (path, count, count / n)
        for path, count in counts.items()
        if count / n >= min_pct
        and path not in ("/", str(Path.home()))
    ]
    if not candidates:
        return []
    # Deepest passing ancestor wins (most specific cluster).
    candidates.sort(key=lambda x: -len(Path(x[0]).parts))
    deepest_path, deepest_count, deepest_pct = candidates[0]
    return [CwdCluster(path=deepest_path, session_count=deepest_count, pct=deepest_pct)]


def compute_daily_suggestions(
    sessions,
    *,
    available_profiles: tuple[str, ...],
) -> list[DailySuggestion]:
    """Produce suggestions from recent sessions. Empty list if cold-start.

    ``sessions`` is an iterable of session-like objects (dict or namespace)
    with the keys/attrs: ``started_at`` (float epoch), ``cwd`` (str|None),
    ``persona`` (str — the persona id classified for that session, may be
    set externally before passing in).
    """
    rows = list(sessions)
    if len(rows) < _MIN_SESSIONS_FOR_ANALYSIS:
        return []

    out: list[DailySuggestion] = []

    def _attr(row, key, default=None):
        """Read either dict[key] or attr access — supports both shapes."""
        if isinstance(row, dict):
            return row.get(key, default)
        return getattr(row, key, default)

    # Persona-cluster signal.
    persona_counts = Counter(
        _attr(r, "persona") for r in rows
        if _attr(r, "persona") and _attr(r, "persona") != "default"
    )
    for persona, count in persona_counts.items():
        if count < 3:
            continue
        # Skip if user already has a fuzzy-matching profile.
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
    timestamps = [_attr(r, "started_at", 0.0) for r in rows]
    time_clusters = bin_by_time_of_day(timestamps, min_pct=0.7, band_hours=4)
    for tc in time_clusters:
        in_band = [
            _attr(r, "persona") for r in rows
            if _attr(r, "persona")
            and _hour_in_band(_attr(r, "started_at", 0.0),
                              tc.band_start_hour, tc.band_end_hour)
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
    cwds = [_attr(r, "cwd") for r in rows]
    cwd_clusters = bin_by_cwd(cwds, min_pct=0.4)
    for cc in cwd_clusters:
        candidate_name = _cwd_to_profile_name(cc.path)
        if candidate_name in available_profiles:
            continue
        if any(s.name == candidate_name for s in out):
            continue
        out.append(DailySuggestion(
            kind="create",
            name=candidate_name,
            persona="coding",
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

    Reuses PERSONA_PROFILE_MAP from profile_analysis.py for consistency
    with the existing /profile-suggest semantics.
    """
    from opencomputer.profile_analysis import PERSONA_PROFILE_MAP
    profile_lower = profile_name.lower()
    candidates = PERSONA_PROFILE_MAP.get(persona, ())
    return any(c in profile_lower or profile_lower in c for c in candidates)


def _cwd_to_profile_name(cwd: str) -> str:
    """cwd path → profile name (last component, sanitized)."""
    name = Path(cwd).name.lower().replace(" ", "-")
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
