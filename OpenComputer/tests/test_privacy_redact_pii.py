"""Tests for privacy.redact_pii — gateway PII hashing (Hermes config v2)."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_pii_salt_loaded_or_generated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import _load_or_create_salt

    salt1 = _load_or_create_salt()
    assert len(salt1) == 32
    salt_file = tmp_path / ".pii_salt"
    assert salt_file.exists()
    salt2 = _load_or_create_salt()
    assert salt1 == salt2  # deterministic across calls


def test_hash_user_id_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import hash_user_id

    h1 = hash_user_id("+1-555-123-4567")
    h2 = hash_user_id("+1-555-123-4567")
    assert h1 == h2
    assert h1 != "+1-555-123-4567"
    assert len(h1) >= 12  # not too short to avoid collisions


def test_hash_chat_id_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import hash_chat_id

    h1 = hash_chat_id("123456789")
    h2 = hash_chat_id("123456789")
    assert h1 == h2


def test_hash_user_and_chat_differ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import hash_chat_id, hash_user_id

    u = hash_user_id("123")
    c = hash_chat_id("123")
    assert u != c  # different namespaces


def test_redact_pii_disabled_by_default() -> None:
    from opencomputer.agent.config import default_config

    cfg = default_config()
    assert cfg.privacy.redact_pii is False


def test_apply_pii_redaction_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import maybe_redact_user_id

    assert maybe_redact_user_id("+1-555", redact=False) == "+1-555"


def test_apply_pii_redaction_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import maybe_redact_user_id

    h = maybe_redact_user_id("+1-555", redact=True)
    assert h != "+1-555"
    assert len(h) >= 12


def test_supported_adapter_set() -> None:
    from opencomputer.gateway.pii import SUPPORTED_ADAPTERS

    assert "whatsapp" in SUPPORTED_ADAPTERS
    assert "signal" in SUPPORTED_ADAPTERS
    assert "telegram" in SUPPORTED_ADAPTERS
    # Discord/Slack route IDs are already opaque per Hermes spec — not in scope.
    assert "discord" not in SUPPORTED_ADAPTERS
    assert "slack" not in SUPPORTED_ADAPTERS


def test_load_config_parses_privacy_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "privacy:\n  redact_pii: true\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.privacy.redact_pii is True


def test_salt_file_chmod_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.gateway.pii import _load_or_create_salt

    _load_or_create_salt()
    salt_file = tmp_path / ".pii_salt"
    mode = salt_file.stat().st_mode & 0o777
    assert mode == 0o600
