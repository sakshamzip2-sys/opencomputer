"""Tests for opencomputer.evolution.pattern_synthesizer (Phase 5.2).

Distinct from ``test_evolution_synthesizer`` which exercises the older
``synthesize.SkillSynthesizer`` (reflection-Insight based). This file
covers the pattern-detection-driven synthesizer added by the catch-up
plan.
"""

from __future__ import annotations

import pytest

from opencomputer.evolution.pattern_detector import SkillDraftProposal
from opencomputer.evolution.pattern_synthesizer import (
    PatternSynthesizer,
    SynthesisError,
)
from opencomputer.evolution.store import (
    approved_dir,
    discard_draft,
    is_archived,
    list_approved,
    list_drafts,
    quarantine_dir,
)

# ---------- Fakes ----------


class _FakeProvider:
    def __init__(self, return_text: str = ""):
        self.return_text = return_text
        self.calls: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.return_text


_GOOD_DRAFT = """---
name: pytest-rerun
description: Use when pytest fails repeatedly to re-run failures fast
---

# Pytest Rerun

## When to use
- pytest failed multiple times in a row
- you want to focus on just the failures

## Steps
1. Run `pytest -lf -x` to re-run last failures only
2. Inspect the failure output
3. Fix and re-run

## Notes
- `-lf` reads `.pytest_cache/lastfailed`
"""


def _proposal() -> SkillDraftProposal:
    return SkillDraftProposal(
        pattern_key="bash:pytest:fail",
        pattern_summary="`pytest` shell command failed 3 times",
        sample_arguments=({"command": "pytest -x"},),
        count=3,
    )


# ---------- Happy path ----------


@pytest.mark.asyncio
async def test_synthesize_writes_draft_to_quarantine(tmp_path):
    s = PatternSynthesizer(home=tmp_path, provider=_FakeProvider(_GOOD_DRAFT))
    path = await s.synthesize(_proposal())
    assert path == quarantine_dir(tmp_path) / "pytest-rerun.md"
    assert path.exists()
    assert "pytest" in path.read_text().lower()


@pytest.mark.asyncio
async def test_prompt_passes_proposal_and_existing_names(tmp_path):
    fake = _FakeProvider(_GOOD_DRAFT)
    s = PatternSynthesizer(home=tmp_path, provider=fake)
    await s.synthesize(_proposal())
    assert len(fake.calls) == 1
    p = fake.calls[0]
    assert "pytest" in p.lower()
    # Existing bundled skills are listed in the prompt so the model
    # can avoid collisions.
    assert "code-review" in p or "test-driven-development" in p


# ---------- Validation ----------


@pytest.mark.asyncio
async def test_empty_output_rejected(tmp_path):
    s = PatternSynthesizer(home=tmp_path, provider=_FakeProvider(""))
    with pytest.raises(SynthesisError, match="empty"):
        await s.synthesize(_proposal())


@pytest.mark.asyncio
async def test_oversized_output_rejected(tmp_path):
    huge = "---\nname: x\n---\n" + "X" * 10_000
    s = PatternSynthesizer(home=tmp_path, provider=_FakeProvider(huge), max_chars=5000)
    with pytest.raises(SynthesisError, match="size cap"):
        await s.synthesize(_proposal())


@pytest.mark.asyncio
async def test_missing_frontmatter_rejected(tmp_path):
    bad = "# Just a heading\n\nNo frontmatter here."
    s = PatternSynthesizer(home=tmp_path, provider=_FakeProvider(bad))
    with pytest.raises(SynthesisError, match="frontmatter"):
        await s.synthesize(_proposal())


@pytest.mark.asyncio
async def test_missing_name_field_rejected(tmp_path):
    bad = "---\ndescription: hi\n---\n# Body"
    s = PatternSynthesizer(home=tmp_path, provider=_FakeProvider(bad))
    with pytest.raises(SynthesisError, match="name"):
        await s.synthesize(_proposal())


@pytest.mark.asyncio
async def test_invalid_slug_format_rejected(tmp_path):
    # underscores + uppercase + leading hyphen are all invalid slug formats
    for bad_slug in ("Bad_Slug", "-leading-hyphen", "trailing-", ""):
        bad = f"---\nname: {bad_slug}\n---\n# Body"
        s = PatternSynthesizer(home=tmp_path, provider=_FakeProvider(bad))
        with pytest.raises(SynthesisError):
            await s.synthesize(_proposal())


@pytest.mark.asyncio
async def test_slug_collision_with_bundled_rejected(tmp_path):
    # `code-review` is one of the bundled skills (Phase 3)
    collide = "---\nname: code-review\ndescription: x\n---\n# x"
    s = PatternSynthesizer(home=tmp_path, provider=_FakeProvider(collide))
    with pytest.raises(SynthesisError, match="collides"):
        await s.synthesize(_proposal())


# ---------- Store helpers ----------


def test_list_drafts_returns_quarantined_files(tmp_path):
    quarantine_dir(tmp_path).mkdir(parents=True)
    (quarantine_dir(tmp_path) / "a.md").write_text("a")
    (quarantine_dir(tmp_path) / "b.md").write_text("b")
    drafts = list_drafts(tmp_path)
    assert len(drafts) == 2


def test_list_drafts_returns_empty_when_no_quarantine(tmp_path):
    assert list_drafts(tmp_path) == []


def test_approve_moves_to_approved_dir(tmp_path):
    quarantine_dir(tmp_path).mkdir(parents=True)
    src = quarantine_dir(tmp_path) / "my-skill.md"
    src.write_text("---\nname: my-skill\n---\n# x")
    from opencomputer.evolution.store import approve_draft
    dest = approve_draft(tmp_path, "my-skill")
    assert dest == approved_dir(tmp_path) / "my-skill" / "SKILL.md"
    assert dest.exists()
    assert not src.exists()
    assert len(list_approved(tmp_path)) == 1


def test_approve_missing_draft_raises(tmp_path):
    from opencomputer.evolution.store import approve_draft
    with pytest.raises(FileNotFoundError):
        approve_draft(tmp_path, "ghost")


def test_approve_existing_dir_raises(tmp_path):
    from opencomputer.evolution.store import approve_draft, ensure_dirs
    ensure_dirs(tmp_path)
    (quarantine_dir(tmp_path) / "x.md").write_text("---\nname: x\n---\n# x")
    (approved_dir(tmp_path) / "x").mkdir()
    with pytest.raises(FileExistsError):
        approve_draft(tmp_path, "x")


def test_discard_moves_to_archive(tmp_path):
    quarantine_dir(tmp_path).mkdir(parents=True)
    (quarantine_dir(tmp_path) / "x.md").write_text("x")
    discard_draft(tmp_path, "x")
    assert is_archived(tmp_path, "x") is True
    assert not (quarantine_dir(tmp_path) / "x.md").exists()


def test_discard_missing_draft_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        discard_draft(tmp_path, "ghost")
