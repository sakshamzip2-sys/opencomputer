"""Tests for cli_banner.py — Hermes-style splash with OC pink + real data
(2026-05-12 redesign, third pass).

Supersedes the OC-minimal-splash test suite. The new splash integrates
the hermes-agent visual shape — chunky ``ansi_shadow`` wordmark over a
rounded panel that puts a Braille caduceus + runtime info on the left
and Available Tools + Available Skills on the right — with OC pink
colors and OC's own data feeds (real registries, real version + SHA,
real model/provider/cwd/session).
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pytest
from rich.console import Console

# --- Helper tests (unchanged — public API stays stable) -----------------


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
    """``cli_banner_art`` exports are public API — downstream consumers
    may reach into them. Don't break that.
    """
    from opencomputer.cli_banner_art import (
        OPEN_COMPUTER_CADUCEUS_PINK,
        OPEN_COMPUTER_LOGO_HERMES_STYLE,
        OPEN_COMPUTER_LOGO_HERMES_STYLE_WIDTH,
        OPENCOMPUTER_BLOCK_LOGO,
        OPENCOMPUTER_LOGO,
        OPENCOMPUTER_LOGO_FALLBACK,
        SIDE_GLYPH,
    )

    # Legacy constants — kept for back-compat.
    assert isinstance(OPENCOMPUTER_LOGO, str)
    assert len(OPENCOMPUTER_LOGO.strip("\n").splitlines()) >= 5
    assert OPENCOMPUTER_LOGO_FALLBACK == "OPENCOMPUTER"
    assert isinstance(SIDE_GLYPH, str)
    assert len(SIDE_GLYPH.splitlines()) >= 6
    assert isinstance(OPENCOMPUTER_BLOCK_LOGO, str)

    # New Hermes-style wordmark + caduceus.
    assert isinstance(OPEN_COMPUTER_LOGO_HERMES_STYLE, str)
    assert "OPEN-COMPUTER" not in OPEN_COMPUTER_LOGO_HERMES_STYLE  # rendered as art, not text
    assert "██" in OPEN_COMPUTER_LOGO_HERMES_STYLE  # solid-block letterforms
    assert OPEN_COMPUTER_LOGO_HERMES_STYLE_WIDTH > 100
    assert isinstance(OPEN_COMPUTER_CADUCEUS_PINK, str)
    assert "⠀" in OPEN_COMPUTER_CADUCEUS_PINK or "⣿" in OPEN_COMPUTER_CADUCEUS_PINK


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


# --- New Hermes-style splash --------------------------------------------


def _render(monkeypatch, *, width=140, tools=None, skills=None, **kwargs):
    """Helper: render the banner into a StringIO and return the text."""
    from opencomputer.cli_banner import build_welcome_banner

    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_skills",
        lambda: skills if skills is not None else {},
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner.get_available_tools",
        lambda: tools if tools is not None else {},
    )
    buf = io.StringIO()
    console = Console(file=buf, width=width, force_terminal=False)
    build_welcome_banner(
        console,
        model=kwargs.pop("model", "claude-opus-4-7"),
        cwd=kwargs.pop("cwd", "/tmp"),
        **kwargs,
    )
    return buf.getvalue()


def test_renders_hermes_style_ansi_shadow_wordmark(monkeypatch):
    """At wide widths the splash renders the OPEN-COMPUTER ansi_shadow
    wordmark — distinctive ``██╗`` / ``╔═══`` / ``╚═══`` blocks present.
    """
    out = _render(monkeypatch, width=140)
    assert "██╗" in out
    assert "╔" in out
    assert "╚" in out
    # Legacy half-block art must not regress back in alongside.
    assert "▄▀▀▀▄" not in out


def test_panel_title_includes_version_and_sha(monkeypatch):
    """Panel title: ``OpenComputer v{__version__} · {sha}``."""
    from opencomputer import __version__

    monkeypatch.setattr(
        "opencomputer.cli_banner._git_short_sha", lambda: "deadbeef"
    )
    out = _render(monkeypatch, width=140)
    assert "OpenComputer" in out
    assert f"v{__version__}" in out
    assert "deadbeef" in out


def test_panel_renders_runtime_info_in_left_column(monkeypatch):
    """Left column under the caduceus shows model · provider, cwd, and
    ``Session: {label}`` — opposite of the previous "no-runtime" spec.
    """
    out = _render(
        monkeypatch,
        width=140,
        model="claude-opus-4-7",
        provider="anthropic",
        cwd="/Users/saksham/Vscode/claude",
        session_id="64d3a534-5f77-4ebf-bfd4-61b16b68d749",
        session_label="my-cool-session",
    )
    assert "claude-opus-4-7" in out
    assert "anthropic" in out
    assert "/Users/saksham/Vscode/claude" in out
    assert "Session:" in out
    assert "my-cool-session" in out
    # Raw uuid never leaks when a human label is set.
    assert "64d3a534-5f77-4ebf-bfd4-61b16b68d749" not in out


def test_panel_elides_session_uuid_when_no_label(monkeypatch):
    """Without a real label, the splash shows ``Session: head…tail``."""
    out = _render(
        monkeypatch,
        width=140,
        session_id="64d3a534-5f77-4ebf-bfd4-61b16b68d749",
    )
    assert "Session:" in out
    assert "64d3a534" in out
    assert "b68d749" in out
    assert "…" in out
    assert "64d3a534-5f77-4ebf-bfd4-61b16b68d749" not in out


def test_session_label_echoing_uuid_is_treated_as_no_label(monkeypatch):
    """``_session_label_for_banner`` returns the uuid as a fallback. The
    splash must detect that and elide instead of printing the raw uuid.
    """
    uuid = "64d3a534-5f77-4ebf-bfd4-61b16b68d749"
    out = _render(monkeypatch, width=140, session_id=uuid, session_label=uuid)
    assert uuid not in out
    assert "64d3a534" in out
    assert "…" in out


def test_panel_renders_tools_section_with_top_8_toolsets(monkeypatch):
    """Tools section: ``Available Tools`` header + up to 8 toolset rows,
    each ``toolset: tool, tool, ...``. Overflow shown as ``(and N more
    toolsets...)``.
    """
    tools = {f"toolset_{i:02d}": [f"tool_{i}_a", f"tool_{i}_b"] for i in range(12)}
    out = _render(monkeypatch, width=140, tools=tools)
    assert "Available Tools" in out
    assert "toolset_00:" in out
    assert "toolset_07:" in out  # 8th (index 7)
    assert "toolset_08:" not in out  # 9th is collapsed
    assert "(and 4 more toolsets...)" in out


def test_panel_renders_skills_section_with_top_8_categories(monkeypatch):
    """Skills section: ``Available Skills`` header + up to 8 category
    rows. Overflow shown as ``(and N more categories...)``.
    """
    skills = {f"cat_{i:02d}": [f"skill_{i}"] for i in range(15)}
    out = _render(monkeypatch, width=140, skills=skills)
    assert "Available Singular Skills" not in out
    assert "Available Skills" in out
    assert "cat_00:" in out
    assert "cat_07:" in out
    assert "cat_08:" not in out  # collapsed
    assert "(and 7 more categories...)" in out


def test_panel_renders_summary_line_with_real_counts(monkeypatch):
    """Summary footer reports total tool and skill counts."""
    tools = {"core": ["A", "B", "C"], "extras": ["D", "E"]}  # 5 tools total
    skills = {"general": ["s1", "s2", "s3", "s4"]}            # 4 skills total
    out = _render(monkeypatch, width=140, tools=tools, skills=skills)
    assert "5 tools" in out
    assert "4 skills" in out
    assert "/help" in out


def test_panel_summary_omitted_when_registries_empty(monkeypatch):
    """When BOTH registries are empty (plugin discovery not yet run),
    the misleading ``0 tools · 0 skills`` summary is suppressed.
    """
    out = _render(monkeypatch, width=140, tools={}, skills={})
    assert "0 tools" not in out
    # The headers themselves do render so users still see the structure.
    assert "Available Tools" in out
    assert "Available Skills" in out


def test_welcome_and_tip_render_after_panel(monkeypatch):
    """Welcome line + a ✦-prefixed tip below the panel."""
    out = _render(monkeypatch, width=140)
    assert "Welcome to OpenComputer!" in out
    assert "Type your message or" in out
    assert "/help" in out
    assert "✦ Tip:" in out


def test_tip_is_drawn_from_curated_list(monkeypatch):
    """The rendered tip must be one of the curated ``_TIPS`` entries."""
    from opencomputer.cli_banner import _TIPS

    out = _render(monkeypatch, width=140)
    assert _TIPS, "_TIPS must not be empty"
    rendered_tips = [t for t in _TIPS if t in out]
    assert len(rendered_tips) == 1, (
        f"Expected exactly one curated tip in output, got {rendered_tips}"
    )


def test_chooses_a_random_tip_per_call(monkeypatch):
    """Multiple invocations cycle through the tip pool (not pinned)."""
    seen: set[str] = set()
    for _ in range(50):
        out = _render(monkeypatch, width=140)
        match = re.search(r"✦ Tip: (.+?)(?:\033|$|\n)", out)
        if match:
            seen.add(match.group(1).strip())
    # 50 calls × 7 tips → overwhelmingly likely we see ≥3 distinct tips.
    assert len(seen) >= 3, f"Tip rotation looks pinned: only saw {seen!r}"


def test_no_hermes_branding_leaks(monkeypatch):
    """The output is OC-branded throughout — no upstream Hermes strings."""
    out = _render(
        monkeypatch,
        width=140,
        model="claude-opus-4-7",
        provider="anthropic",
        session_id="abc-123",
        tools={"core": ["Edit"]},
        skills={"general": ["foo"]},
    )
    forbidden = (
        "NOUS HERMES",
        "Nous Research",
        "Hermes Agent",
        "Messenger of the Digital Gods",
        "hermes shell",
        "kimi-k2.5:cloud",
    )
    for needle in forbidden:
        assert needle.lower() not in out.lower(), f"Hermes leak: {needle!r}"


def test_accepts_legacy_combined_model_provider_string(monkeypatch):
    """Old call sites pass ``"model (provider)"`` as one string. The
    splash splits it back apart so both halves render correctly.
    """
    out = _render(monkeypatch, width=140, model="claude-opus-4-7 (anthropic)")
    assert "claude-opus-4-7" in out
    assert "anthropic" in out
    # The combined form must NOT echo as a literal substring.
    assert "claude-opus-4-7 (anthropic)" not in out


def test_handles_missing_version_gracefully(monkeypatch):
    """Empty ``__version__`` → no stray ``v · sha`` artifact in title."""
    monkeypatch.setattr("opencomputer.cli_banner.__version__", "")
    out = _render(monkeypatch, width=140)
    # No partial ``v · {sha}`` line — when version is empty, the whole
    # version cluster collapses.
    assert not re.search(r"\bv\s*·\s*[0-9a-f]{7}\b", out)


def test_handles_missing_sha_gracefully(monkeypatch):
    """``_git_short_sha → None`` → title shows only ``OpenComputer v{ver}``."""
    from opencomputer import __version__

    monkeypatch.setattr("opencomputer.cli_banner._git_short_sha", lambda: None)
    out = _render(monkeypatch, width=140)
    assert f"v{__version__}" in out
    # No trailing ` · ` after version when SHA is unavailable.
    assert f"v{__version__} · " not in out


def test_accepts_all_legacy_kwargs_without_error(monkeypatch):
    """The signature ``(console, model, cwd, *, provider, session_id,
    session_label, home)`` must stay stable — existing call site uses it.
    """
    # Should not raise.
    _render(
        monkeypatch,
        width=140,
        model="claude-opus-4-7 (anthropic)",
        cwd="/tmp",
        provider="anthropic",
        session_id="uuid-123",
        session_label="alpha",
        home=Path("/home/x/.opencomputer"),
    )


def test_narrow_terminal_falls_back_gracefully(monkeypatch):
    """Below ``_WORDMARK_MIN_WIDTH`` we drop the ansi_shadow art and use
    a smaller fallback. Splash must not crash and must still print the
    OpenComputer name.
    """
    out = _render(monkeypatch, width=80)
    assert "OpenComputer" in out or "OPENCOMPUTER" in out


def test_pathologically_narrow_does_not_crash(monkeypatch):
    """20-col terminal — drops to plain text + stacked panel content."""
    out = _render(monkeypatch, width=20)
    # Plain fallback at min: bold OPENCOMPUTER appears.
    assert "OPENCOMPUTER" in out


def test_empty_model_and_cwd_do_not_crash(monkeypatch):
    """Adversarial inputs: empty strings render without crash, model
    line/cwd line are simply suppressed.
    """
    out = _render(monkeypatch, width=140, model="", cwd="")
    assert "Welcome to OpenComputer!" in out


def test_update_hint_failure_swallowed(monkeypatch):
    """If ``get_update_hint`` raises, the splash still finishes rendering."""
    def boom(timeout: float = 0.2):
        raise RuntimeError("update check broken")

    monkeypatch.setattr(
        "opencomputer.cli_update_check.get_update_hint", boom, raising=False
    )
    out = _render(monkeypatch, width=140)
    assert "Welcome to OpenComputer!" in out


def test_panel_renders_at_full_width_with_real_data_shape(monkeypatch):
    """End-to-end: a realistic registry payload renders cleanly at the
    typical terminal width — no Rich layout exceptions, all sections
    present.
    """
    tools = {
        "core": ["AppleScriptRun", "AskUserQuestion", "Bash", "Clarify"],
        "coding-harness": ["Edit", "MultiEdit", "Read", "Write", "TodoWrite"],
        "extras": ["AmazonTrackPrice", "ArxivSearch"],
    }
    skills = {
        "coding": ["debug-python", "edit-skill", "review-skill"],
        "research": ["arxiv", "blogwatcher", "polymarket"],
    }
    out = _render(
        monkeypatch, width=140,
        tools=tools,
        skills=skills,
        model="claude-opus-4-7",
        provider="anthropic",
        cwd="/Users/saksham/Vscode/claude",
        session_id="64d3a534-5f77-4ebf-bfd4-61b16b68d749",
    )
    # Wordmark + panel + welcome + tip — every block.
    assert "██╗" in out
    assert "OpenComputer" in out
    assert "claude-opus-4-7" in out
    assert "anthropic" in out
    assert "Available Tools" in out
    assert "Available Skills" in out
    assert "Edit" in out
    assert "arxiv" in out
    assert "Welcome to OpenComputer!" in out
    assert "✦ Tip:" in out


def test_long_toolset_items_truncated_per_row(monkeypatch):
    """A toolset with many tools gets its items truncated with ``...``
    so a single row doesn't blow out the panel width.
    """
    tools = {"big": [f"Tool{i:02d}" for i in range(40)]}
    out = _render(monkeypatch, width=140, tools=tools)
    assert "Tool00" in out
    # The 40th tool can't fit in the per-row budget.
    assert "Tool39" not in out
    assert "..." in out


# --- New section tests (panel chrome, MCP, profile, alignment) ---------


def test_panel_renders_rounded_border(monkeypatch):
    """The outer panel must render with ROUNDED box-drawing characters —
    the visual signature of the Hermes-style splash.
    """
    out = _render(monkeypatch, width=140)
    # Top-left + top-right + bottom-left + bottom-right corners of the
    # rounded box. At least the top corners are guaranteed; bottom too.
    assert "╭" in out
    assert "╮" in out
    assert "╰" in out
    assert "╯" in out


def test_panel_renders_braille_caduceus_inside(monkeypatch):
    """The Braille-block caduceus must actually appear inside the
    rendered panel, not just exist as a module-level constant.
    """
    out = _render(monkeypatch, width=140)
    # Distinctive Braille glyphs from the caduceus art that don't
    # appear in any other splash section.
    assert "⣿" in out or "⣦" in out
    assert "⠿" in out or "⣠" in out


def test_panel_renders_mcp_section_when_status_provided(monkeypatch):
    """``MCP Servers`` section + each server's row render when caller
    passes a non-empty ``mcp_status`` list.
    """
    mcp = [
        {
            "name": "filesystem",
            "transport": "stdio",
            "connection_state": "connected",
            "tool_count": 7,
        },
        {
            "name": "web-search",
            "transport": "http://localhost:9000",
            "connection_state": "connected",
            "tool_count": 3,
        },
    ]
    out = _render(monkeypatch, width=140, mcp_status=mcp)
    assert "MCP Servers" in out
    assert "filesystem" in out
    assert "7 tools" in out
    assert "web-search" in out
    assert "3 tools" in out
    # Summary line counts connected MCP servers.
    assert "2 MCP" in out


def test_panel_renders_mcp_failed_state(monkeypatch):
    """A disconnected/errored MCP server shows in red with the error."""
    mcp = [
        {
            "name": "broken-mcp",
            "transport": "stdio",
            "connection_state": "error",
            "last_error": "connection refused",
        }
    ]
    out = _render(monkeypatch, width=140, mcp_status=mcp)
    assert "MCP Servers" in out
    assert "broken-mcp" in out
    assert "connection refused" in out
    # Failed servers don't bump the connected-MCP count.
    assert "1 MCP" not in out


def test_panel_omits_mcp_section_when_none(monkeypatch):
    """No MCP status ⇒ no ``MCP Servers`` section, no MCP in summary."""
    out = _render(monkeypatch, width=140, mcp_status=None)
    assert "MCP Servers" not in out
    assert " MCP " not in out


def test_panel_renders_profile_line_when_non_default(monkeypatch):
    """A non-default active profile surfaces in the left column under
    the caduceus as ``Profile: {name}``.
    """
    monkeypatch.setattr(
        "opencomputer.cli_banner._active_profile_name", lambda: "alpha"
    )
    out = _render(monkeypatch, width=140)
    assert "Profile:" in out
    assert "alpha" in out


def test_panel_omits_profile_line_when_default(monkeypatch):
    """No active profile (default) ⇒ no ``Profile:`` line."""
    monkeypatch.setattr(
        "opencomputer.cli_banner._active_profile_name", lambda: None
    )
    out = _render(monkeypatch, width=140)
    assert "Profile:" not in out


def test_active_profile_name_swallows_errors(monkeypatch):
    """Profile read errors fail open — the helper returns None instead
    of raising, so the splash still renders.
    """
    from opencomputer import cli_banner

    def boom():
        raise RuntimeError("profile config corrupted")

    monkeypatch.setattr(
        "opencomputer.profiles.read_active_profile", boom, raising=False
    )
    # No exception, returns None.
    assert cli_banner._active_profile_name() is None


def test_runtime_block_left_aligned_under_caduceus(monkeypatch):
    """The caduceus is centered in the left column; the runtime info
    below it is LEFT-aligned (not inheriting the column's centering).

    Heuristic: the model line should appear with limited leading
    whitespace, while the caduceus rows have substantial leading
    whitespace from centering.
    """
    out = _render(
        monkeypatch,
        width=140,
        model="claude-opus-4-7",
        provider="anthropic",
    )
    # Strip ANSI sequences for whitespace measurement.
    import re

    ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    plain_lines = [ansi_re.sub("", line) for line in out.splitlines()]
    model_lines = [pl for pl in plain_lines if "claude-opus-4-7" in pl]
    assert model_lines, "model line missing from output"

    # The model line lives inside the panel's left column. Measure how
    # many leading spaces it has *after* the panel's ``│`` border. If the
    # column were center-aligned, the model line would carry many
    # leading spaces (≥ ~10). Left-aligned: only the panel padding (~2-4).
    line = model_lines[0]
    idx = line.find("│")
    after_border = line[idx + 1 :] if idx >= 0 else line
    leading = len(after_border) - len(after_border.lstrip(" "))
    assert leading <= 8, (
        f"Model line looks centered, not left-aligned (leading={leading}): "
        f"{line!r}"
    )


def test_tip_selection_uses_random_choice(monkeypatch):
    """The tip is drawn via ``random.choice(_TIPS)``. Pinning ``choice``
    is a more reliable way to assert that the tip subsystem is wired
    correctly than relying on output sampling.
    """
    import random as random_mod

    from opencomputer.cli_banner import _TIPS

    target = _TIPS[3]  # arbitrary canonical pick
    monkeypatch.setattr(random_mod, "choice", lambda seq: target)
    out = _render(monkeypatch, width=140)
    assert f"✦ Tip: {target}" in out


def test_tip_pool_only_references_real_oc_commands():
    """Regression guard: every tip must reference a real OC command or
    documented env var. No tip may mention a non-existent flag.
    """
    from opencomputer.cli_banner import _TIPS

    # Hard-coded forbidden references — flags/commands we don't ship.
    forbidden_substrings = (
        "oc chat -Q",  # quiet-mode flag never landed in OC
        "opencomputer chat -Q",
        "hermes",
        "hermes_agent",
    )
    for tip in _TIPS:
        for needle in forbidden_substrings:
            assert needle.lower() not in tip.lower(), (
                f"Tip references nonexistent surface {needle!r}: {tip!r}"
            )


def test_update_hint_renders_in_warn_yellow(monkeypatch):
    """When an update IS available, the hint renders bold yellow with
    a ``⚠`` glyph — distinct from the brand pink palette so users
    notice it.
    """
    monkeypatch.setattr(
        "opencomputer.cli_update_check.get_update_hint",
        lambda timeout=0.2: "3 commits behind",
        raising=False,
    )
    out = _render(monkeypatch, width=140)
    assert "⚠" in out
    assert "3 commits behind" in out


def test_end_to_end_panel_has_every_section(monkeypatch):
    """One render call with EVERY production feature wired in: real-shape
    tool/skill registries, an active non-default profile, a connected MCP
    server, a labeled session, version+SHA. Every section must appear.
    """
    monkeypatch.setattr(
        "opencomputer.cli_banner._git_short_sha", lambda: "abc1234"
    )
    monkeypatch.setattr(
        "opencomputer.cli_banner._active_profile_name", lambda: "research"
    )
    tools = {"core": ["Bash", "Edit"], "harness": ["MultiEdit"]}
    skills = {"coding": ["debug-python"]}
    mcp = [
        {"name": "fs", "transport": "stdio", "connection_state": "connected", "tool_count": 5}
    ]
    out = _render(
        monkeypatch,
        width=140,
        tools=tools,
        skills=skills,
        mcp_status=mcp,
        model="claude-opus-4-7",
        provider="anthropic",
        cwd="/Users/saksham/Vscode/claude",
        session_id="64d3a534-5f77-4ebf-bfd4-61b16b68d749",
        session_label="research-session",
    )
    # Wordmark
    assert "██╗" in out
    # Title
    assert "OpenComputer" in out
    assert "abc1234" in out
    # Caduceus
    assert "⣿" in out or "⣦" in out
    # Runtime
    assert "claude-opus-4-7" in out
    assert "anthropic" in out
    assert "/Users/saksham/Vscode/claude" in out
    assert "Session:" in out
    assert "research-session" in out
    assert "Profile:" in out
    assert "research" in out
    # Sections
    assert "Available Tools" in out
    assert "Available Skills" in out
    assert "MCP Servers" in out
    assert "fs" in out
    # Summary
    assert "3 tools" in out
    assert "1 skills" in out
    assert "1 MCP" in out
    # Footer
    assert "Welcome to OpenComputer!" in out
    assert "✦ Tip:" in out


# --- Pico mascot tests (unrelated — kept intact) -----------------------


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
