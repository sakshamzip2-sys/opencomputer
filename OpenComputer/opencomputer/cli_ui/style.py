"""prompt_toolkit Style rules for Hermes-modeled menus.

Visual register:
  - title (yellow, bold) — the "Select provider:" heading
  - hint (dim) — the navigation hint footer
  - selected (green) — current row arrow + text
  - selected.glyph (green bold) — (●) / [✓] in the selected row
  - unselected.glyph (default) — (○) / [ ] in unselected rows
  - description (dim italic) — optional description block under title

Single source for re-skinning. All menu primitives in cli_ui/menu.py
reference these class names.

Hermes v2 D6/D7 wiring (2026-05-09): the static ``MENU_STYLE`` was the
only consumer-side gap left after PR #515. ``current_menu_style()``
now derives its colors from the active skin (``banner_title`` for the
title, ``ui_label`` for the hint, ``ui_ok`` for selection, completion
menu panes from ``completion_menu_*`` keys) so ``/skin <name>``
actually changes how menus look. Legacy ``MENU_STYLE`` is preserved as
the no-skin fallback so import sites that pulled the constant directly
continue to work.
"""
from __future__ import annotations

import logging

from prompt_toolkit.styles import Style

logger = logging.getLogger("opencomputer.cli_ui.style")

# Hermes v2 D6/D7 — derived once per module import as the no-skin
# fallback. Modern call sites should use :func:`current_menu_style` to
# pick up live skin changes; old call sites that grabbed ``MENU_STYLE``
# at import time still get a sensible (default-skin) palette.
_LEGACY_MENU_DICT = {
    "menu.title": "fg:#ffd75f bold",
    "menu.hint": "fg:#888888",
    "menu.selected": "fg:#5fff5f",
    "menu.selected.arrow": "fg:#5fff5f bold",
    "menu.selected.glyph": "fg:#5fff5f bold",
    "menu.unselected.glyph": "",
    "menu.description": "fg:#888888 italic",
    # Hermes v2 D6 — completion menu pane styling. prompt-toolkit's
    # default ``completion-menu`` class names are wired here so the
    # autocomplete dropdown picks up the active skin's color keys.
    "completion-menu": "",
    "completion-menu.completion": "",
    "completion-menu.completion.current": "",
    "completion-menu.meta": "",
    "completion-menu.meta.current": "",
}

MENU_STYLE = Style.from_dict(_LEGACY_MENU_DICT)


def _menu_dict_from_skin() -> dict[str, str]:
    """Build the prompt-toolkit menu-style dict from the active skin.

    Falls back to ``_LEGACY_MENU_DICT`` for any skin where a key is
    missing — preserves the legacy palette as the always-present
    safety net.
    """
    try:
        from opencomputer.cli_ui.skin import current_spec
    except Exception:  # noqa: BLE001 — never break menu render
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
        "menu.title": f"fg:{title} bold",
        "menu.hint": f"fg:{hint}",
        "menu.selected": f"fg:{ok}",
        "menu.selected.arrow": f"fg:{ok} bold",
        "menu.selected.glyph": f"fg:{ok} bold",
        "menu.unselected.glyph": "",
        "menu.description": f"fg:{label} italic",
        # prompt-toolkit completion menu — fg:bg pairs.
        "completion-menu": f"bg:{cm_bg} fg:{fg}",
        "completion-menu.completion": f"bg:{cm_bg} fg:{fg}",
        "completion-menu.completion.current": f"bg:{cm_cur_bg} fg:{fg}",
        "completion-menu.meta": f"bg:{cm_meta_bg} fg:{label}",
        "completion-menu.meta.current": f"bg:{cm_meta_cur_bg} fg:{label}",
    }


def current_menu_style() -> Style:
    """Return a prompt-toolkit ``Style`` reflecting the active skin.

    Call this each time you build a menu so that ``/skin <name>`` mid
    session picks up the new palette.
    """
    return Style.from_dict(_menu_dict_from_skin())


ARROW_GLYPH = "→"
RADIO_ON = "●"
RADIO_OFF = "○"
CHECK_ON = "✓"
CHECK_OFF = " "
