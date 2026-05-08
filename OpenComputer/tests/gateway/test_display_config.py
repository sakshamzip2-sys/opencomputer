"""Tests for per-platform display config (Task B1).

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T2.1)
"""
from __future__ import annotations

import pytest

from opencomputer.gateway.display_config import (
    OVERRIDEABLE_KEYS,
    migrate_legacy_overrides,
    resolve_display_setting,
)

# ── Resolution order ───────────────────────────────────────────────────────


def test_per_platform_override_wins_over_global():
    cfg = {
        "display": {
            "tool_progress": "all",  # global
            "platforms": {"telegram": {"tool_progress": "off"}},
        }
    }
    assert resolve_display_setting(cfg, "telegram", "tool_progress") == "off"


def test_global_user_override_wins_over_tier_default():
    cfg = {"display": {"tool_progress": "off"}}
    # Telegram tier default is "all"; user globally set "off".
    assert resolve_display_setting(cfg, "telegram", "tool_progress") == "off"


def test_tier_default_wins_over_global_default():
    cfg = {}
    # Slack tier default for tool_progress is "off" (overrides global "all").
    assert resolve_display_setting(cfg, "slack", "tool_progress") == "off"


def test_global_default_when_no_tier_or_user():
    cfg = {}
    # Unknown platform → global default "all".
    assert resolve_display_setting(cfg, "no_such_platform", "tool_progress") == "all"


def test_fallback_when_setting_unknown():
    cfg = {}
    assert resolve_display_setting(cfg, "telegram", "made_up_setting", fallback="xyz") == "xyz"


# ── Tier defaults ──────────────────────────────────────────────────────────


def test_tier_high_telegram_discord():
    assert resolve_display_setting({}, "telegram", "tool_progress") == "all"
    assert resolve_display_setting({}, "discord", "tool_progress") == "all"
    assert resolve_display_setting({}, "telegram", "tool_preview_length") == 40


def test_tier_medium_mattermost_matrix_feishu():
    for plat in ("mattermost", "matrix", "feishu"):
        assert resolve_display_setting({}, plat, "tool_progress") == "new"


def test_slack_special_case_off():
    """Slack overrides medium-tier with tool_progress=off."""
    assert resolve_display_setting({}, "slack", "tool_progress") == "off"


def test_tier_low_signal_etc():
    for plat in ("signal", "bluebubbles", "weixin", "wecom", "dingtalk"):
        assert resolve_display_setting({}, plat, "tool_progress") == "off"
        assert resolve_display_setting({}, plat, "streaming") is False


def test_tier_minimal_email_sms_webhook_homeassistant():
    for plat in ("email", "sms", "webhook", "homeassistant"):
        assert resolve_display_setting({}, plat, "tool_progress") == "off"
        assert resolve_display_setting({}, plat, "streaming") is False


# ── Per-knob coverage ──────────────────────────────────────────────────────


def test_overrideable_keys_includes_required_set():
    required = {
        "tool_progress",
        "show_reasoning",
        "tool_preview_length",
        "streaming",
        "background_process_notifications",
        "busy_ack_enabled",
        "busy_input_mode",
        "runtime_footer",
    }
    assert required <= OVERRIDEABLE_KEYS


def test_background_process_notifications_global_default():
    assert resolve_display_setting({}, "telegram", "background_process_notifications") == "all"


def test_background_process_notifications_user_global():
    cfg = {"display": {"background_process_notifications": "result"}}
    assert resolve_display_setting(cfg, "telegram", "background_process_notifications") == "result"


def test_background_process_notifications_per_platform():
    cfg = {
        "display": {
            "background_process_notifications": "all",
            "platforms": {"slack": {"background_process_notifications": "error"}},
        }
    }
    assert resolve_display_setting(cfg, "slack", "background_process_notifications") == "error"
    assert resolve_display_setting(cfg, "telegram", "background_process_notifications") == "all"


def test_runtime_footer_default_disabled():
    val = resolve_display_setting({}, "telegram", "runtime_footer")
    assert isinstance(val, dict)
    assert val["enabled"] is False
    assert "model" in val["fields"]


def test_busy_ack_enabled_user_disable():
    cfg = {"display": {"busy_ack_enabled": False}}
    assert resolve_display_setting(cfg, "telegram", "busy_ack_enabled") is False


# ── Migration ──────────────────────────────────────────────────────────────


def test_migrate_legacy_overrides_moves_flat_dict():
    cfg = {
        "display": {
            "tool_progress_overrides": {
                "telegram": "all",
                "slack": "off",
            }
        }
    }
    migrated = migrate_legacy_overrides(cfg)
    assert (
        migrated["display"]["platforms"]["telegram"]["tool_progress"] == "all"
    )
    assert migrated["display"]["platforms"]["slack"]["tool_progress"] == "off"
    assert "tool_progress_overrides" not in migrated["display"]


def test_migrate_idempotent():
    cfg = {"display": {"tool_progress_overrides": {"telegram": "all"}}}
    once = migrate_legacy_overrides(cfg)
    twice = migrate_legacy_overrides(once)
    assert once == twice


def test_migrate_no_legacy_returns_unchanged():
    cfg = {"display": {"platforms": {"telegram": {"tool_progress": "all"}}}}
    out = migrate_legacy_overrides(cfg)
    # Either equal or carries an "platforms" with same content.
    assert (
        out["display"]["platforms"]["telegram"]["tool_progress"] == "all"
    )


def test_migrate_handles_missing_display():
    assert migrate_legacy_overrides({}) == {"display": {}}
