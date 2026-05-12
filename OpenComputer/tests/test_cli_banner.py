"""Tests for cli_banner.py — OC-style minimal splash (post 2026-05-12).

The previous "Option D HUD" splash (mascot + 4-column runtime grid + tool
+ skill chip rows) was replaced with an OpenCode-style minimal splash:
``OPENCOMPUTER`` half-block wordmark + version-right + footer prompt. Runtime
state (model / provider / cwd / session) moved out of the splash to
``oc status`` and statusline surfaces. See
``docs/superpowers/specs/2026-05-12-oc-splash-replace-hermes-design.md``.
"""
from __future__ import annotations

import io
import re

import pytest
from rich.console import Console

# --- Helpers preserved across the migration -----------------------------


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
    """``cli_banner_art`` exports are public API; downstream consumers may
    reach into them (e.g., a future ``oc status``). Don't break that.
    """
    from opencomputer.cli_banner_art import (
        OPENCOMPUTER_BLOCK_LOGO,
        OPENCOMPUTER_LOGO,
        OPENCOMPUTER_LOGO_FALLBACK,
        SIDE_GLYPH,
    )

    assert isinstance(OPENCOMPUTER_LOGO, str)
    lines = OPENCOMPUTER_LOGO.strip("\n").splitlines()
    assert len(lines) >= 5
    assert any(len(line) >= 50 for line in lines)
    assert OPENCOMPUTER_LOGO_FALLBACK == "OPENCOMPUTER"
    assert isinstance(SIDE_GLYPH, str)
    assert len(SIDE_GLYPH.splitlines()) >= 6
    # The active splash uses the block logo. Three rows, ≥70 cols wide.
    assert isinstance(OPENCOMPUTER_BLOCK_LOGO, str)
    block_rows = OPENCOMPUTER_BLOCK_LOGO.rstrip("\n").splitlines()
    assert len(block_rows) == 3
    assert all(len(row) >= 70 for row in block_rows)


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


def test_get_available_skills_dedupes_across_search_paths(monkeypatch, tmp_path):
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
    assert get_available_skills()["core"] == ["x"]


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


def test_get_available_tools_returns_empty_dict_when_registry_unreachable(monkeypatch):
    from opencomputer.cli_banner import get_available_tools

    def boom():
        raise RuntimeError("registry not initialized")

    monkeypatch.setattr(
        "opencomputer.cli_banner._tool_registry_snapshot", boom
    )

    assert get_available_tools() == {}


# --- New OC-style minimal splash ---------------------------------------


def _render(monkeypatch, *, width=120, **kwargs):
    """Helper: render the welcome banner and return (output, console)."""
    from opencomputer.cli_banner import build_welcome_banner

    # Default monkeypatches — silence helpers that hit disk / subprocess
    # unless the caller explicitly overrides.
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills", lambda: {}
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools", lambda: {}
    )
    buf = io.StringIO()
    console = Console(file=buf, width=width, force_terminal=False)
    build_welcome_banner(
        console,
        model=kwargs.pop("model", "claude-opus-4-7"),
        cwd=kwargs.pop("cwd", "/tmp"),
        **kwargs,
    )
    return buf.getvalue(), console


def test_build_welcome_banner_renders_opencomputer_wordmark(monkeypatch):
    """At ≥73 cols, the splash MUST render the half-block ``OPENCOMPUTER``
    wordmark from ``OPENCOMPUTER_BLOCK_LOGO``. We check for the actual
    block-character row prefixes from that constant.
    """
    out, _ = _render(monkeypatch, width=120)
    # The block logo has solid █ characters across all three rows.
    assert "█" in out
    # Row-1 of OPENCOMPUTER_BLOCK_LOGO starts with the "O" letter:
    # ``▄▀▀▀▄`` — the splash output must include at least one of these
    # distinctive half-block prefixes.
    assert "▄▀▀▀▄" in out


def test_build_welcome_banner_includes_version_right(monkeypatch):
    """Version + git SHA are pulled right on the splash's middle row."""
    from opencomputer import __version__

    monkeypatch.setattr(
        "opencomputer.cli_banner._git_short_sha", lambda: "deadbeef"
    )
    out, _ = _render(monkeypatch, width=120)
    assert f"v{__version__}" in out
    assert "deadbeef" in out
    assert " · " in out  # spacer between version and SHA


def test_build_welcome_banner_includes_footer(monkeypatch):
    out, _ = _render(monkeypatch, width=120)
    # Left footer cluster.
    assert "› Ready." in out
    assert "Type a message, or" in out
    assert "/help" in out
    # Right footer cluster — keep verbatim from spec §3.1.
    assert "/status · /model · /help · /exit" in out


def test_footer_advertises_only_registered_slash_commands():
    """Regression guard: every ``/cmd`` token the splash advertises in
    its right-side footer MUST correspond to a registered slash command.
    No liar UI.
    """
    import re

    from opencomputer.agent.slash_commands import register_builtin_slash_commands
    from opencomputer.cli_banner import _FOOTER_RIGHT
    from opencomputer.cli_ui.slash import SLASH_REGISTRY
    from opencomputer.plugins.registry import registry as plugin_registry

    # Built-in slash commands populate the plugin registry on first call.
    register_builtin_slash_commands()

    # Collect every advertised ``/cmd`` token from the footer.
    advertised = set(re.findall(r"/([a-z][a-z0-9_-]*)", _FOOTER_RIGHT))
    assert advertised, "footer advertises no slash commands — invalid state"

    # Build the set of every name + alias that resolves to a real command.
    legacy_names: set[str] = set()
    for cmd in SLASH_REGISTRY:
        legacy_names.add(cmd.name)
        legacy_names.update(cmd.aliases)
    handler_names: set[str] = set(plugin_registry.slash_commands.keys())

    registered = legacy_names | handler_names
    missing = advertised - registered
    assert not missing, (
        f"Footer advertises slash commands that are NOT registered: "
        f"{sorted(missing)}. Either remove from cli_banner._FOOTER_RIGHT "
        f"or register them in cli_ui/slash.py:SLASH_REGISTRY / "
        f"slash_commands_impl/."
    )


def test_build_welcome_banner_does_not_render_runtime_info(monkeypatch):
    """``provider``, ``session_id``, ``session_label``, ``cwd`` are accepted
    for back-compat but MUST NOT appear in the splash output. They live in
    statusline / ``oc status`` now.
    """
    out, _ = _render(
        monkeypatch,
        width=140,
        model="claude-opus-4-7",
        cwd="/Users/saksham/super-secret-cwd",
        provider="anthropic",
        session_id="64d3a534-5f77-4ebf-bfd4-61b16b68d749",
        session_label="my-cool-session",
    )
    assert "claude-opus-4-7" not in out
    assert "anthropic" not in out
    assert "super-secret-cwd" not in out
    assert "my-cool-session" not in out
    assert "64d3a534" not in out
    assert "b68d749" not in out
    # Old HUD column labels — the splash must not regress.
    assert "MODEL" not in out
    assert "PROVIDER" not in out
    assert "CWD" not in out
    assert "SESSION" not in out
    assert "TOOLS · " not in out
    assert "SKILLS · " not in out


def test_build_welcome_banner_narrow_terminal_fallback(monkeypatch):
    """Below the 73-col block-logo width, the splash falls back to the
    plain ``OPENCOMPUTER`` wordmark with no half-block art.
    """
    out, _ = _render(monkeypatch, width=40)
    assert "OPENCOMPUTER" in out
    # Half-block characters that only appear in the block-logo art MUST NOT
    # appear in the narrow fallback. ``▀▄`` are the half-block markers.
    distinctive_block_chars = ("▄▀▀▀▄", "█▀▀▀▄", "▀▄▄▄▀")
    for marker in distinctive_block_chars:
        assert marker not in out
    # Footer must still render.
    assert "› Ready." in out


def test_build_welcome_banner_does_not_leak_hermes(monkeypatch):
    """The splash must not surface any of the legacy Hermes-derived
    strings. This regresses the migration if anyone adds them back.
    """
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills",
        lambda: {"coding": ["edit"]},
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: {"core": ["Edit"]},
    )
    out, _ = _render(monkeypatch, width=140, session_id="abc-123")
    forbidden = (
        "NOUS HERMES",
        "Nous Research",
        "Messenger of the Digital Gods",
        "hermes shell",
        "Hermes",
    )
    for needle in forbidden:
        assert needle.lower() not in out.lower(), f"Hermes leak: {needle!r}"


def test_build_welcome_banner_handles_missing_version(monkeypatch):
    """Empty ``__version__`` ⇒ splash renders without the version cluster."""
    monkeypatch.setattr("opencomputer.cli_banner.__version__", "")
    out, _ = _render(monkeypatch, width=120)
    # No stray ``v · sha`` artifact.
    assert not re.search(r"\bv\s*·\s*[0-9a-f]{7}\b", out)


def test_build_welcome_banner_handles_missing_sha(monkeypatch):
    """``_git_short_sha`` returning None ⇒ no trailing ` · ` separator."""
    from opencomputer import __version__

    monkeypatch.setattr("opencomputer.cli_banner._git_short_sha", lambda: None)
    out, _ = _render(monkeypatch, width=120)
    assert f"v{__version__}" in out
    # No trailing ` · ` after the version when SHA is unavailable.
    assert f"v{__version__} · " not in out


def test_build_welcome_banner_accepts_all_legacy_kwargs_without_error(monkeypatch):
    """Existing call site in cli.py passes (model, cwd, provider,
    session_id, session_label, home). All must be accepted silently.
    """
    from pathlib import Path

    # Should not raise.
    _render(
        monkeypatch,
        width=120,
        model="claude-opus-4-7 (anthropic)",  # legacy combined form
        cwd="/tmp",
        provider="anthropic",
        session_id="uuid-123",
        session_label="alpha",
        home=Path("/home/x/.opencomputer"),
    )


def test_build_welcome_banner_renders_at_minimum_width_safely(monkeypatch):
    """A pathologically narrow terminal (cols=20) must not crash."""
    # Should not raise.
    out, _ = _render(monkeypatch, width=20)
    assert "OPENCOMPUTER" in out


def test_build_welcome_banner_with_empty_model_and_cwd(monkeypatch):
    """Adversarial inputs: empty strings must not crash or echo as runtime
    info on the splash."""
    out, _ = _render(monkeypatch, width=120, model="", cwd="")
    # Splash still renders.
    assert "› Ready." in out


def test_build_welcome_banner_update_hint_failure_does_not_crash(monkeypatch):
    """If the update-check helper raises, the splash must still render."""
    def boom(timeout: float = 0.2):
        raise RuntimeError("update check broken")

    monkeypatch.setattr(
        "opencomputer.cli_update_check.get_update_hint", boom, raising=False
    )
    out, _ = _render(monkeypatch, width=120)
    assert "› Ready." in out


# --- Pico mascot tests — unrelated, kept intact ------------------------


def test_pico_module_exposes_six_expressions():
    from opencomputer.cli_pico import PICO_EXPRESSIONS

    assert set(PICO_EXPRESSIONS) == {
        "idle", "blink", "happy", "curious", "rolled", "zooming",
    }


def test_pico_grids_are_16_wide_and_well_formed():
    from opencomputer.cli_pico import PICO_EXPRESSIONS

    for name, (grid, color) in PICO_EXPRESSIONS.items():
        assert grid, f"{name} grid is empty"
        assert all(len(row) == 16 for row in grid), \
            f"{name} has rows that aren't 16 wide"
        assert all(set(row) <= {"#", "."} for row in grid), \
            f"{name} has chars outside #/."
        assert color.startswith("#") and len(color) == 7


def test_render_pico_idle_produces_half_block_text_in_rose():
    from rich.text import Text

    from opencomputer.cli_pico import render_pico

    out = render_pico("idle")
    assert isinstance(out, Text)
    plain = out.plain
    assert plain, "rendered Pico has no characters"
    assert any(ch in plain for ch in "▀▄█"), \
        "expected half-block characters in output"
    assert any("#C2185B" in str(span.style) for span in out.spans), \
        "expected #C2185B style on at least one span"


def test_render_pico_unknown_expression_raises_keyerror():
    from opencomputer.cli_pico import render_pico

    with pytest.raises(KeyError):
        render_pico("not-a-real-expression")
