"""Apply a SkinSpec to a Rich Console + module-level renderer hooks.

The chat REPL renderers (streaming, banner, prompt) read theme + branding
+ spinner verbs from this module's process-global state. ``apply_skin``
mutates that state and binds the corresponding Rich Theme onto the
provided Console.
"""
from __future__ import annotations

import logging
import threading

from rich.console import Console
from rich.style import Style
from rich.theme import Theme

from .spec import SkinSpec

logger = logging.getLogger("opencomputer.cli_ui.skin")

_lock = threading.Lock()
_active_spec: SkinSpec | None = None
_active_branding: dict[str, str] = {}
_active_spinner_verbs: tuple[str, ...] = ("thinking",)
_active_spinner_wings: tuple[tuple[str, str], ...] = (("⟨", "⟩"),)
# Hermes v2 D5 (2026-05-08): distinct face cycles for the waiting
# (network/API round-trip) and thinking (model reasoning) phases.
_active_spinner_waiting_faces: tuple[str, ...] = ()
_active_spinner_thinking_faces: tuple[str, ...] = ()
_active_tool_emojis: dict[str, str] = {}
_active_tool_prefix: str = "┊"


def _safe_style(value: str) -> Style | None:
    try:
        return Style.parse(value)
    except Exception as exc:  # noqa: BLE001 — Rich raises various
        logger.warning("skin: invalid style %r — %s", value, exc)
        return None


def _theme_from_colors(colors: dict[str, str]) -> Theme:
    styles: dict[str, Style] = {}
    for key, value in colors.items():
        s = _safe_style(value)
        if s is not None:
            styles[key] = s
    return Theme(styles, inherit=True)


def apply_skin(spec: SkinSpec, console: Console) -> None:
    """Bind ``spec`` to ``console`` and update process-global renderer state.

    Idempotent. Bad hex colors are skipped with a warning; everything
    else continues to apply.
    """
    global _active_spec, _active_branding
    global _active_spinner_verbs, _active_spinner_wings
    global _active_spinner_waiting_faces, _active_spinner_thinking_faces
    global _active_tool_emojis, _active_tool_prefix

    with _lock:
        try:
            theme = _theme_from_colors(spec.colors)
            console.push_theme(theme)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skin: theme push failed — %s", exc)

        _active_spec = spec
        _active_branding = {
            "agent_name": spec.agent_name,
            "response_label": spec.response_label,
            "prompt_symbol": spec.prompt_symbol,
        }
        _active_spinner_verbs = tuple(spec.spinner_thinking_verbs)
        _active_spinner_wings = tuple(spec.spinner_wings)
        _active_spinner_waiting_faces = tuple(spec.spinner_waiting_faces)
        _active_spinner_thinking_faces = tuple(spec.spinner_thinking_faces)
        _active_tool_emojis = dict(spec.tool_emojis)
        _active_tool_prefix = spec.tool_prefix


def current_spec() -> SkinSpec | None:
    return _active_spec


def current_branding() -> dict[str, str]:
    return dict(_active_branding)


def current_spinner_verbs() -> tuple[str, ...]:
    return _active_spinner_verbs


def current_spinner_wings() -> tuple[tuple[str, str], ...]:
    return _active_spinner_wings


def current_spinner_waiting_faces() -> tuple[str, ...]:
    """Faces cycled while waiting for the provider's first byte.

    Returns the active skin's ``spinner.waiting_faces`` tuple, or an
    empty tuple when the active skin doesn't define any (renderers
    should fall back to ``current_spinner_thinking_faces`` or the
    legacy spinner-glyph machinery in that case).
    """
    return _active_spinner_waiting_faces


def current_spinner_thinking_faces() -> tuple[str, ...]:
    """Faces cycled once the model starts emitting reasoning content."""
    return _active_spinner_thinking_faces


def current_tool_emojis() -> dict[str, str]:
    return dict(_active_tool_emojis)


def current_tool_prefix() -> str:
    return _active_tool_prefix


__all__ = [
    "apply_skin",
    "current_spec",
    "current_branding",
    "current_spinner_verbs",
    "current_spinner_wings",
    "current_spinner_waiting_faces",
    "current_spinner_thinking_faces",
    "current_tool_emojis",
    "current_tool_prefix",
]
