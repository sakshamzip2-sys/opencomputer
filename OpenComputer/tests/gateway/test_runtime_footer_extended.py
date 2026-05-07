"""Extended tests for runtime_footer (Tasks B2 + B4).

Original tests live in ``test_runtime_footer.py``; this file covers the
PR-2 extensions: configurable ``fields`` list, ``append_or_send_trailing``
streaming-aware delivery, and the ``_OnboardingLatch`` first-time-tip
behaviour.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from opencomputer.gateway.runtime_footer import (
    FooterConfig,
    _OnboardingLatch,
    append_or_send_trailing,
    busy_ack_text,
    format_runtime_footer,
    resolve_footer_config,
)

# ── FooterConfig.fields ────────────────────────────────────────────────────


def test_resolve_footer_config_default_fields():
    cfg = resolve_footer_config({})
    assert cfg.enabled is False
    assert cfg.fields == ("model", "context_pct", "cwd")


def test_resolve_footer_config_custom_fields_global():
    cfg = resolve_footer_config(
        {"display": {"runtime_footer": {"enabled": True, "fields": ["model", "cwd"]}}}
    )
    assert cfg.enabled is True
    assert cfg.fields == ("model", "cwd")


def test_resolve_footer_config_per_platform_override():
    cfg = resolve_footer_config(
        {
            "display": {
                "runtime_footer": {"enabled": False, "fields": ["model"]},
                "platforms": {
                    "telegram": {
                        "runtime_footer": {
                            "enabled": True,
                            "fields": ["model", "context_pct"],
                        }
                    }
                },
            }
        },
        platform="telegram",
    )
    assert cfg.enabled is True
    assert cfg.fields == ("model", "context_pct")


def test_resolve_footer_config_partial_per_platform():
    """Per-platform may override only enabled, keeping global fields."""
    cfg = resolve_footer_config(
        {
            "display": {
                "runtime_footer": {"enabled": False, "fields": ["model"]},
                "platforms": {
                    "telegram": {"runtime_footer": {"enabled": True}}
                },
            }
        },
        platform="telegram",
    )
    assert cfg.enabled is True
    assert cfg.fields == ("model",)


# ── format_runtime_footer with fields ──────────────────────────────────────


def test_format_respects_fields_order():
    out = format_runtime_footer(
        model="claude-opus-4-7",
        tokens_used=15000,
        context_length=200000,
        cwd="/Users/saksham/proj",
        fields=("cwd", "model"),
    )
    parts = out.split(" · ")
    assert parts[0].startswith("/Users") or parts[0].startswith("~")
    assert parts[1] == "claude-opus-4-7"


def test_format_drops_empty_fields():
    out = format_runtime_footer(
        model="",
        tokens_used=0,
        context_length=None,
        cwd="",
        fields=("model", "context_pct", "cwd"),
    )
    assert out == ""


def test_format_unknown_fields_silently_dropped():
    out = format_runtime_footer(
        model="opus",
        tokens_used=0,
        context_length=None,
        cwd="",
        fields=("model", "fake_field", "cwd"),
    )
    assert out == "opus"


def test_format_dedupes_repeated_fields():
    out = format_runtime_footer(
        model="opus",
        tokens_used=0,
        context_length=None,
        cwd="",
        fields=("model", "model", "model"),
    )
    assert out == "opus"


# ── append_or_send_trailing ────────────────────────────────────────────────


def test_append_non_streaming_concatenates():
    body, trailing = append_or_send_trailing(
        "Hello world", "model · 12% · ~/proj", streaming=False
    )
    assert body == "Hello world\nmodel · 12% · ~/proj"
    assert trailing is None


def test_append_streaming_returns_separate_trailing():
    body, trailing = append_or_send_trailing(
        "Hello", "footer here", streaming=True
    )
    assert body == "Hello"
    assert trailing == "footer here"


def test_append_empty_footer_passthrough():
    body, trailing = append_or_send_trailing("Hello", "", streaming=False)
    assert body == "Hello"
    assert trailing is None


# ── _OnboardingLatch ───────────────────────────────────────────────────────


def test_latch_seen_false_when_file_missing(tmp_path):
    latch = _OnboardingLatch(tmp_path / "onboarding.json")
    assert latch.seen("any_key") is False


def test_latch_mark_then_seen(tmp_path):
    latch = _OnboardingLatch(tmp_path / "onboarding.json")
    latch.mark_seen("busy_input_prompt")
    assert latch.seen("busy_input_prompt") is True
    # Second mark is idempotent.
    latch.mark_seen("busy_input_prompt")
    data = json.loads((tmp_path / "onboarding.json").read_text())
    assert data["seen"] == {"busy_input_prompt": True}


def test_latch_corrupt_file_treated_as_fresh(tmp_path):
    path = tmp_path / "onboarding.json"
    path.write_text("not json {{{", encoding="utf-8")
    latch = _OnboardingLatch(path)
    assert latch.seen("busy_input_prompt") is False
    latch.mark_seen("busy_input_prompt")
    # File got rewritten cleanly.
    data = json.loads(path.read_text())
    assert data["seen"]["busy_input_prompt"] is True


def test_latch_multiple_keys_independent(tmp_path):
    latch = _OnboardingLatch(tmp_path / "onboarding.json")
    latch.mark_seen("foo")
    assert latch.seen("foo") is True
    assert latch.seen("bar") is False
    latch.mark_seen("bar")
    assert latch.seen("bar") is True


# ── busy_ack_text first-time tip ───────────────────────────────────────────


def test_busy_ack_first_time_includes_tip(tmp_path):
    out = busy_ack_text({}, profile_home=tmp_path)
    assert "First-time tip" in out
    assert "busy_input_mode" in out
    assert "queue|steer|interrupt" in out


def test_busy_ack_second_time_omits_tip(tmp_path):
    busy_ack_text({}, profile_home=tmp_path)  # first call latches
    out = busy_ack_text({}, profile_home=tmp_path)
    assert "First-time tip" not in out
    assert "working" in out


def test_busy_ack_no_profile_home_no_tip():
    """When profile_home is None (e.g., test/CLI scaffolding), the tip
    is NEVER emitted — caller hasn't agreed to a latch location."""
    out = busy_ack_text({}, profile_home=None)
    assert "First-time tip" not in out
