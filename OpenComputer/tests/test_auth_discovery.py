"""T69 — auth.json + Claude Code credentials discovery.

Two surfaces:

  1. ``auth.json`` (Hermes parity) — explicit, env-indirected creds at
     ``$OPENCOMPUTER_HOME/auth/auth.json``. Values like
     ``${ANTHROPIC_API_KEY}`` resolve from environment at read time.

  2. Claude Code creds (drop-in convenience) — auto-discover Anthropic
     credentials from a ``~/.claude/.credentials.json`` shape so users
     who already have Claude Code installed don't need to re-export
     env vars to OC.
"""

from __future__ import annotations

import json

import pytest

from opencomputer.auth.discovery import (
    discover_anthropic_credential,
    load_auth_json,
)


def test_load_auth_json_returns_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert load_auth_json() == {}


def test_load_auth_json_resolves_env_indirection(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("MY_KEY", "sk-real-secret")
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        json.dumps(
            {
                "anthropic": {"api_key": "${MY_KEY}"},
                "openai": {"api_key": "literal-key"},
            }
        )
    )
    payload = load_auth_json()
    assert payload["anthropic"]["api_key"] == "sk-real-secret"
    assert payload["openai"]["api_key"] == "literal-key"


def test_load_auth_json_unset_env_stays_literal(tmp_path, monkeypatch):
    """If ${VAR} resolves to nothing, leave the literal so the caller can detect."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("UNSET_VAR", raising=False)
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        json.dumps({"anthropic": {"api_key": "${UNSET_VAR}"}})
    )
    payload = load_auth_json()
    assert payload["anthropic"]["api_key"] == "${UNSET_VAR}"


def test_discover_anthropic_credential_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    cred = discover_anthropic_credential()
    assert cred is not None
    assert cred["api_key"] == "sk-from-env"
    assert cred["source"] == "env"


def test_discover_anthropic_credential_from_auth_json(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        json.dumps({"anthropic": {"api_key": "sk-from-auth-json"}})
    )
    cred = discover_anthropic_credential()
    assert cred is not None
    assert cred["api_key"] == "sk-from-auth-json"
    assert cred["source"] == "auth.json"


def test_discover_anthropic_credential_from_claude_code(tmp_path, monkeypatch):
    """Drop-in: ~/.claude/.credentials.json is read when env + auth.json empty."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "oc-home"))
    (tmp_path / "oc-home").mkdir()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "oauth-token-abc"}}
        )
    )
    cred = discover_anthropic_credential()
    assert cred is not None
    assert cred["api_key"] == "oauth-token-abc"
    assert cred["source"] == "claude-code"


def test_discover_returns_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "oc-home"))
    (tmp_path / "oc-home").mkdir()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert discover_anthropic_credential() is None


def test_discover_env_takes_precedence_over_auth_json(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env-wins")
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "auth.json").write_text(
        json.dumps({"anthropic": {"api_key": "from-auth-json"}})
    )
    cred = discover_anthropic_credential()
    assert cred["api_key"] == "from-env-wins"
    assert cred["source"] == "env"
