"""Tests for `oc memory audit --interactive`.

Part of M4 of the 2026-05-10 memory-observability design. Walks each paragraph
prompting keep/delete/replace/skip; delegates writes to existing
`MemoryManager.remove_*` / `replace_*` paths so locking, backup, and event
publication chain are reused.

Tests feed stdin via Typer's CliRunner.invoke(input="..."). Each line of stdin
satisfies one prompt; we account for the fact that sequential calls to
`typer.prompt` consume one line each.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opencomputer.cli_memory import memory_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fixture_files(tmp_path, monkeypatch):
    from opencomputer import cli_memory
    from opencomputer.agent.memory import MemoryManager

    decl = tmp_path / "MEMORY.md"
    user = tmp_path / "USER.md"
    skills = tmp_path / "skills"
    skills.mkdir()

    decl.write_text(
        "alpha first paragraph\n\n"
        "beta second paragraph TODO\n\n"
        "gamma third paragraph\n",
        encoding="utf-8",
    )
    user.write_text("user paragraph one\n", encoding="utf-8")

    mm = MemoryManager(
        declarative_path=decl,
        user_path=user,
        skills_path=skills,
        memory_char_limit=4000,
        user_char_limit=2000,
    )
    monkeypatch.setattr(cli_memory, "_manager", lambda: mm)
    return decl, user, mm


class TestInteractiveKeep:
    def test_keep_all_leaves_file_unchanged(self, runner, fixture_files):
        decl, _user, mm = fixture_files
        before = decl.read_text(encoding="utf-8")
        # Three paragraphs → three "k" responses
        result = runner.invoke(memory_app, ["audit", "--interactive"], input="k\nk\nk\n")
        assert result.exit_code == 0, result.output
        after = decl.read_text(encoding="utf-8")
        assert after == before


class TestInteractiveSkip:
    def test_skip_is_idempotent(self, runner, fixture_files):
        decl, _user, _mm = fixture_files
        before = decl.read_text(encoding="utf-8")
        result = runner.invoke(memory_app, ["audit", "--interactive"], input="s\ns\ns\n")
        assert result.exit_code == 0, result.output
        assert decl.read_text(encoding="utf-8") == before


class TestInteractiveDelete:
    def test_delete_removes_paragraph(self, runner, fixture_files):
        decl, _user, mm = fixture_files
        # Walk: keep first, delete second (the TODO one), keep third
        result = runner.invoke(memory_app, ["audit", "--interactive"], input="k\nd\nk\n")
        assert result.exit_code == 0, result.output
        body = mm.read_declarative()
        assert "alpha first paragraph" in body
        assert "gamma third paragraph" in body
        assert "beta second paragraph TODO" not in body


class TestInteractiveReplace:
    def test_replace_swaps_paragraph(self, runner, fixture_files):
        decl, _user, mm = fixture_files
        # Walk: keep, replace (with replacement text), keep
        # Sequence: action, then replacement-text prompt
        result = runner.invoke(
            memory_app,
            ["audit", "--interactive"],
            input="k\nr\nbeta TODO resolved\nk\n",
        )
        assert result.exit_code == 0, result.output
        body = mm.read_declarative()
        assert "alpha first paragraph" in body
        assert "gamma third paragraph" in body
        assert "beta second paragraph TODO" not in body
        assert "beta TODO resolved" in body


class TestInteractiveOnUserMD:
    def test_user_flag_walks_user_md(self, runner, fixture_files):
        _decl, user, mm = fixture_files
        # USER.md has one paragraph; delete it
        result = runner.invoke(
            memory_app, ["audit", "--user", "--interactive"], input="d\n"
        )
        assert result.exit_code == 0, result.output
        assert "user paragraph one" not in mm.read_user()


class TestInteractiveCtrlCAborts:
    def test_unknown_input_skips_paragraph(self, runner, fixture_files):
        """Unknown action like 'x' should be treated as skip, not crash."""
        decl, _user, _mm = fixture_files
        before = decl.read_text(encoding="utf-8")
        result = runner.invoke(
            memory_app, ["audit", "--interactive"], input="x\nx\nx\n"
        )
        assert result.exit_code == 0
        # Unknown actions default to skip → no changes
        assert decl.read_text(encoding="utf-8") == before


class TestInteractiveAllWalksBothFiles:
    """Regression test for the /review-flagged "—all --interactive walks only first
    file" claim. Verifies that --all --interactive iterates over BOTH MEMORY.md
    AND USER.md, prompting for paragraphs in each.
    """

    def test_all_interactive_walks_memory_then_user(self, runner, fixture_files):
        _decl, _user, mm = fixture_files
        # Fixture: MEMORY.md has 3 paragraphs, USER.md has 1 paragraph
        # Walk:
        #   MEMORY.md → keep, delete (the TODO one), keep
        #   USER.md → delete the only paragraph
        # Total stdin lines: 4 actions
        result = runner.invoke(
            memory_app,
            ["audit", "--all", "--interactive"],
            input="k\nd\nk\nd\n",
        )
        assert result.exit_code == 0, result.output
        # MEMORY.md: TODO paragraph deleted, others kept
        body = mm.read_declarative()
        assert "alpha first paragraph" in body
        assert "gamma third paragraph" in body
        assert "beta second paragraph TODO" not in body
        # USER.md: paragraph deleted
        assert "user paragraph one" not in mm.read_user()
