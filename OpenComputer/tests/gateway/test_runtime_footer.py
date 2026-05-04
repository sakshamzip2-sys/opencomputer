"""Tests for ``opencomputer.gateway.runtime_footer`` (Wave 5 T4)."""

from __future__ import annotations

from opencomputer.gateway.runtime_footer import (
    FooterConfig,
    format_runtime_footer,
    resolve_footer_config,
    should_send_busy_ack,
)


def test_footer_default_disabled():
    cfg = resolve_footer_config({})
    assert cfg.enabled is False


def test_footer_enabled_at_top_level():
    cfg = resolve_footer_config({"display": {"runtime_footer": {"enabled": True}}})
    assert cfg.enabled is True


def test_footer_renders_pct():
    line = format_runtime_footer(
        model="claude-opus-4-7",
        tokens_used=15000,
        context_length=200000,
        cwd="/Users/saksham/projects/hermes",
    )
    assert "claude-opus-4-7" in line
    assert "8%" in line  # 15000/200000 = 7.5% rounds to 8
    assert "hermes" in line


def test_footer_no_pct_when_context_unknown():
    line = format_runtime_footer(
        model="unknown-model",
        tokens_used=100,
        context_length=None,
        cwd="/x",
    )
    assert "%" not in line
    assert "unknown-model" in line


def test_footer_empty_returns_empty():
    assert (
        format_runtime_footer(model="", tokens_used=0, context_length=0, cwd="")
        == ""
    )


def test_per_platform_override_overrides_base():
    cfg = resolve_footer_config(
        {
            "display": {
                "runtime_footer": {"enabled": False},
                "platforms": {
                    "telegram": {"runtime_footer": {"enabled": True}},
                },
            }
        },
        platform="telegram",
    )
    assert cfg.enabled is True


def test_per_platform_override_can_force_off():
    cfg = resolve_footer_config(
        {
            "display": {
                "runtime_footer": {"enabled": True},
                "platforms": {
                    "discord": {"runtime_footer": {"enabled": False}},
                },
            }
        },
        platform="discord",
    )
    assert cfg.enabled is False


def test_footer_dataclass_is_frozen():
    cfg = FooterConfig(enabled=True)
    import pytest

    with pytest.raises(Exception):
        cfg.enabled = False  # type: ignore[misc]


def test_busy_ack_default_enabled():
    assert should_send_busy_ack({}) is True


def test_busy_ack_explicit_false():
    cfg = {"display": {"busy_ack_enabled": False}}
    assert should_send_busy_ack(cfg) is False


def test_busy_ack_explicit_true():
    cfg = {"display": {"busy_ack_enabled": True}}
    assert should_send_busy_ack(cfg) is True


def test_busy_ack_legacy_no_display_block():
    """Configs that predate the display section default to True."""
    assert should_send_busy_ack({"other_section": {}}) is True
