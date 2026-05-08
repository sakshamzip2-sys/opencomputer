"""D3: `.hermes.md` is loaded by both startup + subdir-hint pipelines.

Hermes v2 spec lists `.hermes.md` / `HERMES.md` as the highest-priority
context-file name. OC's project-context name is `OPENCOMPUTER.md`, but
users who fork upstream Hermes repos still ship `.hermes.md`; the
loaders now recognize both.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.prompt_builder import load_workspace_context
from opencomputer.agent.subdirectory_hints import SubdirectoryHintTracker


def test_hermes_md_loaded_at_start_dir(tmp_path: Path):
    (tmp_path / ".hermes.md").write_text(
        "# Hermes\n\nUse poetry for deps.\n", encoding="utf-8"
    )
    out = load_workspace_context(start=tmp_path)
    assert "## .hermes.md" in out
    assert "Use poetry for deps." in out


def test_hermes_md_priority_above_claude_and_agents(tmp_path: Path):
    (tmp_path / ".hermes.md").write_text("# Hermes\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    out = load_workspace_context(start=tmp_path)
    h_idx = out.index("## .hermes.md")
    c_idx = out.index("## CLAUDE.md")
    a_idx = out.index("## AGENTS.md")
    assert h_idx < c_idx < a_idx


def test_hermes_md_in_subdir_picked_up_by_tracker(tmp_path: Path):
    sub = tmp_path / "service"
    sub.mkdir()
    (sub / ".hermes.md").write_text(
        "# Service hints\n\nUse pnpm.\n", encoding="utf-8"
    )
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    out = tracker.check_tool_call("Read", {"file_path": str(sub / "main.ts")})
    assert out is not None
    assert "Use pnpm." in out
    assert ".hermes.md" in out


def test_uppercase_hermes_md_picked_up_by_tracker(tmp_path: Path):
    sub = tmp_path / "lib"
    sub.mkdir()
    (sub / "HERMES.md").write_text(
        "# Library hints\n\nNo new abstractions.\n", encoding="utf-8"
    )
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    out = tracker.check_tool_call("Read", {"file_path": str(sub / "core.py")})
    assert out is not None
    assert "No new abstractions." in out
