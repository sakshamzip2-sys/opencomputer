"""Filesystem layout for the procedural-memory loop.

Phase 5.B-1: switched to hierarchical layout to match the existing
``evolution/synthesize.SkillSynthesizer`` output. Both synthesizers
write the same shape now.

Layout under each profile home::

    ~/.opencomputer/profiles/<name>/
    └── evolution/
        ├── quarantine/      drafts awaiting user approval
        │   └── <slug>/
        │       └── SKILL.md (+ optional references/, examples/)
        ├── approved/        moved here on approval, also activated
        │   └── <slug>/SKILL.md
        ├── archive/         user-discarded drafts (audit trail)
        │   └── <slug>/SKILL.md
        └── rate.db          per-day / lifetime counters

The ``approved/`` directory is what the skill registry adds to its
search path on next session — same shape as ``opencomputer/skills/
<slug>/SKILL.md`` so the activation matcher works uniformly.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def evolution_root(home: Path) -> Path:
    """Return the per-profile ``<home>/evolution/`` directory."""
    return Path(home) / "evolution"


def quarantine_dir(home: Path) -> Path:
    return evolution_root(home) / "quarantine"


def approved_dir(home: Path) -> Path:
    return evolution_root(home) / "approved"


def archive_dir(home: Path) -> Path:
    return evolution_root(home) / "archive"


def ensure_dirs(home: Path) -> None:
    """Create the evolution subdirectories if missing."""
    for d in (quarantine_dir(home), approved_dir(home), archive_dir(home)):
        d.mkdir(parents=True, exist_ok=True)


def list_drafts(home: Path) -> list[Path]:
    """Return all SKILL.md drafts currently in quarantine, sorted by mtime asc.

    Hierarchical layout: each draft lives at
    ``quarantine/<slug>/SKILL.md``. We yield the SKILL.md file path
    so callers can read content without inspecting the dir.
    """
    q = quarantine_dir(home)
    if not q.exists():
        return []
    return sorted(q.glob("*/SKILL.md"), key=lambda p: p.stat().st_mtime)


def list_approved(home: Path) -> list[Path]:
    """Return all approved SKILL.md files (one per skill dir)."""
    a = approved_dir(home)
    if not a.exists():
        return []
    return sorted(a.glob("*/SKILL.md"))


def approve_draft(home: Path, slug: str) -> Path:
    """Move ``quarantine/<slug>/`` to ``approved/<slug>/``.

    Returns the new ``approved/<slug>/SKILL.md`` path. Raises
    ``FileNotFoundError`` if the draft is missing; raises
    ``FileExistsError`` if the slug already exists in approved
    (collision should be caught earlier, this is the last
    line of defense).
    """
    src = quarantine_dir(home) / slug
    if not src.exists():
        raise FileNotFoundError(f"no draft named {slug!r} in quarantine")
    dest_dir = approved_dir(home) / slug
    if dest_dir.exists():
        raise FileExistsError(f"approved skill {slug!r} already exists")
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest_dir))
    return dest_dir / "SKILL.md"


def discard_draft(home: Path, slug: str) -> None:
    """Move ``quarantine/<slug>/`` to ``archive/<slug>/``.

    Archive entries are kept (not deleted) so we can audit user choices
    and avoid re-proposing the same pattern indefinitely.
    """
    src = quarantine_dir(home) / slug
    if not src.exists():
        raise FileNotFoundError(f"no draft named {slug!r} in quarantine")
    arch = archive_dir(home)
    arch.mkdir(parents=True, exist_ok=True)
    dest = arch / slug
    if dest.exists():
        # Discard-twice race: just remove the new draft, keep the older archived one.
        shutil.rmtree(src)
        return
    shutil.move(str(src), str(dest))


def is_archived(home: Path, slug: str) -> bool:
    """Has the user previously discarded this slug?"""
    return (archive_dir(home) / slug).exists()
