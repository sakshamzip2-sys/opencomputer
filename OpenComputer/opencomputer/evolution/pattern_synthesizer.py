"""Pattern-detector → SKILL.md synthesizer (5.2 + 5.B-1 + 2026-05-02 schema migration).

Takes a :class:`SkillDraftProposal` from the pattern detector and:

1. Loads the Jinja2 template at ``prompts/synthesis_request.j2``.
2. Substitutes the proposal + the list of existing skill names so the
   model can avoid collisions.
3. Calls :func:`opencomputer.agent.structured.parse_structured` with the
   :class:`SynthesizedSkill` Pydantic model — the model emits a
   structured JSON object the provider validates server-side. No
   regex-validation of YAML frontmatter; schema enforcement is the
   provider's job.
4. Validates the slug against existing skills (collision check) — the
   only validation that's not in the schema, because it depends on
   filesystem state.
5. Renders SKILL.md text from the struct (frontmatter + body),
   preserving the existing on-disk format.
6. Hands a :class:`QuarantinedSkill` to a :class:`QuarantineWriter` for
   atomic write + slug-collision auto-resolution.

What it does NOT do (deliberately):

- Activate the skill — quarantine is staging only, user explicitly
  approves via the CLI (Phase 5.B-3).
- Hit the consent gate — caller wraps this with the gate (5.B-3).
- Apply rate limits — caller calls the limiter (5.B-3).
- Audit-log the draft — caller logs (5.B-3).

Distinct from the older
:class:`opencomputer.evolution.synthesize.SkillSynthesizer` which
writes from reflection ``Insight`` payloads. Both now compose with
the same writer.

2026-05-02 — Subsystem C migration: replaces regex-validated
YAML-frontmatter parsing with schema-enforced JSON output. Removes
``_FRONTMATTER_OPEN`` and ``_FRONTMATTER_NAME`` regex; adds the
:class:`SynthesizedSkill` Pydantic schema.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, Field

from opencomputer.agent.structured import StructuredOutputError, parse_structured
from opencomputer.evolution.pattern_detector import SkillDraftProposal
from opencomputer.evolution.quarantine_writer import (
    QuarantinedSkill,
    QuarantineWriter,
)
from opencomputer.evolution.store import ensure_dirs, quarantine_dir
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider

# Legacy-path regex (kept for backwards compatibility with the
# Protocol-based ``_LegacyProvider`` shape). When ``model`` is set on
# the synthesizer, the schema-enforced path runs instead.
_FRONTMATTER_NAME = re.compile(r"^name:\s*([a-z0-9][a-z0-9-]*[a-z0-9])\s*$", re.MULTILINE)
_FRONTMATTER_OPEN = re.compile(r"\A---\s*\n")


class SynthesizedSkill(BaseModel):
    """Schema for a synthesized SKILL.md draft.

    The provider emits a JSON object matching this shape; we render
    the SKILL.md file format from the validated fields. Frontmatter
    + body are constructed deterministically — no LLM control over
    file structure beyond the three field values.
    """

    name: str = Field(
        pattern=r"^[a-z][a-z0-9-]*$",
        min_length=3,
        max_length=64,
        description="Lowercase hyphenated slug. No spaces, no underscores.",
    )
    description: str = Field(
        min_length=10,
        max_length=200,
        description=(
            "One-line skill description starting with 'Use when...'. "
            "Includes distinctive trigger tokens for the activation matcher."
        ),
    )
    body: str = Field(
        min_length=50,
        description=(
            "Markdown body (NO frontmatter — that's synthesized from `name` "
            "and `description`). Must include `# Title`, `## When to use`, "
            "and `## Steps`."
        ),
    )


def render_skill_md(skill: SynthesizedSkill) -> str:
    """Render a validated SynthesizedSkill into the on-disk SKILL.md format."""
    return (
        "---\n"
        f"name: {skill.name}\n"
        f"description: {skill.description}\n"
        "---\n"
        "\n"
        f"{skill.body.rstrip()}\n"
    )


class _LegacyProvider(Protocol):
    """Backwards-compat Protocol for callers that pre-date Subsystem C.

    Only used when ``model`` is not set on the synthesizer — falls
    back to the legacy ``provider.complete(prompt) -> str`` shape and
    parses the response as JSON post-hoc (no server-side schema
    enforcement). Caller must add JSON instructions to the prompt.
    """

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
    provider: BaseProvider | _LegacyProvider
    model: str = ""
    """Model id for the schema-enforced path. When empty, fall back to
    the legacy ``provider.complete(prompt) -> str`` shape (no schema
    enforcement — see :class:`_LegacyProvider`)."""
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

        existing = set(self._existing_skill_names())

        # Schema-enforced path (Subsystem C). Requires a real BaseProvider
        # and a model. When ``model`` is unset, fall back to the legacy
        # YAML-frontmatter path so existing callers + test fakes keep
        # working unchanged.
        if self.model and isinstance(self.provider, BaseProvider):
            try:
                skill = await parse_structured(
                    response_model=SynthesizedSkill,
                    messages=[Message(role="user", content=prompt)],
                    provider=self.provider,
                    model=self.model,
                    max_tokens=2000,
                )
            except StructuredOutputError as exc:
                raise SynthesisError(str(exc)) from exc

            if skill.name in existing:
                raise SynthesisError(
                    f"slug {skill.name!r} collides with existing skill"
                )

            skill_md = render_skill_md(skill)
            if len(skill_md) > self.max_chars:
                raise SynthesisError(
                    f"output exceeds size cap: {len(skill_md)} > {self.max_chars}"
                )
            slug = skill.name
        else:
            # Legacy path: provider.complete(prompt) -> str returns
            # YAML-frontmatter SKILL.md text, validated with regex.
            text = await self.provider.complete(prompt)  # type: ignore[arg-type]
            if not text:
                raise SynthesisError("provider returned empty output")
            if len(text) > self.max_chars:
                raise SynthesisError(
                    f"output exceeds size cap: {len(text)} > {self.max_chars}"
                )
            if not _FRONTMATTER_OPEN.match(text):
                raise SynthesisError(
                    "output does not start with `---` frontmatter"
                )
            m = _FRONTMATTER_NAME.search(text)
            if not m:
                raise SynthesisError(
                    "frontmatter missing valid `name:` field (slug-style required)"
                )
            slug = m.group(1)
            if slug in existing:
                raise SynthesisError(
                    f"slug {slug!r} collides with existing skill"
                )
            skill_md = text

        # ── Persist via shared writer ──
        quarantined = QuarantinedSkill(slug=slug, skill_md_content=skill_md)
        # ``writer`` is set in __post_init__; assert is for type-narrowing.
        assert self.writer is not None
        skill_dir = self.writer.write(quarantined)
        return skill_dir / "SKILL.md"
