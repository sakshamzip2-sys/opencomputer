"""prompt_toolkit Style rules for Hermes-modeled menus.

Visual register:
  - title (yellow, bold) - the "Select provider:" heading
  - hint (dim) - the navigation hint footer
  - selected (green) - current row arrow + text
  - selected.glyph (green bold) - selected radio/check glyphs
  - unselected.glyph (default) - unselected radio/check glyphs
  - description (dim italic) - optional description block under title

Single source for re-skinning. All menu primitives in cli_ui/menu.py
reference these class names.
"""
from __future__ import annotations

import logging

from prompt_toolkit.styles import Style

logger = logging.getLogger("opencomputer.cli_ui.style")

_NO_HIGHLIGHT = "bg:ansidefault noreverse noinherit"

# Derived once per module import as the no-skin fallback. Modern call sites
# should use current_menu_style() to pick up live skin changes; old call sites
# that grabbed MENU_STYLE at import time still get the default palette.
_LEGACY_MENU_DICT = {
    "menu.title": f"fg:#ffd75f bold {_NO_HIGHLIGHT}",
    "menu.hint": f"fg:#888888 {_NO_HIGHLIGHT}",
    "menu.selected": f"fg:#5fff5f bold {_NO_HIGHLIGHT}",
    "menu.selected.arrow": f"fg:#5fff5f bold {_NO_HIGHLIGHT}",
    "menu.selected.glyph": f"fg:#5fff5f bold {_NO_HIGHLIGHT}",
    "menu.unselected.glyph": _NO_HIGHLIGHT,
    "menu.description": f"fg:#888888 italic {_NO_HIGHLIGHT}",
    "completion-menu": "",
    "completion-menu.completion": "",
    "completion-menu.completion.current": "",
    "completion-menu.meta": "",
    "completion-menu.meta.current": "",
}

MENU_STYLE = Style.from_dict(_LEGACY_MENU_DICT)


def _menu_dict_from_skin() -> dict[str, str]:
    """Build the prompt-toolkit menu-style dict from the active skin."""
    try:
        from opencomputer.cli_ui.skin import current_spec
    except Exception:  # noqa: BLE001 - never break menu render
        return dict(_LEGACY_MENU_DICT)
    try:
        spec = current_spec()
    except Exception:  # noqa: BLE001
        return dict(_LEGACY_MENU_DICT)
    if spec is None:
        return dict(_LEGACY_MENU_DICT)
    colors = spec.colors

    def _color(key: str, default: str) -> str:
        val = colors.get(key)
        if isinstance(val, str) and val:
            return val
        return default

    title = _color("banner_title", "#ffd75f")
    hint = _color("banner_dim", "#888888")
    ok = _color("ui_ok", "#5fff5f")
    label = _color("ui_label", "#888888")
    cm_bg = _color("completion_menu_bg", "#3B4252")
    cm_cur_bg = _color("completion_menu_current_bg", "#5E81AC")
    cm_meta_bg = _color("completion_menu_meta_bg", "#434C5E")
    cm_meta_cur_bg = _color("completion_menu_meta_current_bg", "#4C566A")
    fg = _color("banner_text", "#FFFFFF")

    return {
        "menu.title": f"fg:{title} bold {_NO_HIGHLIGHT}",
        "menu.hint": f"fg:{hint} {_NO_HIGHLIGHT}",
        "menu.selected": f"fg:{ok} bold {_NO_HIGHLIGHT}",
        "menu.selected.arrow": f"fg:{ok} bold {_NO_HIGHLIGHT}",
        "menu.selected.glyph": f"fg:{ok} bold {_NO_HIGHLIGHT}",
        "menu.unselected.glyph": _NO_HIGHLIGHT,
        "menu.description": f"fg:{label} italic {_NO_HIGHLIGHT}",
        # prompt-toolkit completion menu - fg:bg pairs.
        "completion-menu": f"bg:{cm_bg} fg:{fg}",
        "completion-menu.completion": f"bg:{cm_bg} fg:{fg}",
        "completion-menu.completion.current": f"bg:{cm_cur_bg} fg:{fg}",
        "completion-menu.meta": f"bg:{cm_meta_bg} fg:{label}",
        "completion-menu.meta.current": f"bg:{cm_meta_cur_bg} fg:{label}",
    }


def current_menu_style() -> Style:
    """Return a prompt-toolkit Style reflecting the active skin."""
    return Style.from_dict(_menu_dict_from_skin())


ARROW_GLYPH = "→"
RADIO_ON = "●"
RADIO_OFF = "○"
CHECK_ON = "✓"
CHECK_OFF = " "
