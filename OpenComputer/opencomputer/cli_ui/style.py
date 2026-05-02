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
"""
from __future__ import annotations

from prompt_toolkit.styles import Style

MENU_STYLE = Style.from_dict({
    "menu.title": "fg:#ffd75f bold",
    "menu.hint": "fg:#888888",
    "menu.selected": "fg:#5fff5f",
    "menu.selected.arrow": "fg:#5fff5f bold",
    "menu.selected.glyph": "fg:#5fff5f bold",
    "menu.unselected.glyph": "",
    "menu.description": "fg:#888888 italic",
})

ARROW_GLYPH = "→"
RADIO_ON = "●"
RADIO_OFF = "○"
CHECK_ON = "✓"
CHECK_OFF = " "
