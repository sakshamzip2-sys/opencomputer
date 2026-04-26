"""``QuarantineWriter`` — shared atomic-write path for synthesized skills.

Phase 5.B-1 of catch-up plan. Extracted from
``evolution/synthesize.py::SkillSynthesizer`` so both synthesizers
(reflection-driven and pattern-driven) can share the persistence
logic instead of duplicating it.

Responsibilities (single):

- Atomically write a ``<dest>/<slug>/SKILL.md`` tree (with optional
  ``references/`` and ``examples/`` subdirs).
- Resolve slug collisions by appending ``-2``, ``-3``, …, ``-99``.
- Reject path-traversal in reference/example filenames.

Non-responsibilities (caller's job):

- Validating the input (caller calls
  :func:`opencomputer.evolution.constraints.validate_synthesized_skill`).
- Generating the SKILL.md content (template render or LLM call).
- Consent-gating, rate-limiting, audit logging.

Atomicity: write happens to a sibling tmp dir under ``<dest_root>/.<slug>.tmp.<rand>/``
and is moved into place via ``os.replace``. On any error mid-write,
the tmp dir is removed and the final dir is never partially written.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

_SLUG_RE: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class QuarantinedSkill:
    """Everything :class:`QuarantineWriter` needs to write one skill.

    The writer treats this as opaque — caller is responsible for
    rendering ``skill_md_content`` (frontmatter + body included).

    Attributes
    ----------
    slug:
        Base slug (lowercase + hyphens). Writer auto-resolves
        collisions by appending ``-2``, ``-3``, ...
    skill_md_content:
        Full text of the ``SKILL.md`` file — frontmatter, body,
        and any embedded markers. Writer does not interpret it.
    references:
        Optional auxiliary docs. Each is a dict ``{"name": str,
        "content": str}``. Written to ``<dir>/references/<name>``.
    examples:
        Same shape as ``references``, written to ``<dir>/examples/<name>``.
    """

    slug: str
    skill_md_content: str
    references: tuple[dict, ...] = ()
    examples: tuple[dict, ...] = ()


@dataclass
class QuarantineWriter:
    """Atomic skill-tree writer. Caller creates one per dest_root.

    Parameters
    ----------
    dest_root:
        Where skills land. Required at construction (no lazy default
        — callers always know which directory they're targeting:
        ``evolution_home() / "skills"`` for the existing reflection
        pipeline, or ``home / "evolution" / "quarantine"`` for the
        pattern-detection pipeline).
    """

    dest_root: Path

    def _resolve_slug(self, base_slug: str) -> str:
        """Return a slug that doesn't collide with an existing dir.

        Tries ``base_slug``, then ``base_slug-2``, ..., ``base_slug-99``.
        Raises ``FileExistsError`` if all 99 are taken.
        """
        if not _SLUG_RE.match(base_slug):
            raise ValueError(
                f"slug {base_slug!r} must match {_SLUG_RE.pattern} "
                "(lowercase alphanumeric + hyphens; first char alphanumeric)"
            )
        candidate = base_slug
        for n in range(2, 100):
            if not (self.dest_root / candidate).exists():
                return candidate
            candidate = f"{base_slug}-{n}"
        raise FileExistsError(
            f"All slugs from {base_slug} to {base_slug}-99 are taken under "
            f"{self.dest_root}"
        )

    @staticmethod
    def _write_safe_named_file(parent: Path, name: str, content: str) -> None:
        """Write ``content`` to ``parent/name``. Rejects path-traversal."""
        if "/" in name or "\\" in name or name.startswith(".") or name == "":
            raise ValueError(
                f"reference/example name {name!r} is unsafe; must be a plain filename"
            )
        (parent / name).write_text(content, encoding="utf-8")

    def write(self, skill: QuarantinedSkill) -> Path:
        """Atomically write the skill tree. Returns the final directory path.

        Atomic-rename pattern: contents go into a tmp dir under
        ``<dest_root>/.<slug>.tmp.XXXXXX/``, then ``os.replace(tmp, final)``.
        """
        self.dest_root.mkdir(parents=True, exist_ok=True)
        slug = self._resolve_slug(skill.slug)
        final_dir = self.dest_root / slug
        tmp_dir = Path(
            tempfile.mkdtemp(prefix=f".{slug}.tmp.", dir=self.dest_root)
        )
        try:
            (tmp_dir / "SKILL.md").write_text(
                skill.skill_md_content, encoding="utf-8"
            )
            if skill.references:
                ref_dir = tmp_dir / "references"
                ref_dir.mkdir()
                for ref in skill.references:
                    if not isinstance(ref, dict) or "name" not in ref or "content" not in ref:
                        raise ValueError(
                            f"reference entry must be {{'name': str, 'content': str}}, got {ref!r}"
                        )
                    self._write_safe_named_file(ref_dir, ref["name"], ref["content"])
            if skill.examples:
                ex_dir = tmp_dir / "examples"
                ex_dir.mkdir()
                for ex in skill.examples:
                    if not isinstance(ex, dict) or "name" not in ex or "content" not in ex:
                        raise ValueError(
                            f"example entry must be {{'name': str, 'content': str}}, got {ex!r}"
                        )
                    self._write_safe_named_file(ex_dir, ex["name"], ex["content"])
            os.replace(tmp_dir, final_dir)
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        return final_dir
