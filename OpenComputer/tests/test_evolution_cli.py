"""CLI tests for ``opencomputer evolution …`` subcommands.

Each test uses an isolated OPENCOMPUTER_HOME (via ``monkeypatch.setenv``)
so the CLI operates against a fresh tmp dir instead of the real user profile.

Provider-related tests (reflect without --dry-run) are left for post-MVP
integration tests — B2 reflects with --dry-run or no trajectories, which
avoids the need for a live provider.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.evolution.entrypoint import evolution_app
from opencomputer.evolution.storage import apply_pending, insert_record
from opencomputer.evolution.trajectory import TrajectoryEvent, TrajectoryRecord

runner = CliRunner()

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    """Set OPENCOMPUTER_HOME to a fresh tmp dir and return it."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_skill_md(path: Path, description: str = "A test skill") -> None:
    """Write a minimal SKILL.md with frontmatter into *path* (creates parents)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nslug: {path.parent.name}\ndescription: {description}\n---\n\n# Body\n",
        encoding="utf-8",
    )


def _make_db_with_records(db_path: Path, count: int = 2) -> list[int]:
    """Create the evolution DB at *db_path* and insert *count* records. Returns record ids."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)

    ids = []
    for i in range(count):
        ev = TrajectoryEvent(
            session_id=f"s{i}",
            message_id=None,
            action_type="tool_call",
            tool_name="Read",
            outcome="success",
            timestamp=float(i),
            metadata={},
        )
        rec = TrajectoryRecord(
            id=None,
            session_id=f"s{i}",
            schema_version=1,
            started_at=float(i),
            ended_at=float(i) + 10.0,
            events=(ev,),
            completion_flag=True,
        )
        record_id = insert_record(rec, conn=conn)
        ids.append(record_id)

    conn.close()
    return ids


# ---------------------------------------------------------------------------
# 1. test_evolution_no_args_shows_help
# ---------------------------------------------------------------------------


def test_evolution_no_args_shows_help(isolated_home):
    """Invoking the subapp with no args shows help text.

    Typer 0.24+ with ``no_args_is_help=True`` on a subcommand group returns
    exit code 0 or 2 depending on the Click version; we only assert that
    help content is shown, which is the meaningful observable behaviour.
    """
    result = runner.invoke(evolution_app, [])
    # Help is printed; exit code may be 0 or 2 depending on Typer/Click version
    assert result.exit_code in (0, 2)
    assert "Self-improvement" in result.output


# ---------------------------------------------------------------------------
# 2. test_skills_list_empty
# ---------------------------------------------------------------------------


def test_skills_list_empty(isolated_home):
    """Fresh home → skills list exits 0 and reports no skills."""
    result = runner.invoke(evolution_app, ["skills", "list"])
    assert result.exit_code == 0
    assert "No synthesized skills yet" in result.output


# ---------------------------------------------------------------------------
# 3. test_skills_list_with_synthesized_skill
# ---------------------------------------------------------------------------


def test_skills_list_with_synthesized_skill(isolated_home):
    """Pre-seeded skill appears in the skills list table."""
    skill_dir = isolated_home / "evolution" / "skills" / "test-slug"
    _make_skill_md(skill_dir / "SKILL.md", description="A synthesized skill")

    result = runner.invoke(evolution_app, ["skills", "list"])
    assert result.exit_code == 0
    assert "test-slug" in result.output
    assert "A synthesized skill" in result.output


# ---------------------------------------------------------------------------
# 4. test_skills_promote_missing_slug_errors
# ---------------------------------------------------------------------------


def test_skills_promote_missing_slug_errors(isolated_home):
    """Promoting a non-existent slug exits 1 and says 'not found'."""
    result = runner.invoke(evolution_app, ["skills", "promote", "nonexistent"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower() or "not found" in (result.stderr or "").lower()


# ---------------------------------------------------------------------------
# 5. test_skills_promote_copies_to_main
# ---------------------------------------------------------------------------


def test_skills_promote_copies_to_main(isolated_home):
    """Promoting a synthesized skill copies it to the main skills dir."""
    slug = "test-slug"
    evo_skill = isolated_home / "evolution" / "skills" / slug
    _make_skill_md(evo_skill / "SKILL.md", description="Promoted skill")

    result = runner.invoke(evolution_app, ["skills", "promote", slug])
    assert result.exit_code == 0, result.output

    main_skill_md = isolated_home / "skills" / slug / "SKILL.md"
    assert main_skill_md.exists(), f"Expected {main_skill_md} to exist after promote"


# ---------------------------------------------------------------------------
# 6. test_skills_promote_refuses_overwrite_without_force
# ---------------------------------------------------------------------------


def test_skills_promote_refuses_overwrite_without_force(isolated_home):
    """Promoting when a main-skills entry already exists → exit 1 without --force."""
    slug = "test-slug"
    evo_skill = isolated_home / "evolution" / "skills" / slug
    _make_skill_md(evo_skill / "SKILL.md", description="Evolution version")

    # Pre-seed main skills entry
    main_skill = isolated_home / "skills" / slug
    _make_skill_md(main_skill / "SKILL.md", description="Main version")

    result = runner.invoke(evolution_app, ["skills", "promote", slug])
    assert result.exit_code == 1
    # Content of main skills should remain unchanged
    assert (main_skill / "SKILL.md").read_text(encoding="utf-8").find("Main version") != -1


# ---------------------------------------------------------------------------
# 7. test_skills_promote_force_overwrites
# ---------------------------------------------------------------------------


def test_skills_promote_force_overwrites(isolated_home):
    """Promoting with --force overwrites an existing main-skills entry."""
    slug = "test-slug"
    evo_skill = isolated_home / "evolution" / "skills" / slug
    _make_skill_md(evo_skill / "SKILL.md", description="Evolution version")

    # Pre-seed main skills entry with different content
    main_skill = isolated_home / "skills" / slug
    _make_skill_md(main_skill / "SKILL.md", description="Old main version")

    result = runner.invoke(evolution_app, ["skills", "promote", slug, "--force"])
    assert result.exit_code == 0, result.output

    updated = (main_skill / "SKILL.md").read_text(encoding="utf-8")
    assert "Evolution version" in updated


# ---------------------------------------------------------------------------
# 8. test_reflect_no_trajectories_message
# ---------------------------------------------------------------------------


def test_reflect_no_trajectories_message(isolated_home):
    """reflect with no trajectories in DB → informative message, exit 0."""
    result = runner.invoke(evolution_app, ["reflect"])
    assert result.exit_code == 0
    assert "No trajectories to reflect on" in result.output


# ---------------------------------------------------------------------------
# 9. test_reflect_dry_run_shows_table
# ---------------------------------------------------------------------------


def test_reflect_dry_run_shows_table(isolated_home):
    """reflect --dry-run with records shows the trajectory table and dry-run note."""
    # Bootstrap the DB in the isolated home
    evo_dir = isolated_home / "evolution"
    evo_dir.mkdir(parents=True, exist_ok=True)
    db_path = evo_dir / "trajectory.sqlite"
    inserted_ids = _make_db_with_records(db_path, count=2)

    result = runner.invoke(evolution_app, ["reflect", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Dry-run: no LLM call made" in result.output
    # Both record ids should appear in the table
    for rid in inserted_ids:
        assert str(rid) in result.output


# ---------------------------------------------------------------------------
# 10. test_reset_no_data_message
# ---------------------------------------------------------------------------


def test_reset_no_data_message(isolated_home):
    """reset --yes on a fresh home prints 'No evolution data to delete', exit 0."""
    result = runner.invoke(evolution_app, ["reset", "--yes"])
    assert result.exit_code == 0
    assert "No evolution data to delete" in result.output


# ---------------------------------------------------------------------------
# 11. test_reset_with_data_deletes
# ---------------------------------------------------------------------------


def test_reset_with_data_deletes(isolated_home):
    """reset --yes removes the evolution directory entirely."""
    evo_skill = isolated_home / "evolution" / "skills" / "foo"
    _make_skill_md(evo_skill / "SKILL.md", description="Temp skill")

    evo_dir = isolated_home / "evolution"
    assert evo_dir.exists()

    result = runner.invoke(evolution_app, ["reset", "--yes"])
    assert result.exit_code == 0, result.output
    assert not evo_dir.exists(), "evolution dir should have been deleted"


# ---------------------------------------------------------------------------
# 12. test_reset_without_yes_prompts_and_can_cancel
# ---------------------------------------------------------------------------


def test_reset_without_yes_prompts_and_can_cancel(isolated_home):
    """reset without --yes prompts; answering 'n' cancels and data is preserved."""
    evo_skill = isolated_home / "evolution" / "skills" / "bar"
    _make_skill_md(evo_skill / "SKILL.md", description="Protected skill")

    evo_dir = isolated_home / "evolution"
    assert evo_dir.exists()

    # Simulate user typing "n" + Enter at the confirmation prompt
    result = runner.invoke(evolution_app, ["reset"], input="n\n")
    assert result.exit_code == 0
    # Data must still be there
    assert evo_dir.exists(), "evolution dir should NOT have been deleted after cancel"
