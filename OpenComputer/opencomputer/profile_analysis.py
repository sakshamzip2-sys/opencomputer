"""Profile usage analysis — single source of truth for profile suggestions.

Pure-function module. Re-classifies recent sessions on demand using the
existing persona classifier (no schema migration required). Reused by:

1. ``/profile-suggest`` slash command (primary surface)
2. Future surfaces (oc doctor check, empty-state hints — deferred)

The persona classifier outputs (PR #271) are NOT persisted on the
``sessions`` table today. This module re-derives them by reading the
first few user messages of each recent session and running the classifier
synchronously. Cost: ~150ms for a 30-session lookback (classifier <5ms
each). Acceptable for a slash command; deferred for hot-path use.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from opencomputer.agent.state import SessionDB
from opencomputer.awareness.personas.classifier import (
    ClassificationContext,
    classify,
)

# Persona name → list of profile-name hints. Profile name fuzzy-matches
# a persona if any hint is a substring of the profile name (or vice-versa).
PERSONA_PROFILE_MAP: dict[str, tuple[str, ...]] = {
    "trading":   ("stock", "trade", "trading", "finance", "market", "invest"),
    "coding":    ("work", "code", "dev", "project", "engineering"),
    "companion": ("personal", "life", "journal", "diary"),
    "relaxed":   ("chill", "casual", "leisure"),
    "learning":  ("study", "research", "learning", "notes"),
}

# A persona must appear in ≥3 of the last LOOKBACK sessions before it
# qualifies for a suggestion. With 30-session lookback that's 10% — high
# enough to skip drive-by classifications, low enough to fire on real signal.
_MIN_SESSIONS_FOR_SUGGESTION = 3
_LOOKBACK_SESSIONS = 30

# Classifier confidence below this is treated as no-signal (bucketed as
# "default") rather than the classifier's literal fallback persona.
# Without this gate, every silent-content session collapses to "companion"
# (the classifier's default) and we'd spuriously suggest creating a
# personal/journal profile for every user.
_CONFIDENCE_GATE = 0.5


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
    """Fuzzy match: profile name overlaps with any persona hint via substring."""
    profile_lower = profile_name.lower()
    candidates = PERSONA_PROFILE_MAP.get(persona, ())
    return any(
        c in profile_lower or profile_lower in c for c in candidates
    )


def _classify_session_persona(db: SessionDB, session_id: str) -> str:
    """Re-classify a session's persona from its first few user messages.

    Returns the persona_id if confidence ≥ ``_CONFIDENCE_GATE``, else
    "default" (treats no-signal as no-signal, not as the classifier's
    fallback companion bucket).
    """
    try:
        messages = db.get_messages(session_id)
    except Exception:  # noqa: BLE001 — defensive: defer rather than crash
        return "default"
    user_msgs = tuple(
        m.content for m in messages
        if m.role == "user" and isinstance(m.content, str) and m.content
    )[:3]
    if not user_msgs:
        return "default"
    ctx = ClassificationContext(
        foreground_app="",            # historical foreground unknown
        time_of_day_hour=12,          # neutral hour bucket
        recent_file_paths=(),         # historical file paths unknown
        last_messages=user_msgs,
    )
    try:
        result = classify(ctx)
    except Exception:  # noqa: BLE001
        return "default"
    if result.confidence < _CONFIDENCE_GATE:
        return "default"
    return result.persona_id


def compute_profile_suggestions(
    *,
    home: Path,
    db: SessionDB,
    current_profile: str,
    available_profiles: tuple[str, ...],
) -> ProfileReport:
    """Analyze recent sessions + available profiles, return ProfileReport.

    Reads last ``_LOOKBACK_SESSIONS`` sessions, re-classifies each to a
    persona, and emits ``create``/``switch``/``stay`` suggestions for any
    persona that appears in ≥ ``_MIN_SESSIONS_FOR_SUGGESTION`` sessions.
    """
    rows = db.list_sessions(limit=_LOOKBACK_SESSIONS)
    persona_counter: Counter[str] = Counter()
    for row in rows:
        sid = row.get("id") or row.get("session_id") or ""
        if not sid:
            continue
        persona = _classify_session_persona(db, sid)
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
        matching = next(
            (
                p for p in available_profiles
                if _persona_matches_profile(persona, p)
            ),
            None,
        )
        if matching is None:
            suggestions.append(ProfileSuggestion(
                kind="create",
                profile_name=None,
                persona=persona,
                rationale=(
                    f"{count} of last {len(rows)} sessions were "
                    f"{persona}-mode and no specialized profile matches"
                ),
                command="oc profile create <name> && oc -p <name>",
            ))
        elif matching != current_profile:
            suggestions.append(ProfileSuggestion(
                kind="switch",
                profile_name=matching,
                persona=persona,
                rationale=(
                    f"{count} {persona}-mode sessions but you're in "
                    f"'{current_profile}' — '{matching}' profile exists"
                ),
                command=f"oc -p {matching}",
            ))
        else:
            suggestions.append(ProfileSuggestion(
                kind="stay",
                profile_name=matching,
                persona=persona,
                rationale=(
                    f"{count} {persona}-mode sessions, you're already "
                    f"in '{matching}'"
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


def render_report(report: ProfileReport) -> str:
    """Render the report as a plain-text block for slash command output."""
    lines = [
        "─" * 60,
        f"Active profile: {report.current_profile}",
        "",
    ]
    if report.sessions_analyzed == 0:
        lines.append(
            "No session history yet — use OC for a few sessions and try again.",
        )
        lines.append("─" * 60)
        return "\n".join(lines)

    lines.append(
        f"Recent persona breakdown (last {report.sessions_analyzed} sessions):",
    )
    for entry in report.persona_breakdown:
        pct = (entry.count / max(report.sessions_analyzed, 1)) * 100
        lines.append(
            f"  {entry.persona_id:12} {entry.count:3} sessions  ({pct:.0f}%)",
        )
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


__all__ = [
    "compute_profile_suggestions",
    "render_report",
    "PersonaSessionCount",
    "ProfileSuggestion",
    "ProfileReport",
    "PERSONA_PROFILE_MAP",
]
