"""User-learnable persona priors.

Records every ``/persona-mode <id>`` override + the context that
preceded it (foreground app, hour-of-day, last user message hash).
Subsequent classifications get a Bayesian-style boost for the
persona the user previously chose in similar context.

Storage: ``~/.opencomputer/<profile>/persona_priors.json``.
Schema:
    {
        "version": 1,
        "overrides": [
            {
                "persona_id": "trading",
                "foreground_app": "Chrome",
                "hour": 14,
                "msg_hash": "ab12cd34",
                "ts": 1234567890.0
            }
        ]
    }

Cap: 200 most-recent records (oldest dropped).
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencomputer.awareness.personas.classifier import (
        ClassificationContext,
    )
    from opencomputer.awareness.personas.classifier_v2 import _Signal

_FILE_NAME = "persona_priors.json"
_MAX_RECORDS = 200
_PRIOR_BOOST = 0.5  # weight contributed when a context match hits


def _priors_path(profile_home: str | Path) -> Path:
    return Path(profile_home) / _FILE_NAME


def _hash_msg(msg: str) -> str:
    """Short stable hash used as a coarse content signature."""
    return hashlib.sha256(msg.encode("utf-8")).hexdigest()[:8]


def _load(profile_home: str | Path) -> list[dict]:
    """Return the override list. Empty on first read or any error."""
    path = _priors_path(profile_home)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    overrides = data.get("overrides", [])
    if not isinstance(overrides, list):
        return []
    return overrides


def _save(profile_home: str | Path, overrides: list[dict]) -> None:
    path = _priors_path(profile_home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    body = {"version": 1, "overrides": overrides[-_MAX_RECORDS:]}
    try:
        path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    except OSError:
        pass


def record_override(
    *,
    profile_home: str | Path,
    persona_id: str,
    foreground_app: str = "",
    hour: int = 12,
    last_msg: str = "",
) -> None:
    """Persist a ``/persona-mode <id>`` override + its context features.

    Called from ``persona_mode_cmd.py``. Best-effort: any IO error
    silently swallowed so a misbehaving FS never breaks the slash
    command.
    """
    if not profile_home:
        return
    overrides = _load(profile_home)
    overrides.append({
        "persona_id": persona_id,
        "foreground_app": (foreground_app or "").lower(),
        "hour": int(hour) if hour else 12,
        "msg_hash": _hash_msg(last_msg) if last_msg else "",
        "ts": time.time(),
    })
    _save(profile_home, overrides)


def score_priors(ctx: ClassificationContext) -> list[_Signal]:
    """Return weighted Signals for personas the user has historically
    chosen in similar context. Empty when no priors recorded yet.
    """
    from opencomputer.awareness.personas.classifier_v2 import _Signal

    profile_home = getattr(ctx, "profile_home", "") or ""
    if not profile_home:
        return []

    overrides = _load(profile_home)
    if not overrides:
        return []

    persona_scores: dict[str, float] = defaultdict(float)
    persona_evidence: dict[str, str] = {}
    fg = (ctx.foreground_app or "").lower()
    hour_now = ctx.time_of_day_hour

    for rec in overrides:
        persona = rec.get("persona_id", "")
        if not persona:
            continue
        # Score similarity: foreground match is the strongest feature.
        sim = 0.0
        if fg and fg == rec.get("foreground_app", ""):
            sim += 0.7
        rec_hour = rec.get("hour", 12)
        if abs(rec_hour - hour_now) <= 2:
            sim += 0.3
        if sim < 0.3:
            continue

        contribution = _PRIOR_BOOST * sim
        persona_scores[persona] += contribution
        if persona not in persona_evidence:
            persona_evidence[persona] = (
                f"prior override (app={rec.get('foreground_app', '?')}, "
                f"hour={rec_hour})"
            )

    out: list[_Signal] = []
    for persona, score in persona_scores.items():
        out.append(_Signal(
            persona_id=persona,
            weight=min(0.6, score),  # cap so priors can't fully override
            reason=persona_evidence.get(persona, "user prior"),
        ))
    return out


__all__ = ["record_override", "score_priors"]
