"""TS-T5 — Subdirectory hint discovery tests.

Each test exercises one rule of ``SubdirectoryHintTracker``:

* No hints when there are no hint files in the touched directory.
* ``AGENTS.md`` / ``CLAUDE.md`` / ``OPENCOMPUTER.md`` get loaded on first visit.
* The startup ``working_dir`` is pre-marked so its hints are NOT re-loaded
  (the prompt builder already handed them to the system prompt).
* Each subdirectory's hints load exactly once.
* Tool args walk up ancestors so reading ``project/src/main.py`` finds
  ``project/AGENTS.md``.
* Bash / terminal commands have their path tokens extracted.
* ``OPENCOMPUTER.md`` wins over ``AGENTS.md`` / ``CLAUDE.md`` in the same dir.
"""

from __future__ import annotations

from opencomputer.agent.subdirectory_hints import SubdirectoryHintTracker


def test_no_hints_when_no_md_files(tmp_path):
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "sub"
    sub.mkdir()
    hints = tracker.check_tool_call("Read", {"file_path": str(sub / "file.py")})
    assert hints is None


def test_loads_agents_md_from_subdir(tmp_path):
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "backend"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("# Backend rules\nUse FastAPI.")
    hints = tracker.check_tool_call("Read", {"file_path": str(sub / "main.py")})
    assert hints is not None
    assert "FastAPI" in hints
    assert "backend" in hints  # relative path included


def test_loads_claude_md_from_subdir(tmp_path):
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "frontend"
    sub.mkdir()
    (sub / "CLAUDE.md").write_text("React 18, no class components.")
    hints = tracker.check_tool_call("Read", {"file_path": str(sub / "App.tsx")})
    assert hints is not None
    assert "React 18" in hints


def test_hints_only_loaded_once(tmp_path):
    """Same directory's hints should only be returned the first time."""
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "x"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("rules")
    first = tracker.check_tool_call("Read", {"file_path": str(sub / "f.py")})
    assert first is not None
    second = tracker.check_tool_call("Read", {"file_path": str(sub / "g.py")})
    assert second is None  # already loaded


def test_working_dir_pre_marked(tmp_path):
    """The startup CWD's hints are NOT re-loaded (handled by prompt builder)."""
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    (tmp_path / "AGENTS.md").write_text("root rules")
    hints = tracker.check_tool_call("Read", {"file_path": str(tmp_path / "main.py")})
    assert hints is None  # CWD already loaded


def test_walks_up_ancestors(tmp_path):
    """Reading project/src/main.py discovers project/AGENTS.md even when src/ has none."""
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("project-wide rules")
    src = project / "src"
    src.mkdir()
    hints = tracker.check_tool_call("Read", {"file_path": str(src / "main.py")})
    assert hints is not None
    assert "project-wide" in hints


def test_extracts_paths_from_terminal_command(tmp_path):
    """Terminal/Bash commands have their path tokens extracted."""
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "scripts"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("scripts rules")
    hints = tracker.check_tool_call("Bash", {"command": "ls scripts/build.sh"})
    assert hints is not None
    assert "scripts rules" in hints


def test_opencomputer_md_takes_priority(tmp_path):
    """When OPENCOMPUTER.md and AGENTS.md coexist, OPENCOMPUTER.md wins (V3.A-T8 convention)."""
    tracker = SubdirectoryHintTracker(working_dir=str(tmp_path))
    sub = tmp_path / "service"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("agents-md content")
    (sub / "OPENCOMPUTER.md").write_text("opencomputer-md content")
    hints = tracker.check_tool_call("Read", {"file_path": str(sub / "main.py")})
    assert hints is not None
    # First-match-wins => OPENCOMPUTER.md content present, AGENTS.md content absent
    assert "opencomputer-md content" in hints
    assert "agents-md content" not in hints
