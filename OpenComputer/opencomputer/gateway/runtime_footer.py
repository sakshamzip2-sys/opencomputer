"""Per-platform runtime-metadata footer (Wave 5 T4).

Hermes-port from ``e123f4ecf``. Optional one-line footer appended to the
last assistant message of each turn — surfacing model name, context-window
fill percentage, and the agent's working directory. Default disabled so
existing deployments see no change; opt-in via
``display.runtime_footer.enabled = true`` (with optional per-platform
overrides under ``display.platforms.<name>.runtime_footer.enabled``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class FooterConfig:
    """Resolved per-platform footer enablement."""

    enabled: bool


def resolve_footer_config(
    cfg: dict,
    *,
    platform: str | None = None,
) -> FooterConfig:
    """Resolve effective footer enablement for ``platform``.

    Looks up ``display.runtime_footer.enabled`` (default False), then
    overlays ``display.platforms.<platform>.runtime_footer.enabled`` when
    the platform is supplied. Both layers are optional. Anything missing
    or non-boolean degrades to False.
    """
    display = cfg.get("display") or {}
    base = display.get("runtime_footer") or {}
    enabled = bool(base.get("enabled", False))
    if platform:
        plat = (display.get("platforms") or {}).get(platform) or {}
        plat_footer = plat.get("runtime_footer") or {}
        if "enabled" in plat_footer:
            enabled = bool(plat_footer["enabled"])
    return FooterConfig(enabled=enabled)


def format_runtime_footer(
    *,
    model: str,
    tokens_used: int,
    context_length: int | None,
    cwd: str,
) -> str:
    """Render a single ``model · pct% · ~/cwd`` line for an assistant reply.

    Empty-output policy: when both ``model`` and ``cwd`` are blank, return
    an empty string so callers don't accidentally append a stray glyph.
    Pct is omitted when ``context_length`` is falsy (model unknown or
    provider doesn't surface a context window). cwd is shortened relative
    to ``$HOME`` so the line is compact in chat surfaces.
    """
    if not model and not cwd:
        return ""
    parts: list[str] = []
    if model:
        parts.append(model)
    if context_length and tokens_used >= 0:
        # Round to nearest int — 7.5% renders as 8% (matches Hermes ref).
        pct = round(100.0 * tokens_used / context_length)
        parts.append(f"{pct}%")
    if cwd:
        parts.append(_shorten_cwd(cwd))
    if not parts:
        return ""
    return " · ".join(parts)


def _shorten_cwd(cwd: str) -> str:
    """Replace the user's home prefix with ``~`` for a compact path."""
    home = os.path.expanduser("~")
    if cwd.startswith(home):
        return "~" + cwd[len(home):]
    return cwd


def should_send_busy_ack(cfg: dict) -> bool:
    """``display.busy_ack_enabled`` knob (default True).

    Default-on preserves the historical UX where the gateway tells the
    user "got it, working on it" after long replies. Set to False to
    suppress the explicit ack and rely on typing indicators alone.
    """
    return bool((cfg.get("display") or {}).get("busy_ack_enabled", True))


__all__ = [
    "FooterConfig",
    "format_runtime_footer",
    "resolve_footer_config",
    "should_send_busy_ack",
]
