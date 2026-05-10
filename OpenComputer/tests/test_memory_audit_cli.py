"""Tests for the `opencomputer memory audit` CLI subcommand.

Part of M3 of the 2026-05-10 memory-observability design. The audit command does
per-paragraph inspection of MEMORY.md / USER.md (read-only by default; interactive write
path lands in M4). Distinct from the multi-layer health command at `cli_memory.py:629`
which is named `doctor`.

Tests use Typer's `CliRunner` to exercise the command end-to-end.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from opencomputer.cli_memory import memory_app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_memory_files(tmp_path, monkeypatch):
    """Create MEMORY.md + USER.md fixtures and point the CLI at them.

    The audit command resolves files via `_manager()` → `load_config()`. We
    monkeypatch the manager factory to return a real MemoryManager rooted in
    tmp_path so we don't depend on the user's actual ~/.opencomputer/.
    """
    from opencomputer import cli_memory
    from opencomputer.agent.memory import MemoryManager

    decl = tmp_path / "MEMORY.md"
    user = tmp_path / "USER.md"
    skills = tmp_path / "skills"
    skills.mkdir()

    decl.write_text(
        "alpha durable rule about TDD\n\n"
        "TODO: figure out the X part of Y\n\n"
        "gamma rule that's load-bearing\n",
        encoding="utf-8",
    )
    user.write_text(
        "user prefers concise output\n\n"
        "user works in Asia/Kolkata timezone\n",
        encoding="utf-8",
    )

    mm = MemoryManager(
        declarative_path=decl,
        user_path=user,
        skills_path=skills,
        memory_char_limit=4000,
        user_char_limit=2000,
    )

    def fake_manager() -> MemoryManager:
        return mm

    monkeypatch.setattr(cli_memory, "_manager", fake_manager)
    return decl, user, mm


class TestAuditReadOnly:
    def test_audit_default_is_memory_md(self, runner, fake_memory_files):
        decl, _user, _mm = fake_memory_files
        result = runner.invoke(memory_app, ["audit"])
        assert result.exit_code == 0, result.output
        # Output should mention the file and its cap pct
        assert "MEMORY.md" in result.output

    def test_audit_lists_paragraphs_with_indices(self, runner, fake_memory_files):
        result = runner.invoke(memory_app, ["audit"])
        assert result.exit_code == 0
        # Three paragraphs in the fixture; their indices should appear
        # somewhere in the output (e.g. "[1]" "[2]" "[3]" or "1." "2." "3.")
        text = result.output
        # Be flexible on exact format; require all three numbers present
        assert "1" in text and "2" in text and "3" in text
        # Content snippets present
        assert "alpha" in text
        assert "TODO" in text
        assert "gamma" in text

    def test_audit_flags_todo_marker(self, runner, fake_memory_files):
        result = runner.invoke(memory_app, ["audit"])
        assert result.exit_code == 0
        # The middle paragraph contains "TODO" — the audit should flag it
        assert "TODO" in result.output
        # The flag annotation is present (we accept various phrasings)
        assert "[TODO]" in result.output or "todo" in result.output.lower()

    def test_audit_user_flag_targets_user_md(self, runner, fake_memory_files):
        result = runner.invoke(memory_app, ["audit", "--user"])
        assert result.exit_code == 0
        assert "USER.md" in result.output
        assert "Asia/Kolkata" in result.output

    def test_audit_all_lists_both(self, runner, fake_memory_files):
        result = runner.invoke(memory_app, ["audit", "--all"])
        assert result.exit_code == 0
        assert "MEMORY.md" in result.output
        assert "USER.md" in result.output

    def test_audit_includes_cap_pct(self, runner, fake_memory_files):
        result = runner.invoke(memory_app, ["audit"])
        assert result.exit_code == 0
        # Either a "X%" or "X/Y chars" should appear
        assert "%" in result.output or "chars" in result.output.lower()

    def test_audit_missing_file_exits_cleanly(self, runner, tmp_path, monkeypatch):
        from opencomputer import cli_memory
        from opencomputer.agent.memory import MemoryManager

        skills = tmp_path / "skills"
        skills.mkdir()
        # No MEMORY.md / USER.md created
        mm = MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=skills,
        )
        monkeypatch.setattr(cli_memory, "_manager", lambda: mm)

        result = runner.invoke(memory_app, ["audit"])
        # Should NOT crash — exit cleanly, possibly with "empty" or "(empty)" message
        assert result.exit_code == 0
        assert "empty" in result.output.lower() or "0 chars" in result.output.lower()
