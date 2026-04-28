"""Tests for opencomputer.security.redact runtime PII / secrets sweep.

Hermes Tier 3.D port. Mirrors the Hermes ``tests/agent/test_redact.py``
shape but adapted to OC's pattern catalog.
"""
from __future__ import annotations

import pytest

from opencomputer.security.redact import (
    is_enabled,
    redact_runtime_mapping,
    redact_runtime_text,
    redact_runtime_text_with_counts,
)

# ---------------------------------------------------------------------------
# Vendor-specific keys
# ---------------------------------------------------------------------------


def test_anthropic_key():
    assert "<ANTHROPIC_KEY_REDACTED>" in redact_runtime_text(
        "key=sk-ant-abc123def456ghi789"
    )


def test_aws_access_key():
    assert "<AWS_KEY_REDACTED>" in redact_runtime_text("AWS=AKIAIOSFODNN7EXAMPLE")


def test_aws_short_keyid_not_matched():
    """``AKIA`` followed by < 16 chars should NOT match (avoid false positives)."""
    out = redact_runtime_text("AKIA1234")
    assert "AKIA1234" in out


def test_google_ai_key():
    out = redact_runtime_text("AIzaSyD-Vc8WAj7Bc7ZlnP_Yp0kKbLxZ-1A2b3C4d")
    assert "<GOOGLE_AI_KEY_REDACTED>" in out


def test_github_pat():
    out = redact_runtime_text("token: ghp_abc123def456ghi789jkl012mno345pqr678st")
    assert "<GITHUB_PAT_REDACTED>" in out


def test_github_pat_v2():
    out = redact_runtime_text("token: github_pat_abc123def456ghi789jkl012mno345pqr678st")
    assert "<GITHUB_PAT_REDACTED>" in out


def test_slack_bot_token():
    # Construct via concat so GitHub secret-scanning doesn't flag this fake test token.
    fake = "xoxb" + "-12345678901-1234567890123-abcdefghijklmnopqrstuvwx"
    out = redact_runtime_text(fake)
    assert "<SLACK_TOKEN_REDACTED>" in out


def test_telegram_bot_token():
    fake = "12345678" + ":AAEhBP0av28ZAaXc4zCY6t-XeSMxoNXp4eQ"
    out = redact_runtime_text(fake)
    assert "<TELEGRAM_TOKEN_REDACTED>" in out


def test_groq_key():
    out = redact_runtime_text("gsk_abc123def456ghi789jkl012mno345pqr678stu901vwx234yz")
    assert "<GROQ_KEY_REDACTED>" in out


def test_perplexity_key():
    out = redact_runtime_text("pplx-abc123def456ghi789jkl012mno345pqr678stu901vwx234yz")
    assert "<PERPLEXITY_KEY_REDACTED>" in out


def test_huggingface_token():
    out = redact_runtime_text("hf_abc123def456ghi789jkl012mno345pqr678")
    assert "<HF_TOKEN_REDACTED>" in out


def test_openai_generic_key():
    out = redact_runtime_text("sk-abc123def456ghi789jkl012")
    assert "<OPENAI_KEY_REDACTED>" in out


def test_openai_short_key_not_matched():
    """Short ``sk-x`` strings shouldn't match — false positive guard."""
    out = redact_runtime_text("the sk-x flag")
    assert "sk-x" in out


def test_anthropic_precedes_openai():
    """``sk-ant-`` should match the Anthropic-specific label, not generic OpenAI."""
    out = redact_runtime_text("sk-ant-abc123def456ghi789jkl012")
    assert "<ANTHROPIC_KEY_REDACTED>" in out
    assert "<OPENAI_KEY_REDACTED>" not in out


# ---------------------------------------------------------------------------
# Bearer / JWT
# ---------------------------------------------------------------------------


def test_bearer_token():
    out = redact_runtime_text("Authorization: Bearer abc.def.ghi-jkl_mno")
    assert "Bearer <REDACTED>" in out


def test_jwt():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    out = redact_runtime_text(jwt)
    assert "<JWT_REDACTED>" in out


# ---------------------------------------------------------------------------
# DB / URL credentials
# ---------------------------------------------------------------------------


def test_postgres_connection_string():
    out = redact_runtime_text("postgres://user:secret@db.example.com:5432/mydb")
    assert "user:secret" not in out
    assert "<DB_CREDENTIALS_REDACTED>" in out


def test_mongodb_connection_string():
    out = redact_runtime_text("mongodb://admin:s3cr3t@cluster.mongodb.net/test")
    assert "admin:s3cr3t" not in out


def test_https_userinfo():
    out = redact_runtime_text("https://user:pass@api.example.com/x")
    assert "user:pass" not in out
    assert "<URL_USERINFO_REDACTED>" in out


def test_url_query_access_token():
    out = redact_runtime_text("https://api.example.com/oauth/cb?access_token=abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "<URL_PARAM_REDACTED>" in out


def test_url_query_api_key():
    out = redact_runtime_text("https://api.example.com/x?api_key=secretvalue")
    assert "secretvalue" not in out


def test_url_query_signature():
    out = redact_runtime_text("https://s3.amazonaws.com/x?X-Amz-Signature=abc123")
    assert "abc123" not in out


# ---------------------------------------------------------------------------
# Sensitive field assignments
# ---------------------------------------------------------------------------


def test_json_password_field():
    out = redact_runtime_text('{"username": "alice", "password": "hunter2"}')
    assert "hunter2" not in out
    assert "<JSON_FIELD_REDACTED>" in out


def test_json_apikey_field():
    out = redact_runtime_text('{"apiKey": "secretvalue", "user": "bob"}')
    assert "secretvalue" not in out


def test_env_assignment():
    out = redact_runtime_text("ANTHROPIC_API_KEY=sk-ant-shouldbe-redacted-too")
    # Either the env-assignment or the anthropic-key sweep covers this; both
    # are acceptable. What matters: the raw key is not in the output.
    assert "shouldbe-redacted-too" not in out


def test_password_env_assignment():
    out = redact_runtime_text("DB_PASSWORD=hunter2")
    assert "hunter2" not in out


# ---------------------------------------------------------------------------
# PEM private keys
# ---------------------------------------------------------------------------


def test_pem_private_key_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxabc123\n"
        "morebase64stuff\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = redact_runtime_text(pem)
    assert "<PEM_PRIVATE_KEY_REDACTED>" in out
    assert "MIIEowIBAAKCAQEAxabc123" not in out


# ---------------------------------------------------------------------------
# PII (file path / email / IP / phone / Discord mention)
# ---------------------------------------------------------------------------


def test_file_path():
    out = redact_runtime_text("/Users/saksham/Vscode/claude/foo.py")
    assert "/Users/REDACTED/" in out
    assert "saksham" not in out


def test_file_path_preserves_tail():
    out = redact_runtime_text("File at /Users/alice/work/proj/main.py exists")
    assert "/Users/REDACTED/work/proj/main.py" in out


def test_email():
    out = redact_runtime_text("Reach me at saksham@example.com")
    assert "<EMAIL_REDACTED>" in out
    assert "saksham@example.com" not in out


def test_ip_loopback_preserved():
    out = redact_runtime_text("Connect to 127.0.0.1:8080")
    assert "127.0.0.1" in out


def test_ip_external_redacted():
    out = redact_runtime_text("Connect to 203.0.113.5")
    assert "<IP_REDACTED>" in out
    assert "203.0.113.5" not in out


def test_phone_e164():
    out = redact_runtime_text("Call +1 555 0100")
    assert "<PHONE_REDACTED>" in out


def test_discord_mention():
    out = redact_runtime_text("Hey <@1234567890123456789> what's up?")
    assert "<DISCORD_MENTION_REDACTED>" in out
    assert "1234567890123456789" not in out


# ---------------------------------------------------------------------------
# Counts API
# ---------------------------------------------------------------------------


def test_counts_api():
    text = "Bearer abc.def + sk-ant-secret123abc456def + saksham@example.com"
    out, counts = redact_runtime_text_with_counts(text)
    assert counts.get("bearer", 0) == 1
    assert counts.get("anthropic", 0) == 1
    assert counts.get("email", 0) == 1


def test_counts_api_zero_when_clean():
    text = "Hello, world. This is a benign string."
    _, counts = redact_runtime_text_with_counts(text)
    assert all(v == 0 for v in counts.values())


# ---------------------------------------------------------------------------
# Mapping recursion
# ---------------------------------------------------------------------------


def test_mapping_walk():
    data = {
        "user": {"email": "alice@example.com"},
        "config": {"api_key": "sk-ant-secretvalue123abcdefghi"},
        "logs": ["Bearer xyz123", "no secret here"],
    }
    out = redact_runtime_mapping(data)
    assert out["user"]["email"] == "<EMAIL_REDACTED>"
    assert "<ANTHROPIC_KEY_REDACTED>" in out["config"]["api_key"]
    assert "Bearer <REDACTED>" in out["logs"][0]
    assert out["logs"][1] == "no secret here"


# ---------------------------------------------------------------------------
# Idempotence / no-double-redaction
# ---------------------------------------------------------------------------


def test_idempotent():
    text = "Bearer abc + saksham@example.com"
    once = redact_runtime_text(text)
    twice = redact_runtime_text(once)
    assert once == twice


def test_empty_string():
    assert redact_runtime_text("") == ""


def test_no_secrets_unchanged():
    text = "The quick brown fox jumps over the lazy dog."
    assert redact_runtime_text(text) == text


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_is_enabled_default_true():
    """Default: redaction is enabled when env var not set or set to truthy."""
    # In the test env, OC_REDACT_RUNTIME is unset → default enabled.
    assert is_enabled() is True


def test_kill_switch_runtime_mutation_does_nothing(monkeypatch):
    """Setting OC_REDACT_RUNTIME=false at runtime does NOT disable redaction.

    The env var is snapshotted at import. Runtime mutations are ignored.
    """
    monkeypatch.setenv("OC_REDACT_RUNTIME", "false")
    # Re-importing the module does NOT reset the snapshot for THIS test
    # (the import system caches the module). The snapshot is fixed.
    out = redact_runtime_text("sk-ant-shouldstillredact-12345-67890")
    assert "<ANTHROPIC_KEY_REDACTED>" in out


# ---------------------------------------------------------------------------
# Pattern interactions (order)
# ---------------------------------------------------------------------------


def test_url_with_token_in_query_and_userinfo():
    """Both userinfo and query-param patterns fire, both redacted."""
    out = redact_runtime_text("https://user:pass@api.example.com/x?api_key=abc123")
    assert "user:pass" not in out
    assert "abc123" not in out
