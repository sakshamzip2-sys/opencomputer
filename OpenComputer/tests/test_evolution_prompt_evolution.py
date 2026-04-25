"""Tests for opencomputer.evolution.prompt_evolution.PromptEvolver."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from opencomputer.evolution.prompt_evolution import PromptEvolver, PromptProposal
from opencomputer.evolution.reflect import Insight
from opencomputer.evolution.storage import apply_pending

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_edit_insight(
    target: str = "system",
    diff_hint: str = "Add better error handling guidance",
    confidence: float = 0.8,
) -> Insight:
    return Insight(
        observation="Errors are not handled well",
        evidence_refs=(1, 2),
        action_type="edit_prompt",
        payload={"target": target, "diff_hint": diff_hint},
        confidence=confidence,
    )


@pytest.fixture()
def isolated_evolver(tmp_path, monkeypatch):
    """Return a PromptEvolver wired to an isolated tmp environment."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    # Pre-create and migrate the DB so storage helpers work without the CLI.
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir(parents=True, exist_ok=True)
    db_path = evo_dir / "trajectory.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    conn.close()

    diff_dir = evo_dir / "prompt_proposals"
    return PromptEvolver(dest_dir=diff_dir)


# ---------------------------------------------------------------------------
# propose() — happy path
# ---------------------------------------------------------------------------


def test_propose_returns_prompt_proposal(isolated_evolver):
    insight = _make_edit_insight()
    proposal = isolated_evolver.propose(insight)
    assert isinstance(proposal, PromptProposal)
    assert proposal.id > 0
    assert proposal.target == "system"
    assert proposal.diff_hint == "Add better error handling guidance"
    assert proposal.status == "pending"
    assert proposal.decided_at is None
    assert proposal.decided_reason is None


def test_propose_writes_sidecar_diff_file(isolated_evolver, tmp_path):
    insight = _make_edit_insight(diff_hint="My specific diff hint")
    proposal = isolated_evolver.propose(insight)

    diff_dir = tmp_path / "evolution" / "prompt_proposals"
    sidecar = diff_dir / f"{proposal.id}.diff"
    assert sidecar.exists(), f"Expected sidecar at {sidecar}"
    assert sidecar.read_text(encoding="utf-8") == "My specific diff hint"


def test_propose_no_tmp_file_lingers(isolated_evolver, tmp_path):
    """After a successful propose, the .diff.tmp file must not remain."""
    insight = _make_edit_insight()
    proposal = isolated_evolver.propose(insight)

    diff_dir = tmp_path / "evolution" / "prompt_proposals"
    tmp_file = diff_dir / f"{proposal.id}.diff.tmp"
    assert not tmp_file.exists(), "Temporary file should be gone after atomic replace"


def test_propose_tool_spec_target(isolated_evolver):
    insight = _make_edit_insight(target="tool_spec", diff_hint="Clarify tool parameters")
    proposal = isolated_evolver.propose(insight)
    assert proposal.target == "tool_spec"


# ---------------------------------------------------------------------------
# propose() — rejection / validation paths
# ---------------------------------------------------------------------------


def test_propose_rejects_non_edit_prompt_action(isolated_evolver):
    insight = Insight(
        observation="Pattern found",
        evidence_refs=(),
        action_type="create_skill",
        payload={"slug": "foo", "draft_text": "bar"},
        confidence=0.7,
    )
    with pytest.raises(ValueError, match="action_type='edit_prompt'"):
        isolated_evolver.propose(insight)


def test_propose_rejects_empty_target(isolated_evolver):
    """target must be non-empty; unknown targets are allowed (no cache warning)."""
    insight = Insight(
        observation="Obs",
        evidence_refs=(),
        action_type="edit_prompt",
        payload={"target": "", "diff_hint": "some hint"},
        confidence=0.5,
    )
    with pytest.raises(ValueError, match="payload.target"):
        isolated_evolver.propose(insight)


def test_propose_rejects_empty_diff_hint(isolated_evolver):
    insight = Insight(
        observation="Obs",
        evidence_refs=(),
        action_type="edit_prompt",
        payload={"target": "system", "diff_hint": ""},
        confidence=0.5,
    )
    with pytest.raises(ValueError, match="diff_hint"):
        isolated_evolver.propose(insight)


def test_propose_rejects_whitespace_only_diff_hint(isolated_evolver):
    insight = Insight(
        observation="Obs",
        evidence_refs=(),
        action_type="edit_prompt",
        payload={"target": "system", "diff_hint": "   "},
        confidence=0.5,
    )
    with pytest.raises(ValueError, match="diff_hint"):
        isolated_evolver.propose(insight)


# ---------------------------------------------------------------------------
# apply() / reject()
# ---------------------------------------------------------------------------


def test_apply_marks_status(isolated_evolver):
    proposal = isolated_evolver.propose(_make_edit_insight())
    updated = isolated_evolver.apply(proposal.id, reason="LGTM")
    assert updated.status == "applied"
    assert updated.decided_reason == "LGTM"
    assert updated.decided_at is not None


def test_reject_marks_status(isolated_evolver):
    proposal = isolated_evolver.propose(_make_edit_insight())
    updated = isolated_evolver.reject(proposal.id, reason="Too broad")
    assert updated.status == "rejected"
    assert "Too broad" in (updated.decided_reason or "")


# ---------------------------------------------------------------------------
# list_pending / list_all / get
# ---------------------------------------------------------------------------


def test_list_pending_filters_pending_only(isolated_evolver):
    p1 = isolated_evolver.propose(_make_edit_insight(diff_hint="hint1"))
    p2 = isolated_evolver.propose(_make_edit_insight(diff_hint="hint2"))
    isolated_evolver.apply(p1.id)

    pending = isolated_evolver.list_pending()
    pending_ids = [p.id for p in pending]
    assert p2.id in pending_ids
    assert p1.id not in pending_ids


def test_list_all_includes_all_statuses(isolated_evolver):
    p1 = isolated_evolver.propose(_make_edit_insight(diff_hint="h1"))
    p2 = isolated_evolver.propose(_make_edit_insight(diff_hint="h2"))
    isolated_evolver.apply(p2.id)

    all_proposals = isolated_evolver.list_all()
    ids = [p.id for p in all_proposals]
    assert p1.id in ids
    assert p2.id in ids


def test_get_raises_key_error_on_missing(isolated_evolver):
    with pytest.raises(KeyError, match="No prompt proposal with id=99999"):
        isolated_evolver.get(99999)
