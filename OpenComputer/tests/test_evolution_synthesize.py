"""Tests for opencomputer.evolution.synthesize — SkillSynthesizer stub.

All tests are pure unit tests; no I/O performed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.evolution.reflect import Insight
from opencomputer.evolution.synthesize import SkillSynthesizer

# ---------------------------------------------------------------------------
# Helper: a valid Insight for use in synthesize() calls
# ---------------------------------------------------------------------------


def _valid_insight() -> Insight:
    return Insight(
        observation="Agent always re-reads files it just wrote.",
        evidence_refs=(10, 11),
        action_type="create_skill",
        payload={"slug": "avoid-redundant-reread"},
        confidence=0.75,
    )


# ---------------------------------------------------------------------------
# 1. Constructs with defaults (dest_dir=None)
# ---------------------------------------------------------------------------


def test_synthesizer_constructs_with_defaults() -> None:
    """SkillSynthesizer() with no args works; internal dest_dir is None."""
    synth = SkillSynthesizer()
    # _dest_dir is private; confirm construction doesn't raise and object exists.
    assert synth is not None
    assert synth._dest_dir is None  # noqa: SLF001


# ---------------------------------------------------------------------------
# 2. Constructs with explicit dest_dir
# ---------------------------------------------------------------------------


def test_synthesizer_constructs_with_dest_dir(tmp_path: Path) -> None:
    """SkillSynthesizer accepts an explicit Path for dest_dir."""
    synth = SkillSynthesizer(dest_dir=tmp_path)
    assert synth._dest_dir == tmp_path  # noqa: SLF001


# ---------------------------------------------------------------------------
# 3. synthesize() raises NotImplementedError mentioning B2
# ---------------------------------------------------------------------------


def test_synthesize_raises_not_implemented() -> None:
    """synthesize() raises NotImplementedError with a message mentioning 'B2'."""
    synth = SkillSynthesizer()
    with pytest.raises(NotImplementedError, match="B2"):
        synth.synthesize(_valid_insight())
