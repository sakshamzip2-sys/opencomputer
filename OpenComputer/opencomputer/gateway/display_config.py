"""Per-platform display config — port of Hermes ``gateway/display_config.py``.

Single entry point ``resolve_display_setting`` for reading display knobs
(tool_progress, show_reasoning, tool_preview_length, streaming,
runtime_footer, background_process_notifications, busy_ack_enabled,
busy_input_mode) with per-platform tier defaults + user overrides.

Resolution order (first non-None wins):
    1. ``display.platforms.<platform>.<key>``  — explicit per-platform user override
    2. ``display.<key>``                       — global user setting
    3. ``_PLATFORM_DEFAULTS[<platform>][<key>]``  — built-in tier default
    4. ``_GLOBAL_DEFAULTS[<key>]``              — built-in global default
    5. caller-supplied fallback

Migration: ``display.tool_progress_overrides`` (legacy flat dict) auto-
migrates into ``display.platforms.<platform>.tool_progress``.

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T2.1)
"""
from __future__ import annotations

from typing import Any

# ── Global defaults — apply to any platform missing a tier override ────────

_GLOBAL_DEFAULTS: dict[str, Any] = {
    "tool_progress": "all",            # all | new | off | verbose
    "show_reasoning": False,
    "tool_preview_length": 0,
    "streaming": None,                 # None = follow top-level streaming
    "background_process_notifications": "all",  # all | result | error | off
    "busy_ack_enabled": True,
    "busy_input_mode": "interrupt",    # interrupt | queue | steer
    "runtime_footer": {                # opt-in metadata footer
        "enabled": False,
        "fields": ["model", "context_pct", "cwd"],
    },
}

# ── Tier-based per-platform defaults — Hermes-spec parity ──────────────────
# Tier 1 (high)    — supports message edits, personal/team use → spammy ok
# Tier 2 (medium)  — supports edits but workspace/customer-facing
# Tier 3 (low)     — no edit support; each progress msg is permanent
# Tier 4 (minimal) — batch / non-interactive delivery

_TIER_HIGH: dict[str, Any] = {
    "tool_progress": "all",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": None,
}
_TIER_MEDIUM: dict[str, Any] = {
    "tool_progress": "new",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": None,
}
_TIER_LOW: dict[str, Any] = {
    "tool_progress": "off",
    "show_reasoning": False,
    "tool_preview_length": 40,
    "streaming": False,
}
_TIER_MINIMAL: dict[str, Any] = {
    "tool_progress": "off",
    "show_reasoning": False,
    "tool_preview_length": 0,
    "streaming": False,
}

_PLATFORM_DEFAULTS: dict[str, dict[str, Any]] = {
    # Tier 1 — full edit support, personal use
    "telegram": _TIER_HIGH,
    "discord": _TIER_HIGH,

    # Tier 2 — edit support, workspace/customer channels
    # Slack: tool_progress off — Bolt posts can't be edited like CLI;
    # 'new'/'all' would spam permanent lines.
    "slack": {**_TIER_MEDIUM, "tool_progress": "off"},
    "mattermost": _TIER_MEDIUM,
    "matrix": _TIER_MEDIUM,
    "feishu": _TIER_MEDIUM,
    # WhatsApp Baileys bridge supports /edit → Tier 2.
    "whatsapp": _TIER_MEDIUM,

    # Tier 3 — no edit support, progress messages permanent
    "signal": _TIER_LOW,
    "bluebubbles": _TIER_LOW,
    "weixin": _TIER_LOW,
    "wecom": _TIER_LOW,
    "wecom_callback": _TIER_LOW,
    "dingtalk": _TIER_LOW,
    "qq": _TIER_LOW,
    "irc": _TIER_LOW,
    "yuanbao": _TIER_LOW,

    # Tier 4 — batch/non-interactive
    "email": _TIER_MINIMAL,
    "sms": _TIER_MINIMAL,
    "webhook": _TIER_MINIMAL,
    "homeassistant": _TIER_MINIMAL,
    "teams": _TIER_MINIMAL,

    # API server / CLI — full surface, but no preview text in JSON.
    "api_server": {**_TIER_HIGH, "tool_preview_length": 0},
    "cli": {**_TIER_HIGH, "tool_preview_length": 0},
}

#: Set of keys that callers may resolve via ``resolve_display_setting``.
OVERRIDEABLE_KEYS = frozenset(_GLOBAL_DEFAULTS.keys())


def resolve_display_setting(
    user_config: dict | None,
    platform_key: str | None,
    setting: str,
    fallback: Any = None,
) -> Any:
    """Resolve a display setting with per-platform override support.

    Parameters
    ----------
    user_config
        The full top-level user config dict (loaded from YAML). May be
        ``None`` (treated as empty).
    platform_key
        Lowercase platform name ('telegram', 'discord', ...). ``None`` or
        empty string means "global / CLI" — skips the per-platform layer.
    setting
        Name of the knob (one of ``OVERRIDEABLE_KEYS``).
    fallback
        Returned when no layer provides a value (rare — global defaults
        cover every key in OVERRIDEABLE_KEYS).
    """
    cfg = (user_config or {}).get("display") or {}

    # 1. Per-platform user override.
    if platform_key:
        plat_cfg = (cfg.get("platforms") or {}).get(platform_key)
        if isinstance(plat_cfg, dict) and setting in plat_cfg:
            return plat_cfg[setting]

    # 2. Global user setting.
    if setting in cfg:
        return cfg[setting]

    # 3. Built-in tier default.
    if platform_key and platform_key in _PLATFORM_DEFAULTS:
        plat_default = _PLATFORM_DEFAULTS[platform_key]
        if setting in plat_default:
            return plat_default[setting]

    # 4. Built-in global default.
    if setting in _GLOBAL_DEFAULTS:
        return _GLOBAL_DEFAULTS[setting]

    # 5. Caller fallback.
    return fallback


def migrate_legacy_overrides(cfg: dict) -> dict:
    """Move ``display.tool_progress_overrides`` (flat dict of platform→value)
    into ``display.platforms.<platform>.tool_progress``.

    Returns a NEW dict (does not mutate ``cfg``). Idempotent: re-running
    on a migrated config is a no-op. Callers that want migration should
    wrap their YAML load with this.
    """
    if not isinstance(cfg, dict):
        return cfg
    new_cfg = dict(cfg)
    display = dict(new_cfg.get("display") or {})
    legacy = display.get("tool_progress_overrides")
    if isinstance(legacy, dict) and legacy:
        platforms = dict(display.get("platforms") or {})
        for plat, value in legacy.items():
            if not isinstance(plat, str):
                continue
            plat_cfg = dict(platforms.get(plat) or {})
            plat_cfg.setdefault("tool_progress", value)
            platforms[plat] = plat_cfg
        display["platforms"] = platforms
        # Drop the legacy key so subsequent loads don't re-migrate.
        display.pop("tool_progress_overrides", None)
    new_cfg["display"] = display
    return new_cfg


__all__ = [
    "OVERRIDEABLE_KEYS",
    "resolve_display_setting",
    "migrate_legacy_overrides",
]
