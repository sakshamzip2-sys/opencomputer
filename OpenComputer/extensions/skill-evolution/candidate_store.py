"""Candidate store — file-based persistence for proposed skills (T4).

Layout
------
::

    <profile_home>/skills/
    ├── code-review/SKILL.md          (active — bundled or accepted)
    ├── _proposed/                     (staging area for auto-generated)
    │   ├── auto-abc12345-port-cpp/
    │   │   ├── SKILL.md
    │   │   └── provenance.json
    │   └── auto-def67890-something/
    │       ├── SKILL.md
    │       └── provenance.json

The leading underscore on ``_proposed/`` makes it visibly different from
active skills and lets standard skill loaders ignore it (they conventionally
skip dirs that start with ``_`` or ``.``).

Atomicity
---------
All write paths use a stage-then-``os.rename`` pattern. ``os.rename`` of a
directory within the same filesystem is atomic on POSIX, so concurrent
``list_candidates`` callers either see the candidate fully or not at all —
there is no partial-write window.

* **add_candidate** — write into ``_proposed/.staging/<random>/`` then
  rename to the final name.
* **accept_candidate** — copy into ``<profile_home>/skills/.accepting-<name>/``
  then rename to ``<profile_home>/skills/<name>/``.
* **reject_candidate** — rename to ``_proposed/.deleted-<name>-<ts>/``
  before ``shutil.rmtree`` so a concurrent lister never sees a half-deleted
  directory.

Listings deliberately skip any directory whose name starts with ``.`` or
``_`` — that filter hides every staging/marker dir these primitives create.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .skill_extractor import ProposedSkill

_log = logging.getLogger("opencomputer.skill_evolution.candidate_store")


# ─── public dataclass ─────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CandidateMetadata:
    """Lightweight summary of one staged candidate."""

    name: str
    description: str
    confidence_score: int
    generated_at: float
    session_id: str
    age_days: float


# ─── path helpers ─────────────────────────────────────────────────────


def _proposed_dir(profile_home: Path) -> Path:
    return profile_home / "skills" / "_proposed"


def _active_dir(profile_home: Path) -> Path:
    return profile_home / "skills"


def _is_hidden(name: str) -> bool:
    """Skip dot-prefixed scratch dirs in listings.

    ``_proposed`` itself starts with ``_`` — but it's the parent we scan,
    not a sibling. Inside ``_proposed`` we only filter dot-prefixed entries
    (``.staging``, ``.deleted-*``).
    """
    return name.startswith(".")


# ─── add ──────────────────────────────────────────────────────────────


def _resolve_collision_name(proposed_root: Path, name: str) -> str:
    """``foo`` → ``foo`` if free, else ``foo-2``, ``foo-3``, ..."""
    if not (proposed_root / name).exists():
        return name
    n = 2
    while (proposed_root / f"{name}-{n}").exists():
        n += 1
    return f"{name}-{n}"


def add_candidate(profile_home: Path, proposal: ProposedSkill) -> Path:
    """Atomically write ``_proposed/<name>/`` containing the proposed skill.

    On collision (``_proposed/<name>/`` already exists), appends a numeric
    suffix: ``auto-X-y`` becomes ``auto-X-y-2``, etc. Returns the actual
    path written.

    Raises whatever the underlying write raises; on failure no partial
    state is visible to :func:`list_candidates` — the staging directory is
    cleaned up before propagation.
    """
    proposed_root = _proposed_dir(profile_home)
    proposed_root.mkdir(parents=True, exist_ok=True)

    final_name = _resolve_collision_name(proposed_root, proposal.name)
    final_path = proposed_root / final_name

    staging_root = proposed_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)

    # mkdtemp guarantees a unique scratch dir even under concurrent adds.
    staging_dir = Path(
        tempfile.mkdtemp(prefix=f"{final_name}-", dir=str(staging_root))
    )

    try:
        skill_path = staging_dir / "SKILL.md"
        provenance_path = staging_dir / "provenance.json"

        # Materialise the body. We don't re-derive frontmatter — the
        # extractor's body already includes it; we trust the dataclass.
        skill_path.write_text(proposal.body, encoding="utf-8")

        provenance_blob = dict(proposal.provenance)
        # If the extractor didn't stamp these, fall back to "now". They're
        # what listing/pruning sort and threshold against.
        provenance_blob.setdefault("generated_at", time.time())
        provenance_blob.setdefault("name", final_name)
        provenance_blob.setdefault("description", proposal.description)

        provenance_path.write_text(
            json.dumps(provenance_blob, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        # Atomic rename — within same filesystem this either fully succeeds
        # or leaves the destination untouched.
        os.rename(staging_dir, final_path)
    except Exception:
        # Clean up the staging dir so a subsequent list/prune doesn't trip
        # over orphaned scratch state. We do this best-effort — if cleanup
        # itself fails we still propagate the original error.
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    return final_path


# ─── list / get ───────────────────────────────────────────────────────


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _read_provenance(path: Path) -> dict | None:
    """Load ``provenance.json``; return ``None`` on missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _log.warning("skill-evolution: unreadable provenance at %s", path)
        return None


def list_candidates(profile_home: Path) -> list[CandidateMetadata]:
    """Scan ``_proposed/``; return metadata sorted newest first.

    Skips any sub-directory whose name starts with ``.`` (scratch /
    deletion markers). Skips entries whose ``provenance.json`` is missing
    or unreadable.
    """
    proposed_root = _proposed_dir(profile_home)
    if not proposed_root.exists():
        return []

    now = time.time()
    out: list[CandidateMetadata] = []

    for child in sorted(proposed_root.iterdir()):
        if not child.is_dir() or _is_hidden(child.name):
            continue
        prov_path = child / "provenance.json"
        if not prov_path.exists():
            continue
        prov = _read_provenance(prov_path)
        if prov is None:
            continue

        generated_at = _coerce_float(prov.get("generated_at"))
        age_days = max(0.0, (now - generated_at) / 86400.0) if generated_at else 0.0

        out.append(
            CandidateMetadata(
                name=child.name,
                description=str(prov.get("description", "")),
                confidence_score=_coerce_int(prov.get("confidence_score")),
                generated_at=generated_at,
                session_id=str(prov.get("session_id", "")),
                age_days=age_days,
            )
        )

    out.sort(key=lambda c: c.generated_at, reverse=True)
    return out


def get_candidate(profile_home: Path, name: str) -> ProposedSkill | None:
    """Load the full SKILL.md + provenance for review. ``None`` if missing."""
    if _is_hidden(name):
        return None
    cand_dir = _proposed_dir(profile_home) / name
    skill_path = cand_dir / "SKILL.md"
    prov_path = cand_dir / "provenance.json"
    if not (cand_dir.is_dir() and skill_path.exists() and prov_path.exists()):
        return None

    try:
        body = skill_path.read_text(encoding="utf-8")
    except OSError:
        return None
    prov = _read_provenance(prov_path)
    if prov is None:
        return None

    description = str(prov.get("description", ""))

    return ProposedSkill(
        name=name,
        description=description,
        body=body,
        provenance=prov,
    )


# ─── accept ───────────────────────────────────────────────────────────


def accept_candidate(profile_home: Path, name: str) -> Path:
    """Move ``_proposed/<name>/`` → ``<profile_home>/skills/<name>/``.

    Implementation: stage a copy under ``skills/.accepting-<name>/`` then
    atomically ``os.rename`` to the final active path. The original
    ``_proposed/<name>/`` is removed last so that on any mid-flight error
    the candidate is still recoverable from staging.

    Raises :class:`FileExistsError` if active ``skills/<name>/`` already
    exists (we never overwrite a real skill). Raises
    :class:`FileNotFoundError` if the candidate doesn't exist.
    """
    if _is_hidden(name):
        raise FileNotFoundError(f"candidate {name!r} not found")

    src = _proposed_dir(profile_home) / name
    if not src.is_dir():
        raise FileNotFoundError(f"candidate {name!r} not found")

    active_root = _active_dir(profile_home)
    active_root.mkdir(parents=True, exist_ok=True)
    dest = active_root / name

    if dest.exists():
        raise FileExistsError(
            f"active skill {name!r} already exists at {dest}"
        )

    staging = active_root / f".accepting-{name}"
    if staging.exists():
        # Stale staging from a prior crashed accept — clean it up.
        shutil.rmtree(staging, ignore_errors=True)

    try:
        shutil.copytree(src, staging)
        os.rename(staging, dest)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    # Source removal happens after the active copy is in place — if this
    # fails the user has the active skill plus a stale candidate, which is
    # recoverable; the inverse would risk losing the skill entirely.
    shutil.rmtree(src, ignore_errors=True)

    return dest


# ─── reject ───────────────────────────────────────────────────────────


def reject_candidate(profile_home: Path, name: str) -> bool:
    """Delete ``_proposed/<name>/``. Returns ``True`` if found and deleted.

    Implementation renames the directory into a ``.deleted-<name>-<ts>/``
    sibling first, so a concurrent :func:`list_candidates` never observes
    a half-emptied directory.
    """
    if _is_hidden(name):
        return False

    proposed_root = _proposed_dir(profile_home)
    src = proposed_root / name
    if not src.is_dir():
        return False

    tombstone = proposed_root / f".deleted-{name}-{int(time.time() * 1000)}"
    try:
        os.rename(src, tombstone)
    except OSError:
        # Fall back to direct rmtree; the rename failure already means the
        # path's gone or unreachable.
        shutil.rmtree(src, ignore_errors=True)
        return not src.exists()

    shutil.rmtree(tombstone, ignore_errors=True)
    return True


# ─── prune ────────────────────────────────────────────────────────────


def prune_old_candidates(profile_home: Path, max_age_days: int = 90) -> int:
    """Auto-delete proposals older than the threshold. Returns count pruned."""
    cutoff = time.time() - max_age_days * 86400.0
    pruned = 0
    for meta in list_candidates(profile_home):
        if (
            meta.generated_at
            and meta.generated_at < cutoff
            and reject_candidate(profile_home, meta.name)
        ):
            pruned += 1
    return pruned


__all__ = [
    "CandidateMetadata",
    "accept_candidate",
    "add_candidate",
    "get_candidate",
    "list_candidates",
    "prune_old_candidates",
    "reject_candidate",
]
