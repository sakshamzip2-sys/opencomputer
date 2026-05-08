"""Tests for BashTool infrastructure-var blocklist (Hermes parity)."""
from __future__ import annotations

from opencomputer.tools.bash import _strip_infra_env_vars


def test_strips_telegram_bot_token():
    env = {"PATH": "/usr/bin", "TELEGRAM_BOT_TOKEN": "1234:secret"}
    out = _strip_infra_env_vars(env)
    assert "PATH" in out
    assert "TELEGRAM_BOT_TOKEN" not in out


def test_strips_discord_bot_token():
    env = {"PATH": "/usr/bin", "DISCORD_BOT_TOKEN": "secret"}
    out = _strip_infra_env_vars(env)
    assert "DISCORD_BOT_TOKEN" not in out


def test_strips_gateway_allowlist():
    env = {
        "PATH": "/usr/bin",
        "GATEWAY_ALLOW_ALL_USERS": "true",
        "GATEWAY_ALLOWED_USERS": "1,2,3",
    }
    out = _strip_infra_env_vars(env)
    assert "GATEWAY_ALLOW_ALL_USERS" not in out
    assert "GATEWAY_ALLOWED_USERS" not in out


def test_strips_opencomputer_prefix():
    env = {
        "PATH": "/usr/bin",
        "OPENCOMPUTER_PROFILE": "default",
        "OPENCOMPUTER_LOG_LEVEL": "DEBUG",
    }
    out = _strip_infra_env_vars(env)
    assert "PATH" in out
    assert "OPENCOMPUTER_PROFILE" not in out
    assert "OPENCOMPUTER_LOG_LEVEL" not in out


def test_strips_hermes_prefix():
    env = {"PATH": "/usr/bin", "HERMES_HOME": "/x", "HERMES_API": "y"}
    out = _strip_infra_env_vars(env)
    assert "HERMES_HOME" not in out
    assert "HERMES_API" not in out


def test_strips_oc_prefix():
    env = {"PATH": "/usr/bin", "OC_REDACT_RUNTIME": "true", "OC_PROFILE": "p"}
    out = _strip_infra_env_vars(env)
    assert "OC_REDACT_RUNTIME" not in out
    assert "OC_PROFILE" not in out


def test_passes_through_user_third_party_keys():
    """User scripts need NPM_TOKEN, AWS_*, GH_TOKEN etc. — pass through."""
    env = {
        "PATH": "/usr/bin",
        "NPM_TOKEN": "npm-secret",
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "...",
        "GH_TOKEN": "ghp_user-supplied",
        "DATABASE_URL": "postgres://...",
    }
    out = _strip_infra_env_vars(env)
    for k in env:
        assert k in out, f"user var {k} should pass through but was stripped"


def test_passes_through_provider_keys_for_user_scripts():
    """User scripts may legitimately need provider keys (running Anthropic SDK,
    etc.) — pass through. Block list is OC infrastructure only."""
    env = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "sk-ant-user",
        "OPENAI_API_KEY": "sk-user",
        "OPENROUTER_API_KEY": "user",
    }
    out = _strip_infra_env_vars(env)
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        assert k in out, f"provider key {k} should pass through (user-script use)"


def test_strips_opencomputer_allow_root_gateway():
    env = {"PATH": "/usr/bin", "OPENCOMPUTER_ALLOW_ROOT_GATEWAY": "1"}
    out = _strip_infra_env_vars(env)
    assert "OPENCOMPUTER_ALLOW_ROOT_GATEWAY" not in out


def test_none_env_returns_none():
    assert _strip_infra_env_vars(None) is None


def test_empty_env_returns_empty():
    assert _strip_infra_env_vars({}) == {}
