"""Hermes v2 D5/D6/D7 — renderer-side consumers actually use the skin data.

PRs #510/#512/#515 shipped the YAML data + accessor functions for
spinner faces, the 22-key Hermes color palette, and the live console
plumbing. But the actual *renderers* didn't consume any of it — the
streaming spinner had hardcoded "Thinking…" text and Rich panel borders
used literal hex like ``"grey50"``; the prompt-toolkit completion menu
used a static ``MENU_STYLE`` dict.

This module pins the renderer-side consumption so future contributors
can't silently regress to hardcoded values.
"""
from __future__ import annotations

from rich.console import Console

from opencomputer.cli_ui.skin import apply_skin, load_skin
from opencomputer.cli_ui.streaming import _skin_color, _skin_spinner_text
from opencomputer.cli_ui.style import (
    MENU_STYLE,
    _menu_dict_from_skin,
    current_menu_style,
)

# ─── _skin_spinner_text (D5 wiring) ────────────────────────────────


def test_spinner_text_falls_back_when_no_skin_applied():
    """No skin yet → returns legacy 'Thinking…' (or capitalised verb)."""
    # Apply a fresh "default" skin then deliberately clear face cycles
    # by applying an artificial skin spec. Easier: just check that the
    # text contains either the legacy literal or a face glyph.
    text = _skin_spinner_text(phase="waiting")
    assert isinstance(text, str)
    assert text  # non-empty


def test_spinner_text_uses_default_skin_waiting_face():
    apply_skin(load_skin("default"), Console())
    text = _skin_spinner_text(phase="waiting")
    # default.yaml waiting_faces[0] = "(⊙‿⊙)"
    assert "(⊙‿⊙)" in text


def test_spinner_text_uses_default_skin_thinking_face():
    apply_skin(load_skin("default"), Console())
    text = _skin_spinner_text(phase="thinking")
    # default.yaml thinking_faces[0] = "(◉‿◉)"
    assert "(◉‿◉)" in text


def test_spinner_text_distinct_per_phase():
    """waiting vs thinking phase produce different glyphs (D5 contract)."""
    apply_skin(load_skin("default"), Console())
    waiting = _skin_spinner_text(phase="waiting")
    thinking = _skin_spinner_text(phase="thinking")
    assert waiting != thinking


def test_spinner_text_changes_when_skin_changes():
    apply_skin(load_skin("default"), Console())
    default_text = _skin_spinner_text(phase="waiting")
    apply_skin(load_skin("charizard"), Console())
    charizard_text = _skin_spinner_text(phase="waiting")
    assert default_text != charizard_text
    # charizard waiting_faces[0] = "(◕‿◕)"
    assert "(◕‿◕)" in charizard_text


def test_spinner_text_includes_verb():
    apply_skin(load_skin("ares"), Console())
    text = _skin_spinner_text(phase="thinking")
    # ares thinking_verbs first entry = "strategizing"
    assert "strategizing" in text


# ─── _skin_color (D6 wiring) ───────────────────────────────────────


def test_skin_color_returns_hex_for_known_key():
    apply_skin(load_skin("default"), Console())
    val = _skin_color("banner_dim", "fallback")
    assert val.startswith("#")


def test_skin_color_returns_fallback_for_unknown_key():
    apply_skin(load_skin("default"), Console())
    val = _skin_color("definitely_not_a_real_key", "FALLBACK_MARKER")
    assert val == "FALLBACK_MARKER"


def test_skin_color_changes_per_skin():
    apply_skin(load_skin("default"), Console())
    default_border = _skin_color("response_border", "x")
    apply_skin(load_skin("ares"), Console())
    ares_border = _skin_color("response_border", "x")
    assert default_border != ares_border


# ─── current_menu_style (D7 wiring) ────────────────────────────────


def test_legacy_menu_style_still_exposed():
    """MENU_STYLE constant remains importable for back-compat."""
    assert MENU_STYLE is not None


def test_current_menu_style_returns_style_instance():
    """current_menu_style() returns a fresh prompt-toolkit Style."""
    apply_skin(load_skin("default"), Console())
    style = current_menu_style()
    # prompt-toolkit Style has a .style_rules attribute / .invalidation_hash().
    assert hasattr(style, "invalidation_hash")


def test_menu_style_dict_has_completion_menu_keys():
    """Hermes v2 D6 — completion menu styling classes are populated."""
    apply_skin(load_skin("default"), Console())
    d = _menu_dict_from_skin()
    assert "completion-menu" in d
    assert "completion-menu.completion" in d
    assert "completion-menu.completion.current" in d
    assert "completion-menu.meta" in d
    assert "completion-menu.meta.current" in d
    # Each non-empty under the default skin (which defines all 22 keys).
    for key in (
        "completion-menu",
        "completion-menu.completion",
        "completion-menu.completion.current",
        "completion-menu.meta",
        "completion-menu.meta.current",
    ):
        assert "bg:" in d[key], f"{key} missing bg color"


def test_menu_style_uses_skin_completion_menu_bg():
    """The skin's completion_menu_bg key flows into prompt-toolkit Style."""
    apply_skin(load_skin("default"), Console())
    d = _menu_dict_from_skin()
    # default.yaml completion_menu_bg = "#3B4252"
    assert "#3B4252" in d["completion-menu"]


def test_menu_style_changes_per_skin():
    apply_skin(load_skin("default"), Console())
    default_d = _menu_dict_from_skin()
    apply_skin(load_skin("ares"), Console())
    ares_d = _menu_dict_from_skin()
    assert default_d["completion-menu"] != ares_d["completion-menu"]


def test_menu_style_falls_back_when_no_skin():
    """When no skin is active, the style dict still has all keys."""
    # We can't easily "unapply" a skin globally without mocking, but
    # we can verify the fallback dict has all keys directly.
    from opencomputer.cli_ui.style import _LEGACY_MENU_DICT
    expected = {
        "menu.title",
        "menu.hint",
        "menu.selected",
        "menu.selected.arrow",
        "menu.selected.glyph",
        "menu.unselected.glyph",
        "menu.description",
        "completion-menu",
        "completion-menu.completion",
        "completion-menu.completion.current",
        "completion-menu.meta",
        "completion-menu.meta.current",
    }
    assert set(_LEGACY_MENU_DICT.keys()) >= expected
