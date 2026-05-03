"""ARIA-role classification — verbatim port of OpenClaw's snapshot-roles.ts.

17 interactive + 10 content + 19 structural = 46 classified roles.

Rules used by the snapshot builders:
  - INTERACTIVE_ROLES → always get a ref (even when unnamed).
  - CONTENT_ROLES → ref only when `name` is non-empty.
  - STRUCTURAL_ROLES → never get a ref. Dropped entirely under
    `compact=True` when also unnamed.
  - Everything else (e.g. paragraph, text, img) → kept verbatim, no ref.

These three sets are intentionally `frozenset`s — they're read in hot
paths (every snapshot line tested for membership) and must not be mutated.
"""

from __future__ import annotations

from typing import Final

INTERACTIVE_ROLES: Final[frozenset[str]] = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "listbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
        "treeitem",
    }
)

CONTENT_ROLES: Final[frozenset[str]] = frozenset(
    {
        "article",
        "cell",
        "columnheader",
        "gridcell",
        "heading",
        "listitem",
        "main",
        "navigation",
        "region",
        "rowheader",
    }
)

STRUCTURAL_ROLES: Final[frozenset[str]] = frozenset(
    {
        "application",
        "directory",
        "document",
        "generic",
        "grid",
        "group",
        "ignored",
        "list",
        "menu",
        "menubar",
        "none",
        "presentation",
        "row",
        "rowgroup",
        "table",
        "tablist",
        "toolbar",
        "tree",
        "treegrid",
    }
)


# Sanity check — fails import if the verbatim port drifts.
assert len(INTERACTIVE_ROLES) == 17, f"interactive role count drifted: {len(INTERACTIVE_ROLES)}"
assert len(CONTENT_ROLES) == 10, f"content role count drifted: {len(CONTENT_ROLES)}"
assert len(STRUCTURAL_ROLES) == 19, f"structural role count drifted: {len(STRUCTURAL_ROLES)}"
