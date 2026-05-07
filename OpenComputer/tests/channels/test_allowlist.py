"""Tests for the allowlist gate (Task 1.5).

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T1.5)
Spec: docs/superpowers/specs/2026-05-08-messaging-gateway-parity-design.md (§5.16)
"""
from __future__ import annotations

import json

import pytest

from opencomputer.channels.allowlist import (
    ALLOW_ALL_ENV,
    GLOBAL_ENV,
    PLATFORM_ENV_VARS,
    AllowlistDecision,
    AllowlistGate,
)
from opencomputer.channels.pairing_codes import PairingCodeStore


# ── Helpers ─────────────────────────────────────────────────────────────────


@pytest.fixture
def gate(tmp_path, monkeypatch):
    # Clear all platform + global env vars to start clean.
    monkeypatch.delenv(ALLOW_ALL_ENV, raising=False)
    monkeypatch.delenv(GLOBAL_ENV, raising=False)
    for env in PLATFORM_ENV_VARS.values():
        monkeypatch.delenv(env, raising=False)
    return AllowlistGate(profile_home=tmp_path)


# ── Allow-all escape hatch ─────────────────────────────────────────────────


def test_allow_all_env_grants_unconditional(gate, monkeypatch):
    monkeypatch.setenv(ALLOW_ALL_ENV, "true")
    decision = gate.check("telegram", "anyone")
    assert decision.allowed is True
    assert decision.source == "allow-all"


def test_allow_all_truthy_variants(gate, monkeypatch):
    for val in ("1", "yes", "on", "TRUE", "True"):
        monkeypatch.setenv(ALLOW_ALL_ENV, val)
        assert gate.check("telegram", "x").allowed is True


def test_allow_all_false_does_not_grant(gate, monkeypatch):
    monkeypatch.setenv(ALLOW_ALL_ENV, "false")
    decision = gate.check("telegram", "anyone")
    assert decision.allowed is False


# ── Per-platform env ───────────────────────────────────────────────────────


def test_telegram_env_grants_listed_user(gate, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123,456,789")
    assert gate.check("telegram", "456").allowed is True
    assert gate.check("telegram", "456").source == "env-platform"


def test_telegram_env_denies_unlisted_user(gate, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123")
    assert gate.check("telegram", "999").allowed is False


def test_per_platform_env_isolated(gate, monkeypatch):
    """Discord allowlist must NOT leak into Telegram."""
    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "111")
    assert gate.check("telegram", "111").allowed is False
    assert gate.check("discord", "111").allowed is True


# ── Catch-all env ──────────────────────────────────────────────────────────


def test_global_env_grants_across_platforms(gate, monkeypatch):
    monkeypatch.setenv(GLOBAL_ENV, "777")
    for plat in ("telegram", "discord", "slack"):
        d = gate.check(plat, "777")
        assert d.allowed is True
        assert d.source == "env-global"


# ── File overlay ───────────────────────────────────────────────────────────


def test_file_overlay_grants(gate, tmp_path):
    (tmp_path / "allowlist.json").write_text(
        json.dumps({"telegram": ["888"]}), encoding="utf-8"
    )
    d = gate.check("telegram", "888")
    assert d.allowed is True
    assert d.source == "file"


def test_file_overlay_corrupt_treated_as_empty(gate, tmp_path):
    (tmp_path / "allowlist.json").write_text("not json {{{", encoding="utf-8")
    # Should NOT raise; falls through to deny.
    d = gate.check("telegram", "anyone")
    assert d.allowed is False


# ── DM-pairing approval ───────────────────────────────────────────────────


def test_pairing_approved_grants(gate):
    code = gate.pairing_store.generate_code("telegram", "user_a", "Alice")
    gate.pairing_store.approve_code("telegram", code)
    d = gate.check("telegram", "user_a")
    assert d.allowed is True
    assert d.source == "pairing-approved"


# ── Denied path mints a code ──────────────────────────────────────────────


def test_denied_user_gets_pairing_code(gate):
    d = gate.check("telegram", "stranger", user_name="Stranger")
    assert d.allowed is False
    assert d.source == "denied"
    assert d.pairing_code is not None
    assert len(d.pairing_code) == 8


def test_denied_user_rate_limited_no_code(gate):
    # First DM mints a code.
    first = gate.check("telegram", "stranger")
    assert first.pairing_code is not None
    # Second DM within rate-limit window — None.
    second = gate.check("telegram", "stranger")
    assert second.pairing_code is None


# ── Resolution order ──────────────────────────────────────────────────────


def test_allow_all_beats_everything(gate, monkeypatch):
    monkeypatch.setenv(ALLOW_ALL_ENV, "true")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")  # empty
    d = gate.check("telegram", "x")
    assert d.source == "allow-all"


def test_env_platform_beats_global(gate, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "111")
    monkeypatch.setenv(GLOBAL_ENV, "111")
    d = gate.check("telegram", "111")
    assert d.source == "env-platform"


def test_env_global_beats_file(gate, monkeypatch, tmp_path):
    monkeypatch.setenv(GLOBAL_ENV, "222")
    (tmp_path / "allowlist.json").write_text(
        json.dumps({"telegram": ["222"]}), encoding="utf-8"
    )
    d = gate.check("telegram", "222")
    assert d.source == "env-global"


def test_file_beats_pairing(gate, tmp_path):
    code = gate.pairing_store.generate_code("telegram", "333", "x")
    gate.pairing_store.approve_code("telegram", code)
    (tmp_path / "allowlist.json").write_text(
        json.dumps({"telegram": ["333"]}), encoding="utf-8"
    )
    d = gate.check("telegram", "333")
    assert d.source == "file"


# ── 19 platform env vars ──────────────────────────────────────────────────


@pytest.mark.parametrize("platform,env_var", list(PLATFORM_ENV_VARS.items()))
def test_each_platform_env_var_works(gate, monkeypatch, platform, env_var):
    monkeypatch.setenv(env_var, "u-test")
    d = gate.check(platform, "u-test")
    assert d.allowed is True
    assert d.source == "env-platform"


# ── Non-numeric env var doesn't crash ─────────────────────────────────────


def test_env_with_whitespace_handled(gate, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "  111  ,  222 , ,")
    assert gate.check("telegram", "111").allowed is True
    assert gate.check("telegram", "222").allowed is True
