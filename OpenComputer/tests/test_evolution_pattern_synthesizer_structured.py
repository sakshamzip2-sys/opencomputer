"""Tests for the Subsystem C schema-validated path of PatternSynthesizer.

Distinct from ``test_evolution_pattern_synthesizer.py`` which covers the
legacy YAML-frontmatter path (``model=""``, Protocol-based ``_FakeProvider``).
This file exercises the new path that uses
:func:`opencomputer.agent.structured.parse_structured` end-to-end.
"""

from __future__ import annotations

from typing import Any

import pytest

from opencomputer.evolution.pattern_detector import SkillDraftProposal
from opencomputer.evolution.pattern_synthesizer import (
    PatternSynthesizer,
    SynthesisError,
    SynthesizedSkill,
    render_skill_md,
)
from opencomputer.evolution.store import quarantine_dir
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)


class _StubBaseProvider(BaseProvider):
    """Real BaseProvider stub — accepts response_schema kwarg + returns JSON."""

    name = "stub"
    default_model = "stub-1"

    def __init__(self, json_text: str) -> None:
        self.json_text = json_text
        self.captured_kwargs: dict[str, Any] = {}

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        self.captured_kwargs = kwargs
        return ProviderResponse(
            message=Message(role="assistant", content=self.json_text),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(self, **kwargs: Any):
        resp = await self.complete(**kwargs)
        yield StreamEvent(kind="done", response=resp)


def _proposal() -> SkillDraftProposal:
    return SkillDraftProposal(
        pattern_key="bash:pytest:fail",
        pattern_summary="`pytest` shell command failed 3 times",
        sample_arguments=({"command": "pytest -x"},),
        count=3,
    )


_GOOD_JSON = (
    '{'
    '"name": "pytest-rerun",'
    '"description": "Use when pytest fails repeatedly to re-run failures fast",'
    '"body": "# Pytest Rerun\\n\\n## When to use\\n- pytest failed multiple times\\n\\n## Steps\\n1. Run pytest -lf -x\\n"'
    '}'
)


@pytest.mark.asyncio
async def test_schema_validated_path_writes_draft(tmp_path) -> None:
    """Happy path: schema-enforced JSON → valid SKILL.md in quarantine."""
    provider = _StubBaseProvider(_GOOD_JSON)
    synth = PatternSynthesizer(
        home=tmp_path,
        provider=provider,
        model="stub-1",
    )

    skill_md_path = await synth.synthesize(_proposal())

    assert skill_md_path.exists()
    written = skill_md_path.read_text()
    # Frontmatter synthesized from the structured fields.
    assert written.startswith("---\nname: pytest-rerun\n")
    assert "description: Use when pytest fails" in written
    # Body content from the JSON.
    assert "# Pytest Rerun" in written
    # Lives under quarantine dir.
    assert quarantine_dir(tmp_path) in skill_md_path.parents


@pytest.mark.asyncio
async def test_schema_path_passes_response_schema_to_provider(tmp_path) -> None:
    """response_schema kwarg must reach the provider."""
    provider = _StubBaseProvider(_GOOD_JSON)
    synth = PatternSynthesizer(
        home=tmp_path,
        provider=provider,
        model="stub-1",
    )

    await synth.synthesize(_proposal())

    schema_spec = provider.captured_kwargs.get("response_schema")
    assert schema_spec is not None
    assert schema_spec["name"] == "synthesizedskill"
    assert schema_spec["schema"]["type"] == "object"
    assert "name" in schema_spec["schema"]["properties"]
    assert "description" in schema_spec["schema"]["properties"]
    assert "body" in schema_spec["schema"]["properties"]


@pytest.mark.asyncio
async def test_schema_path_invalid_json_raises_synthesis_error(tmp_path) -> None:
    """Non-JSON output → SynthesisError (translated from StructuredOutputError)."""
    provider = _StubBaseProvider("not json at all")
    synth = PatternSynthesizer(
        home=tmp_path,
        provider=provider,
        model="stub-1",
    )

    with pytest.raises(SynthesisError, match="not valid JSON"):
        await synth.synthesize(_proposal())


@pytest.mark.asyncio
async def test_schema_path_invalid_slug_raises_synthesis_error(tmp_path) -> None:
    """Slug with invalid chars (uppercase) violates Pydantic pattern."""
    bad_json = (
        '{"name": "PytestRerun",'
        '"description": "Use when pytest fails to re-run failures fast",'
        '"body": "# Pytest Rerun\\n\\n## When to use\\n- bullet\\n\\n## Steps\\n1. step\\n"}'
    )
    provider = _StubBaseProvider(bad_json)
    synth = PatternSynthesizer(
        home=tmp_path,
        provider=provider,
        model="stub-1",
    )

    with pytest.raises(SynthesisError, match="schema validation"):
        await synth.synthesize(_proposal())


@pytest.mark.asyncio
async def test_schema_path_collision_check_still_runs(tmp_path) -> None:
    """Filesystem-state collision check applies to schema path too."""
    # Pre-create an approved skill with the same name.
    from opencomputer.evolution.store import approved_dir, ensure_dirs
    ensure_dirs(tmp_path)
    (approved_dir(tmp_path) / "pytest-rerun").mkdir(parents=True)

    provider = _StubBaseProvider(_GOOD_JSON)
    synth = PatternSynthesizer(
        home=tmp_path,
        provider=provider,
        model="stub-1",
    )

    with pytest.raises(SynthesisError, match="collides"):
        await synth.synthesize(_proposal())


def test_render_skill_md_format() -> None:
    """Pure function — verify SKILL.md format from a SynthesizedSkill."""
    skill = SynthesizedSkill(
        name="my-skill",
        description="Use when something happens that this skill addresses",
        body="# My Skill\n\n## When to use\n- always\n\n## Steps\n1. do it\n",
    )
    rendered = render_skill_md(skill)

    assert rendered.startswith("---\n")
    assert "name: my-skill\n" in rendered
    assert "description: Use when" in rendered
    assert "\n---\n\n" in rendered  # blank line after closing frontmatter
    assert rendered.endswith("\n")  # trailing newline
    assert "# My Skill" in rendered
