"""Tests for opencomputer.evolution.pattern_detector (Phase 5.1)."""

from __future__ import annotations

import pytest

from opencomputer.evolution.pattern_detector import (
    PatternDetector,
    SkillDraftProposal,
)

# ---------- Threshold + draining ----------


def test_below_threshold_no_proposals():
    det = PatternDetector(threshold=3)
    for _ in range(2):
        det.observe("Bash", {"command": "pytest"}, error=True)
    assert det.drain_proposals() == []


def test_threshold_reached_emits_one_proposal():
    det = PatternDetector(threshold=3)
    for _ in range(3):
        det.observe("Bash", {"command": "pytest"}, error=True)
    proposals = det.drain_proposals()
    assert len(proposals) == 1
    assert isinstance(proposals[0], SkillDraftProposal)
    assert proposals[0].count == 3
    assert "pytest" in proposals[0].pattern_summary


def test_drain_is_idempotent_until_reset():
    det = PatternDetector(threshold=3)
    for _ in range(3):
        det.observe("Bash", {"command": "pytest"}, error=True)
    first = det.drain_proposals()
    second = det.drain_proposals()
    assert len(first) == 1
    assert second == []  # already proposed
    det.reset_proposed()
    assert len(det.drain_proposals()) == 1  # back on the table


def test_distinct_patterns_tracked_independently():
    det = PatternDetector(threshold=3)
    for _ in range(3):
        det.observe("Edit", {"file_path": "/x/foo.py"})
    for _ in range(2):
        det.observe("Bash", {"command": "pytest"}, error=True)
    proposals = det.drain_proposals()
    assert len(proposals) == 1
    assert "edit" in proposals[0].pattern_key.lower()


def test_failures_and_successes_are_distinct_patterns():
    det = PatternDetector(threshold=3)
    for _ in range(3):
        det.observe("Bash", {"command": "pytest"}, error=False)  # ok
    for _ in range(3):
        det.observe("Bash", {"command": "pytest"}, error=True)   # fail
    proposals = det.drain_proposals()
    keys = {p.pattern_key for p in proposals}
    assert "bash:pytest:ok" in keys
    assert "bash:pytest:fail" in keys


# ---------- Pattern key shape ----------


@pytest.mark.parametrize(
    "tool,args,error,expected",
    [
        ("Bash", {"command": "pytest -x tests/"}, True, "bash:pytest:fail"),
        ("Bash", {"command": "git status"}, False, "bash:git:ok"),
        ("Bash", {"command": "ENV=prod ./deploy.sh"}, False, "bash:./deploy.sh:ok"),
        ("Edit", {"file_path": "/p/foo.py"}, False, "edit:.py:ok"),
        ("Edit", {"file_path": "/p/Dockerfile"}, False, "edit::ok"),
        ("Write", {"file_path": "/p/x.md"}, False, "write:.md:ok"),
        ("Read", {"file_path": "/x"}, False, "read:ok"),
        ("Recall", {}, False, "recall:ok"),
    ],
)
def test_pattern_key_shape(tool: str, args: dict, error: bool, expected: str):
    det = PatternDetector()
    assert det._pattern_key(tool, args, error) == expected


def test_bash_empty_command_does_not_crash():
    det = PatternDetector(threshold=3)
    for _ in range(3):
        det.observe("Bash", {}, error=True)
    proposals = det.drain_proposals()
    assert len(proposals) == 1


# ---------- Sample retention ----------


def test_samples_capped_at_three_in_proposal():
    det = PatternDetector(threshold=3)
    for i in range(10):
        det.observe("Bash", {"command": f"pytest -k test_{i}"}, error=True)
    p = det.drain_proposals()[0]
    assert len(p.sample_arguments) == 3
    assert p.count == 10


def test_observe_with_no_arguments_dict():
    """``arguments`` defaults to an empty dict — should still work."""
    det = PatternDetector(threshold=3)
    for _ in range(3):
        det.observe("Recall")
    assert len(det.drain_proposals()) == 1


# ---------- Frozen dataclass discipline ----------


def test_proposal_is_frozen():
    p = SkillDraftProposal(
        pattern_key="x", pattern_summary="x", sample_arguments=(), count=3
    )
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        p.count = 99  # type: ignore[misc]


def test_proposal_summary_for_bash():
    det = PatternDetector(threshold=3)
    for _ in range(5):
        det.observe("Bash", {"command": "pytest"}, error=True)
    p = det.drain_proposals()[0]
    assert "pytest" in p.pattern_summary
    assert "5" in p.pattern_summary
    assert "failed" in p.pattern_summary
