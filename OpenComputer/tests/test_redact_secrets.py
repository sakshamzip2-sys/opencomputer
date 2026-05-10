"""Tests for security.redact_secrets — strip API key patterns from text."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_disabled_by_default() -> None:
    from opencomputer.agent.config import default_config

    cfg = default_config()
    assert cfg.security.redact_secrets is False


def test_redact_openai_style_key() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text

    out = redact_secrets_in_text("api_key: sk-abc123def456ghijklmnop")
    assert "sk-abc123def456" not in out
    assert "[REDACTED]" in out


def test_does_not_redact_short_sk_string() -> None:
    """Avoid false positives on short strings like 'sk-1' or 'sk-short'."""
    from opencomputer.agent.redactors import redact_secrets_in_text

    out = redact_secrets_in_text("not a key: sk-1")
    assert "sk-1" in out
    assert "[REDACTED]" not in out


def test_redact_github_pat_classic() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text

    pat = "ghp_" + "a" * 36
    out = redact_secrets_in_text(f"token: {pat}")
    assert pat not in out
    assert "[REDACTED]" in out


def test_redact_github_pat_fine_grained() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text

    pat = "github_pat_" + "a" * 22 + "_" + "b" * 59
    out = redact_secrets_in_text(f"token: {pat}")
    assert "[REDACTED]" in out


def test_redact_aws_access_key() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text

    out = redact_secrets_in_text("aws: AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_redact_slack_bot_token() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text

    tok = "xoxb-" + "a" * 30
    out = redact_secrets_in_text(f"token: {tok}")
    assert "[REDACTED]" in out


def test_redact_bearer_in_authorization_header() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text

    out = redact_secrets_in_text("Authorization: Bearer " + "x" * 40)
    assert "[REDACTED]" in out
    assert "x" * 40 not in out


def test_multiple_secrets_in_one_string() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text

    text = (
        "openai: sk-abcdefghij1234567890\n"
        "github: ghp_" + "z" * 36 + "\n"
    )
    out = redact_secrets_in_text(text)
    assert "[REDACTED]" in out
    assert out.count("[REDACTED]") == 2


def test_empty_string_passthrough() -> None:
    from opencomputer.agent.redactors import redact_secrets_in_text

    assert redact_secrets_in_text("") == ""


def test_load_config_parses_security_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.agent.config_store import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "security:\n  redact_secrets: true\n", encoding="utf-8"
    )
    cfg = load_config(cfg_path)
    assert cfg.security.redact_secrets is True


def test_apply_secret_redaction_helper_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``maybe_redact_secrets`` is a passthrough when redact=False."""
    from opencomputer.agent.redactors import maybe_redact_secrets

    text = "key: sk-abc123def456ghijklmnop"
    assert maybe_redact_secrets(text, redact=False) == text


def test_apply_secret_redaction_helper_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.agent.redactors import maybe_redact_secrets

    text = "key: sk-abc123def456ghijklmnop"
    out = maybe_redact_secrets(text, redact=True)
    assert "[REDACTED]" in out
    assert "sk-abc123def456" not in out
