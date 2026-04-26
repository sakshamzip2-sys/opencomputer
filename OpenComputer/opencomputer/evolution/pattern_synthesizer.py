"""Generate a SKILL.md draft from a SkillDraftProposal.

Phase 5.2 of catch-up plan, refactored at Phase 5.B-1 to compose with
:class:`opencomputer.evolution.quarantine_writer.QuarantineWriter` so
both synthesizers share the persistence path.

What this module does:

1. Loads the Jinja2 template at ``prompts/synthesis_request.j2``.
2. Substitutes the proposal + the list of existing skill names so the
   model can avoid collisions.
3. Calls ``provider.complete(prompt)`` once.
4. Validates the output:
   - Starts with ``---`` (YAML frontmatter)
   - Has a ``name:`` slug field (lowercase, hyphenated)
   - Total length ≤ ``max_chars``
   - Slug is not already in approved skills + bundled skills
5. Hands a :class:`QuarantinedSkill` to a :class:`QuarantineWriter` for
   atomic write + slug-collision auto-resolution.

What it does NOT do (deliberately):

- Activate the skill — quarantine is staging only, user explicitly
  approves via the CLI (Phase 5.B-3).
- Hit the consent gate — caller wraps this with the gate (5.B-3).
- Apply rate limits — caller calls the limiter (5.B-3).
- Audit-log the draft — caller logs (5.B-3).

The synthesizer is the simplest possible component; layered guards
go around it, not inside.

Distinct from the older
:class:`opencomputer.evolution.synthesize.SkillSynthesizer` which
writes from reflection ``Insight`` payloads. Both now compose with
the same writer.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jinja2 import Environment, FileSystemLoader

from opencomputer.evolution.pattern_detector import SkillDraftProposal
from opencomputer.evolution.quarantine_writer import (
    QuarantinedSkill,
    QuarantineWriter,
)
from opencomputer.evolution.store import ensure_dirs, quarantine_dir

_FRONTMATTER_NAME = re.compile(r"^name:\s*([a-z0-9][a-z0-9-]*[a-z0-9])\s*$", re.MULTILINE)
_FRONTMATTER_OPEN = re.compile(r"\A---\s*\n")


class _Provider(Protocol):
    """Anything with an awaitable ``complete(prompt) -> str``."""

    def complete(self, prompt: str) -> Awaitable[str]: ...


class SynthesisError(ValueError):  # noqa: N818 — domain term, not exception suffix
    """The model output failed validation."""


@dataclass
class PatternSynthesizer:
    """Pattern-detector → SKILL.md synthesizer.

    Distinct from the older
    :class:`opencomputer.evolution.synthesize.SkillSynthesizer`. Both
    compose with :class:`QuarantineWriter` for persistence.
    """

    home: Path
    provider: _Provider
    max_chars: int = 5000
    writer: QuarantineWriter | None = field(default=None)

    def __post_init__(self) -> None:
        prompts_dir = Path(__file__).parent / "prompts"
        self._env = Environment(
            loader=FileSystemLoader(prompts_dir),
            autoescape=False,
            keep_trailing_newline=True,
        )
        if self.writer is None:
            ensure_dirs(self.home)
            self.writer = QuarantineWriter(dest_root=quarantine_dir(self.home))

    def _existing_skill_names(self) -> list[str]:
        """Bundled skill names + already-approved profile-local skills."""
        names: set[str] = set()
        bundled = Path(__file__).resolve().parents[1] / "skills"
        if bundled.is_dir():
            names.update(p.name for p in bundled.iterdir() if p.is_dir())
        from opencomputer.evolution.store import approved_dir
        approved = approved_dir(self.home)
        if approved.is_dir():
            names.update(p.name for p in approved.iterdir() if p.is_dir())
        return sorted(names)

    async def synthesize(self, proposal: SkillDraftProposal) -> Path:
        prompt = self._env.get_template("synthesis_request.j2").render(
            proposal=proposal,
            existing_names=self._existing_skill_names(),
            max_chars=self.max_chars,
        )

        text = await self.provider.complete(prompt)

        # ── Validation ──
        if not text:
            raise SynthesisError("provider returned empty output")
        if len(text) > self.max_chars:
            raise SynthesisError(
                f"output exceeds size cap: {len(text)} > {self.max_chars}"
            )
        if not _FRONTMATTER_OPEN.match(text):
            raise SynthesisError("output does not start with `---` frontmatter")
        m = _FRONTMATTER_NAME.search(text)
        if not m:
            raise SynthesisError(
                "frontmatter missing valid `name:` field (slug-style required)"
            )
        slug = m.group(1)
        existing = set(self._existing_skill_names())
        if slug in existing:
            raise SynthesisError(
                f"slug {slug!r} collides with existing skill"
            )

        # ── Persist via shared writer ──
        skill = QuarantinedSkill(slug=slug, skill_md_content=text)
        # ``writer`` is set in __post_init__; assert is for type-narrowing.
        assert self.writer is not None
        skill_dir = self.writer.write(skill)
        return skill_dir / "SKILL.md"
