"""Tests for user-authored markdown slash commands (Recipe 1).

``opencomputer.agent.markdown_commands`` scans up to three directories
for ``*.md`` files and turns each into a slash command whose body is the
prompt template. This mirrors Claude Code's ``~/.claude/commands/`` —
zero Python, zero restart.

Conflict policy (port plan R1.6): project > per-profile > global, and a
markdown command may shadow a built-in. Every override logs a WARNING.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.markdown_commands import (
    MAX_BODY_BYTES,
    MarkdownCommand,
    discover_markdown_commands,
    render_command_body,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ── discovery ────────────────────────────────────────────────────────


def test_discovers_md_file_in_profile_commands_dir(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    _write(profile / "commands" / "tldr.md", "Summarize in 3 bullets.")
    cmds = discover_markdown_commands(profile, global_root=tmp_path / "empty")
    assert [c.name for c in cmds] == ["tldr"]
    assert cmds[0].body == "Summarize in 3 bullets."
    assert cmds[0].source_path == profile / "commands" / "tldr.md"


def test_missing_dir_yields_nothing(tmp_path: Path) -> None:
    assert discover_markdown_commands(
        tmp_path / "nope", global_root=tmp_path / "also-nope"
    ) == []


def test_frontmatter_description_is_parsed(tmp_path: Path) -> None:
    profile = tmp_path / "p"
    _write(
        profile / "commands" / "review.md",
        "---\ndescription: Review the diff\nargs_hint: \"[path]\"\n"
        "category: dev\n---\nReview {{args}}.",
    )
    (cmd,) = discover_markdown_commands(profile, global_root=tmp_path / "g")
    assert cmd.description == "Review the diff"
    assert cmd.args_hint == "[path]"
    assert cmd.category == "dev"
    assert cmd.body == "Review {{args}}."


def test_frontmatter_model_override_and_tools(tmp_path: Path) -> None:
    profile = tmp_path / "p"
    _write(
        profile / "commands" / "deploy.md",
        "---\nmodel_override: claude-opus-4-7\ntools: [Bash, Read]\n---\nShip it.",
    )
    (cmd,) = discover_markdown_commands(profile, global_root=tmp_path / "g")
    assert cmd.model_override == "claude-opus-4-7"
    assert cmd.tools == ("Bash", "Read")


# ── conflict policy ──────────────────────────────────────────────────


def test_profile_command_shadows_global(tmp_path: Path) -> None:
    global_root = tmp_path / "global"
    profile = tmp_path / "profile"
    _write(global_root / "commands" / "tldr.md", "GLOBAL body")
    _write(profile / "commands" / "tldr.md", "PROFILE body")
    cmds = discover_markdown_commands(profile, global_root=global_root)
    by_name = {c.name: c for c in cmds}
    assert by_name["tldr"].body == "PROFILE body"


def test_project_command_shadows_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    project = tmp_path / "project"
    _write(profile / "commands" / "build.md", "PROFILE body")
    _write(project / ".opencomputer" / "commands" / "build.md", "PROJECT body")
    cmds = discover_markdown_commands(
        profile, global_root=tmp_path / "g", project_cwd=project
    )
    by_name = {c.name: c for c in cmds}
    assert by_name["build"].body == "PROJECT body"


def test_project_dir_ignored_when_cwd_not_passed(tmp_path: Path) -> None:
    project = tmp_path / "project"
    _write(project / ".opencomputer" / "commands" / "x.md", "body")
    cmds = discover_markdown_commands(tmp_path / "p", global_root=tmp_path / "g")
    assert cmds == []


def test_default_profile_does_not_double_register(tmp_path: Path) -> None:
    """When profile_home == global_root (the 'default' profile) the same
    dir must not yield two MarkdownCommand entries for one file."""
    root = tmp_path / "root"
    _write(root / "commands" / "tldr.md", "body")
    cmds = discover_markdown_commands(root, global_root=root)
    assert [c.name for c in cmds] == ["tldr"]


# ── malformed / hostile input ────────────────────────────────────────


def test_malformed_frontmatter_skips_file_keeps_siblings(tmp_path: Path) -> None:
    profile = tmp_path / "p"
    _write(profile / "commands" / "bad.md", "---\n: : not yaml : :\n---\nbody")
    _write(profile / "commands" / "good.md", "fine")
    cmds = discover_markdown_commands(profile, global_root=tmp_path / "g")
    assert [c.name for c in cmds] == ["good"]


def test_invalid_filename_is_skipped(tmp_path: Path) -> None:
    profile = tmp_path / "p"
    _write(profile / "commands" / "Bad Name.md", "body")
    _write(profile / "commands" / "ok.md", "body")
    cmds = discover_markdown_commands(profile, global_root=tmp_path / "g")
    assert [c.name for c in cmds] == ["ok"]


def test_oversized_body_is_skipped(tmp_path: Path) -> None:
    profile = tmp_path / "p"
    _write(profile / "commands" / "huge.md", "x" * (MAX_BODY_BYTES + 1))
    _write(profile / "commands" / "small.md", "tiny")
    cmds = discover_markdown_commands(profile, global_root=tmp_path / "g")
    assert [c.name for c in cmds] == ["small"]


def test_non_md_files_ignored(tmp_path: Path) -> None:
    profile = tmp_path / "p"
    _write(profile / "commands" / "notes.txt", "not a command")
    _write(profile / "commands" / "real.md", "body")
    cmds = discover_markdown_commands(profile, global_root=tmp_path / "g")
    assert [c.name for c in cmds] == ["real"]


# ── body rendering ───────────────────────────────────────────────────


def test_render_substitutes_args_placeholder() -> None:
    cmd = MarkdownCommand(
        name="explain", body="Explain {{args}} like I'm a junior.",
        source_path=Path("x.md"),
    )
    assert render_command_body(cmd, "monads") == (
        "Explain monads like I'm a junior."
    )


def test_render_empty_args_substitutes_empty_string() -> None:
    cmd = MarkdownCommand(
        name="x", body="Before {{args}} after.", source_path=Path("x.md"),
    )
    assert render_command_body(cmd, "") == "Before  after."


def test_render_appends_args_when_no_placeholder() -> None:
    """Args must never be silently dropped — if there's no placeholder
    the args are appended so the user's input still reaches the model."""
    cmd = MarkdownCommand(
        name="x", body="Do the thing.", source_path=Path("x.md"),
    )
    assert render_command_body(cmd, "now").startswith("Do the thing.")
    assert "now" in render_command_body(cmd, "now")


def test_render_no_args_no_placeholder_returns_body_verbatim() -> None:
    cmd = MarkdownCommand(
        name="x", body="Just do it.", source_path=Path("x.md"),
    )
    assert render_command_body(cmd, "") == "Just do it."
