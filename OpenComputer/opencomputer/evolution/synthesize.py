"""Skill synthesizer for OpenComputer Evolution.

``SkillSynthesizer`` writes synthesized skills to the evolution quarantine
namespace using a III.4 hierarchical layout.  Atomic writes via a sibling tmp
dir + os.replace ensure the final skill tree is never partially written.

Design reference: OpenComputer/docs/evolution/design.md §8.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from jinja2 import Environment, FileSystemLoader

from opencomputer.evolution.reflect import Insight
from opencomputer.evolution.storage import evolution_home

if TYPE_CHECKING:
    pass  # nothing extra

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_TEMPLATE_DIR = Path(__file__).parent / "prompts"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


# ---------------------------------------------------------------------------
# SkillSynthesizer
# ---------------------------------------------------------------------------


class SkillSynthesizer:
    """Writes synthesized skills to the evolution quarantine namespace.

    Implements III.4 hierarchical layout::

        <evolution_home>/skills/<slug>/
        ├── SKILL.md
        ├── references/   (optional)
        └── examples/     (optional)

    The ``<!-- generated-by: opencomputer-evolution -->`` comment in every
    SKILL.md is the quarantine marker distinguishing synthesized from
    user-authored skills.
    """

    def __init__(self, *, dest_dir: Path | None = None) -> None:
        """Initialise the synthesizer.

        Args:
            dest_dir: Destination directory for synthesized skill trees.  If
                ``None``, defaults to ``evolution_home() / "skills"`` — resolved
                lazily inside ``synthesize()`` to keep imports light.
        """
        self._dest_dir = dest_dir

    def _resolve_dest_dir(self) -> Path:
        """Lazy resolution so import-time doesn't materialize ~/.opencomputer/."""
        if self._dest_dir is not None:
            return self._dest_dir
        return evolution_home() / "skills"

    def _build_env(self) -> Environment:
        return Environment(
            loader=FileSystemLoader(_TEMPLATE_DIR),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )

    def _resolve_slug(self, base_slug: str, dest_root: Path) -> str:
        """Return a slug that doesn't collide with an existing dir.

        First tries ``base_slug``. If ``<dest_root>/<base_slug>/`` exists,
        tries ``<base_slug>-2``, ``<base_slug>-3``, … up to
        ``<base_slug>-99``. Raises ``FileExistsError`` if all 99 are taken
        (defensive — unlikely in practice).
        """
        if not _SLUG_RE.match(base_slug):
            raise ValueError(
                f"slug {base_slug!r} must match {_SLUG_RE.pattern} "
                "(lowercase alphanumeric + hyphens; first char alphanumeric)"
            )
        candidate = base_slug
        for n in range(2, 100):
            if not (dest_root / candidate).exists():
                return candidate
            candidate = f"{base_slug}-{n}"
        raise FileExistsError(
            f"All slugs from {base_slug} to {base_slug}-99 are taken under {dest_root}"
        )

    def synthesize(self, insight: Insight) -> Path:
        """Write a SKILL.md (+ optional references/ + examples/) tree from an Insight.

        Atomic write: contents go into a tmp dir under ``<dest_root>/.<slug>.tmp/``,
        then ``os.replace(tmp, final)``. On any error mid-write, the tmp dir is
        removed and the final dir is never partially written.

        Returns the path to the written skill directory.

        Raises:
            ValueError: if ``insight.action_type`` is not ``"create_skill"`` or if
                required payload fields are missing or the slug is invalid.
            FileExistsError: if slug collision resolution exhausts all 99 candidates.
        """
        if insight.action_type != "create_skill":
            raise ValueError(
                f"SkillSynthesizer.synthesize requires action_type='create_skill', "
                f"got {insight.action_type!r}"
            )

        # Validate required payload fields
        payload = dict(insight.payload)
        for required in ("slug", "name", "description", "body"):
            if required not in payload:
                raise ValueError(
                    f"create_skill payload missing required field {required!r}"
                )

        # T1.3 PR-5: pre-write constraint gates. Mirrors Hermes
        # evolution/core/constraints.py — invalid candidates rejected
        # BEFORE the atomic tmp+os.replace write reaches disk.
        from opencomputer.evolution.constraints import (  # noqa: PLC0415
            ConstraintViolation,  # noqa: F401 — re-exported so callers can catch it
            validate_synthesized_skill,
        )
        validate_synthesized_skill(payload)
        # ConstraintViolation is a ValueError subclass; existing call sites
        # already catch ValueError so this integrates cleanly.

        dest_root = self._resolve_dest_dir()
        dest_root.mkdir(parents=True, exist_ok=True)

        slug = self._resolve_slug(payload["slug"], dest_root)
        final_dir = dest_root / slug

        # Write into a sibling tmp dir, then os.replace atomic-rename to final
        tmp_dir = Path(tempfile.mkdtemp(prefix=f".{slug}.tmp.", dir=dest_root))
        try:
            self._write_skill_tree(tmp_dir, slug, insight, payload)
            os.replace(tmp_dir, final_dir)  # atomic on POSIX
        except Exception:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise

        return final_dir

    def _write_skill_tree(
        self,
        target_dir: Path,
        slug: str,
        insight: Insight,
        payload: dict,
    ) -> None:
        """Write SKILL.md + optional references/ + examples/ into target_dir."""
        env = self._build_env()
        template = env.get_template("synthesize.j2")
        rendered = template.render(
            slug=slug,
            name=payload["name"],
            description=payload["description"],
            body=payload["body"],
            confidence=insight.confidence,
            evidence_refs=insight.evidence_refs,
        )
        (target_dir / "SKILL.md").write_text(rendered, encoding="utf-8")

        references = payload.get("references") or []
        if references:
            ref_dir = target_dir / "references"
            ref_dir.mkdir()
            for ref in references:
                if not isinstance(ref, dict) or "name" not in ref or "content" not in ref:
                    raise ValueError(
                        f"reference entry must be {{'name': str, 'content': str}}, got {ref!r}"
                    )
                self._write_safe_named_file(ref_dir, ref["name"], ref["content"])

        examples = payload.get("examples") or []
        if examples:
            ex_dir = target_dir / "examples"
            ex_dir.mkdir()
            for ex in examples:
                if not isinstance(ex, dict) or "name" not in ex or "content" not in ex:
                    raise ValueError(
                        f"example entry must be {{'name': str, 'content': str}}, got {ex!r}"
                    )
                self._write_safe_named_file(ex_dir, ex["name"], ex["content"])

    @staticmethod
    def _write_safe_named_file(parent: Path, name: str, content: str) -> None:
        """Write content to parent/name. Rejects names with path-traversal characters."""
        if "/" in name or "\\" in name or name.startswith(".") or name == "":
            raise ValueError(
                f"reference/example name {name!r} is unsafe; must be a plain filename"
            )
        (parent / name).write_text(content, encoding="utf-8")
