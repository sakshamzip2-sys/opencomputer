"""V3.A-T8 — workspace context loader.

Finds OPENCOMPUTER.md / CLAUDE.md / AGENTS.md from cwd or any ancestor
and concatenates the contents under file-tagged headers, ready to be
injected into the system prompt's ``{{ workspace_context }}`` slot.

Per-file content is capped at 100KB so a misconfigured workspace file
can't blow the prompt budget. The walker stops at filesystem root or
``max_depth`` levels — whichever comes first.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.prompt_builder import (
    PromptBuilder,
    load_workspace_context,
)


def test_loads_opencomputer_md(tmp_path: Path) -> None:
    (tmp_path / "OPENCOMPUTER.md").write_text("# OC project\nUse python3.13.")
    ctx = load_workspace_context(start=tmp_path)
    assert "python3.13" in ctx
    assert "OPENCOMPUTER.md" in ctx


def test_loads_all_three_when_present(tmp_path: Path) -> None:
    (tmp_path / "OPENCOMPUTER.md").write_text("oc rules")
    (tmp_path / "CLAUDE.md").write_text("claude rules")
    (tmp_path / "AGENTS.md").write_text("agents rules")
    ctx = load_workspace_context(start=tmp_path)
    assert "oc rules" in ctx
    assert "claude rules" in ctx
    assert "agents rules" in ctx


def test_walks_up_to_find_file(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("rules")
    ctx = load_workspace_context(start=nested)
    assert "rules" in ctx


def test_returns_empty_when_no_files(tmp_path: Path) -> None:
    ctx = load_workspace_context(start=tmp_path)
    assert ctx == ""


def test_max_depth_caps_walk(tmp_path: Path) -> None:
    """Don't walk forever — max_depth caps the ancestor walk."""
    nested = tmp_path / "a" / "b" / "c" / "d" / "e" / "f"
    nested.mkdir(parents=True)
    (tmp_path / "CLAUDE.md").write_text("very deep root")
    # nested is 6 levels deep; with max_depth=3 we shouldn't reach root
    ctx = load_workspace_context(start=nested, max_depth=3)
    assert "very deep root" not in ctx


def test_handles_unicode_decode_errors(tmp_path: Path) -> None:
    """A binary file with .md extension shouldn't crash the walk."""
    (tmp_path / "CLAUDE.md").write_bytes(b"\xff\xfe\x00\x00invalid")
    ctx = load_workspace_context(start=tmp_path)
    # Should be loaded with errors='replace', not crash
    assert "CLAUDE.md" in ctx


def test_caps_huge_file_at_100kb(tmp_path: Path) -> None:
    """A 200KB CLAUDE.md gets truncated with a marker, not dumped wholesale."""
    big = "X" * 200_000
    (tmp_path / "CLAUDE.md").write_text(big)
    ctx = load_workspace_context(start=tmp_path)
    # Result must NOT contain the full 200KB payload.
    assert len(ctx) < 110_000
    # The truncation marker should be visible so the agent can ask for more.
    assert "truncated" in ctx.lower()


def test_loads_files_from_multiple_ancestors(tmp_path: Path) -> None:
    """Both ``parent/CLAUDE.md`` and ``cwd/CLAUDE.md`` are different files;
    both should be loaded so closer-to-cwd conventions take precedence in
    the final concatenation order."""
    nested = tmp_path / "child"
    nested.mkdir()
    (tmp_path / "CLAUDE.md").write_text("parent rules")
    (nested / "CLAUDE.md").write_text("child rules")
    ctx = load_workspace_context(start=nested)
    assert "child rules" in ctx
    assert "parent rules" in ctx
    # Closer-to-cwd file appears first.
    assert ctx.index("child rules") < ctx.index("parent rules")


def test_workspace_context_flows_into_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: cwd has CLAUDE.md → prompt includes its content."""
    (tmp_path / "CLAUDE.md").write_text("Project rules: use type hints.")
    monkeypatch.chdir(tmp_path)

    pb = PromptBuilder()
    ws_ctx = load_workspace_context()
    rendered = pb.build(workspace_context=ws_ctx)
    assert "use type hints" in rendered
