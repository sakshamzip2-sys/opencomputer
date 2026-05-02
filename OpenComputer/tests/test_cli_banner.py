"""Tests for cli_banner.py — banner assembly + helpers."""
from __future__ import annotations

from pathlib import Path


def test_format_banner_version_label_includes_version_string():
    from opencomputer import __version__
    from opencomputer.cli_banner import format_banner_version_label

    label = format_banner_version_label()
    assert __version__ in label
    assert "OpenComputer" in label


def test_format_banner_version_label_includes_git_sha_when_available(monkeypatch):
    from opencomputer.cli_banner import format_banner_version_label

    monkeypatch.setattr(
        "opencomputer.cli_banner._git_short_sha", lambda: "deadbeef"
    )
    assert "deadbeef" in format_banner_version_label()


def test_format_banner_version_label_omits_git_sha_when_unavailable(monkeypatch):
    from opencomputer.cli_banner import format_banner_version_label

    monkeypatch.setattr("opencomputer.cli_banner._git_short_sha", lambda: None)
    label = format_banner_version_label()
    assert "None" not in label


def test_ascii_art_constants_exist():
    from opencomputer.cli_banner_art import (
        OPENCOMPUTER_LOGO,
        OPENCOMPUTER_LOGO_FALLBACK,
        SIDE_GLYPH,
    )

    assert isinstance(OPENCOMPUTER_LOGO, str)
    # Logo is figlet-style art, so the literal "OPENCOMPUTER" text won't
    # appear character-by-character — but we sanity-check it has multiple
    # lines and substantial width (>50 chars on at least one line).
    lines = OPENCOMPUTER_LOGO.strip("\n").splitlines()
    assert len(lines) >= 5, "Logo is at least 5 lines tall"
    assert any(len(line) >= 50 for line in lines), \
        "Logo has at least one line >= 50 chars wide"

    assert OPENCOMPUTER_LOGO_FALLBACK == "OPENCOMPUTER"

    assert isinstance(SIDE_GLYPH, str)
    assert len(SIDE_GLYPH.splitlines()) >= 6, "Side glyph is at least 6 lines"


def test_get_available_skills_walks_skill_dirs(monkeypatch, tmp_path):
    from opencomputer.cli_banner import get_available_skills

    (tmp_path / "coding" / "edit-skill").mkdir(parents=True)
    (tmp_path / "coding" / "edit-skill" / "SKILL.md").write_text("# Edit\n")
    (tmp_path / "coding" / "review-skill").mkdir()
    (tmp_path / "coding" / "review-skill" / "SKILL.md").write_text("# Review\n")
    (tmp_path / "research" / "arxiv").mkdir(parents=True)
    (tmp_path / "research" / "arxiv" / "SKILL.md").write_text("# arxiv\n")

    monkeypatch.setattr(
        "opencomputer.cli_banner._skill_search_paths",
        lambda: [tmp_path],
    )

    grouped = get_available_skills()
    assert sorted(grouped["coding"]) == ["edit-skill", "review-skill"]
    assert grouped["research"] == ["arxiv"]


def test_get_available_skills_dedupes_across_search_paths(
    monkeypatch, tmp_path,
):
    from opencomputer.cli_banner import get_available_skills

    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "core" / "x").mkdir(parents=True)
    (a / "core" / "x" / "SKILL.md").write_text("# x\n")
    (b / "core" / "x").mkdir(parents=True)
    (b / "core" / "x" / "SKILL.md").write_text("# x dup\n")

    monkeypatch.setattr(
        "opencomputer.cli_banner._skill_search_paths", lambda: [a, b]
    )

    grouped = get_available_skills()
    assert grouped["core"] == ["x"], "duplicate skill names dedupe"


def test_get_available_tools_groups_by_module_path(monkeypatch):
    from opencomputer.cli_banner import get_available_tools

    fake_snapshot = {
        "Edit": "coding-harness",
        "MultiEdit": "coding-harness",
        "Read": "core",
        "Bash": "core",
    }
    monkeypatch.setattr(
        "opencomputer.cli_banner._tool_registry_snapshot", lambda: fake_snapshot
    )

    grouped = get_available_tools()
    assert sorted(grouped["coding-harness"]) == ["Edit", "MultiEdit"]
    assert sorted(grouped["core"]) == ["Bash", "Read"]


def test_get_available_tools_returns_empty_dict_when_registry_unreachable(
    monkeypatch,
):
    from opencomputer.cli_banner import get_available_tools

    def boom():
        raise RuntimeError("registry not initialized")

    monkeypatch.setattr(
        "opencomputer.cli_banner._tool_registry_snapshot", boom
    )

    assert get_available_tools() == {}


def test_build_welcome_banner_renders_logo_and_version(monkeypatch):
    import io

    from rich.console import Console

    from opencomputer.cli_banner import build_welcome_banner

    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills",
        lambda: {"coding": ["edit", "read"]},
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: {"core": ["Edit", "Read"]},
    )

    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    build_welcome_banner(
        console, model="claude-opus-4-7", cwd="/tmp",
        session_id="abc123", home=Path("/home/user/.opencomputer"),
    )
    out = buf.getvalue()
    # Either the figlet OPENCOMPUTER logo or its plain-text fallback
    assert "OPENCOMPUTER" in out or "/_____/" in out
    assert "claude-opus-4-7" in out
    assert "abc123" in out


def test_build_welcome_banner_lists_tools_and_skills(monkeypatch):
    import io

    from rich.console import Console

    from opencomputer.cli_banner import build_welcome_banner

    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills",
        lambda: {"research": ["arxiv", "blogwatcher"]},
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: {"coding-harness": ["Edit", "MultiEdit", "TodoWrite"]},
    )

    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    build_welcome_banner(console, "m", "/cwd")
    out = buf.getvalue()
    assert "research" in out
    assert "arxiv" in out
    assert "coding-harness" in out
    assert "Edit" in out


def test_build_welcome_banner_footer_counts(monkeypatch):
    import io

    from rich.console import Console

    from opencomputer.cli_banner import build_welcome_banner

    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills",
        lambda: {"a": ["s1", "s2"], "b": ["s3"]},  # 3 skills
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: {"core": ["t1", "t2", "t3", "t4"]},  # 4 tools
    )

    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    build_welcome_banner(console, "m", "/cwd")
    out = buf.getvalue()
    assert "4 tools" in out
    assert "3 skills" in out
    assert "/help" in out


def test_build_welcome_banner_truncates_long_tool_lines(monkeypatch):
    import io

    from rich.console import Console

    from opencomputer.cli_banner import build_welcome_banner

    long_tool_list = [f"Tool{i:02d}" for i in range(40)]
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills", lambda: {}
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: {"big": long_tool_list},
    )

    buf = io.StringIO()
    console = Console(file=buf, width=80, force_terminal=False)
    build_welcome_banner(console, "m", "/cwd")
    out = buf.getvalue()
    assert "…" in out, "Long lists must be truncated with ellipsis"
