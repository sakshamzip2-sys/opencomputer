"""B4 CLI tests for opencomputer evolution subcommands.

Covers: prompts list/apply/reject, dashboard, skills retire (+ collision),
skills record-invocation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.evolution.entrypoint import evolution_app
from opencomputer.evolution.reflect import Insight
from opencomputer.evolution.storage import (
    apply_pending,
    list_prompt_proposals,
    list_skill_invocations,
    record_reflection,
    record_skill_invocation,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    """Set OPENCOMPUTER_HOME to an isolated tmp dir; pre-create evolution DB."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    evo_dir = tmp_path / "evolution"
    evo_dir.mkdir(parents=True, exist_ok=True)
    db_path = evo_dir / "trajectory.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    conn.close()
    return tmp_path


def _make_skill_md(path: Path, description: str = "A test skill") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nslug: {path.parent.name}\ndescription: {description}\n---\n\n# Body\n",
        encoding="utf-8",
    )


def _seed_proposal(isolated_home: Path, diff_hint: str = "Change something") -> int:
    """Seed a prompt proposal via PromptEvolver and return the id."""
    from opencomputer.evolution.prompt_evolution import PromptEvolver

    diff_dir = isolated_home / "evolution" / "prompt_proposals"
    pe = PromptEvolver(dest_dir=diff_dir)
    insight = Insight(
        observation="Observed a pattern",
        evidence_refs=(1,),
        action_type="edit_prompt",
        payload={"target": "system", "diff_hint": diff_hint},
        confidence=0.75,
    )
    proposal = pe.propose(insight)
    return proposal.id


# ---------------------------------------------------------------------------
# prompts list
# ---------------------------------------------------------------------------


def test_prompts_list_empty_pending(isolated_home):
    result = runner.invoke(evolution_app, ["prompts", "list"])
    assert result.exit_code == 0
    assert "pending" in result.output.lower() or "No prompt proposals" in result.output


def test_prompts_list_shows_pending_proposals(isolated_home):
    _seed_proposal(isolated_home, diff_hint="Important change")
    result = runner.invoke(evolution_app, ["prompts", "list"])
    assert result.exit_code == 0
    assert "Important change" in result.output or "pending" in result.output.lower()


def test_prompts_list_all_shows_applied(isolated_home):
    """After applying a proposal, `prompts list --status all` shows it."""
    pid = _seed_proposal(isolated_home, diff_hint="Applied hint")
    # Apply it via CLI
    runner.invoke(evolution_app, ["prompts", "apply", str(pid)])
    result = runner.invoke(evolution_app, ["prompts", "list", "--status", "all"])
    assert result.exit_code == 0
    assert "applied" in result.output.lower() or "Applied hint" in result.output


def test_prompts_list_status_filter_pending_only(isolated_home):
    """list with default --status pending should not show applied proposals."""
    pid = _seed_proposal(isolated_home, diff_hint="Will be applied")
    _seed_proposal(isolated_home, diff_hint="Still pending")
    runner.invoke(evolution_app, ["prompts", "apply", str(pid)])

    result = runner.invoke(evolution_app, ["prompts", "list", "--status", "pending"])
    assert result.exit_code == 0
    # The applied one should not appear in pending list
    assert "Will be applied" not in result.output or "applied" not in result.output.lower()


# ---------------------------------------------------------------------------
# prompts apply
# ---------------------------------------------------------------------------


def test_prompts_apply_marks_applied(isolated_home):
    pid = _seed_proposal(isolated_home)
    result = runner.invoke(evolution_app, ["prompts", "apply", str(pid)])
    assert result.exit_code == 0
    assert "applied" in result.output.lower()


def test_prompts_apply_persists_to_db(isolated_home):
    pid = _seed_proposal(isolated_home)
    runner.invoke(evolution_app, ["prompts", "apply", str(pid), "--reason", "good change"])

    db_path = isolated_home / "evolution" / "trajectory.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = list_prompt_proposals(conn=conn)
    conn.close()
    row = next(r for r in rows if r["id"] == pid)
    assert row["status"] == "applied"
    assert row["decided_reason"] == "good change"


def test_prompts_apply_missing_id_exits_1(isolated_home):
    result = runner.invoke(evolution_app, ["prompts", "apply", "99999"])
    assert result.exit_code == 1
    assert "No proposal" in result.output


# ---------------------------------------------------------------------------
# prompts reject
# ---------------------------------------------------------------------------


def test_prompts_reject_marks_rejected(isolated_home):
    pid = _seed_proposal(isolated_home)
    result = runner.invoke(evolution_app, ["prompts", "reject", str(pid)])
    assert result.exit_code == 0
    assert "rejected" in result.output.lower() or "Rejected" in result.output


def test_prompts_reject_missing_id_exits_1(isolated_home):
    result = runner.invoke(evolution_app, ["prompts", "reject", "88888"])
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


def test_dashboard_empty_home_renders(isolated_home):
    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "Evolution dashboard" in result.output
    assert "total reflections" in result.output


def test_dashboard_shows_atrophy_with_seeded_old_invocation(isolated_home):
    """Seed a skill with an old invocation; dashboard should show atrophied=1."""
    import time

    skills_dir = isolated_home / "evolution" / "skills"
    _make_skill_md(skills_dir / "old-skill" / "SKILL.md", description="Old skill")

    db_path = isolated_home / "evolution" / "trajectory.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    old_ts = time.time() - (100 * 24 * 3600)
    record_skill_invocation("old-skill", invoked_at=old_ts, conn=conn)
    conn.close()

    result = runner.invoke(evolution_app, ["dashboard"])
    assert result.exit_code == 0
    assert "atrophied" in result.output.lower()


# ---------------------------------------------------------------------------
# skills retire
# ---------------------------------------------------------------------------


def test_skills_retire_moves_skill(isolated_home):
    """--yes skips confirmation; skill moves to retired/."""
    skill_dir = isolated_home / "evolution" / "skills" / "bye-skill"
    _make_skill_md(skill_dir / "SKILL.md")

    result = runner.invoke(evolution_app, ["skills", "retire", "bye-skill", "--yes"])
    assert result.exit_code == 0
    assert not skill_dir.exists(), "skill dir should be gone from quarantine"
    retired = isolated_home / "evolution" / "retired" / "bye-skill"
    assert retired.exists(), "skill should be in retired/"


def test_skills_retire_missing_slug_exits_1(isolated_home):
    result = runner.invoke(evolution_app, ["skills", "retire", "no-such-skill", "--yes"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower() or "Skill not found" in result.output


def test_skills_retire_collision_handling(isolated_home):
    """Retiring the same slug twice lands the second one at retired/slug-2/."""
    for _ in range(2):
        # Re-create the skill dir each time (first retire removes it)
        skill_dir = isolated_home / "evolution" / "skills" / "dup-skill"
        _make_skill_md(skill_dir / "SKILL.md")
        runner.invoke(evolution_app, ["skills", "retire", "dup-skill", "--yes"])

    retired = isolated_home / "evolution" / "retired"
    assert (retired / "dup-skill").exists()
    assert (retired / "dup-skill-2").exists()


def test_skills_retire_cancel_keeps_skill(isolated_home):
    """Without --yes and answering 'n', skill should remain."""
    skill_dir = isolated_home / "evolution" / "skills" / "keep-skill"
    _make_skill_md(skill_dir / "SKILL.md")

    result = runner.invoke(evolution_app, ["skills", "retire", "keep-skill"], input="n\n")
    assert result.exit_code == 0
    assert skill_dir.exists(), "skill should not have been moved after cancel"


# ---------------------------------------------------------------------------
# skills record-invocation
# ---------------------------------------------------------------------------


def test_skills_record_invocation_writes_row(isolated_home):
    result = runner.invoke(
        evolution_app, ["skills", "record-invocation", "my-skill", "--source", "agent_loop"]
    )
    assert result.exit_code == 0
    assert "my-skill" in result.output

    db_path = isolated_home / "evolution" / "trajectory.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    rows = list_skill_invocations(slug="my-skill", conn=conn)
    conn.close()
    assert len(rows) == 1
    assert rows[0]["source"] == "agent_loop"
