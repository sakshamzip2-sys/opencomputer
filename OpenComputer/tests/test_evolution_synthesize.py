"""Tests for opencomputer.evolution.synthesize — SkillSynthesizer constructor.

Tests for synthesize() logic are in test_evolution_synthesize_skill.py.
"""

from __future__ import annotations

from pathlib import Path

from opencomputer.evolution.synthesize import SkillSynthesizer

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
