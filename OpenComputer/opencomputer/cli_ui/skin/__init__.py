"""Skins: visual theme for the CLI chat REPL.

Resolution order:
  1. User skin at ``~/.opencomputer/skins/<name>.yaml``
  2. Built-in skin at ``opencomputer/cli_ui/skin/builtins/<name>.yaml``
  3. ``default`` (always available)

Apply via ``apply_skin(spec, console)``. Idempotent — calling again
with a different spec swaps everything live.

Skinning is scoped to the chat REPL surface (palette, spinner,
branding, banner, tool prefix, tool emojis). Channel adapters
(Telegram, Discord) reuse only the branding string; everything else is
a no-op there. The dashboard / TUI custom-painted views are NOT
skin-aware in v1.
"""
from __future__ import annotations

from .apply import (
    apply_skin,
    current_branding,
    current_spec,
    current_spinner_verbs,
    current_spinner_wings,
    current_tool_emojis,
    current_tool_prefix,
)
from .loader import DEFAULT_NAME, USER_SKINS_DIR, list_builtin_names, load_skin
from .spec import SkinSpec

__all__ = [
    "DEFAULT_NAME",
    "USER_SKINS_DIR",
    "SkinSpec",
    "apply_skin",
    "current_branding",
    "current_spec",
    "current_spinner_verbs",
    "current_spinner_wings",
    "current_tool_emojis",
    "current_tool_prefix",
    "list_builtin_names",
    "load_skin",
]
