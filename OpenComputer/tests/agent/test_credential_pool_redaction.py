"""A2 — RR-4 credential pool log leak fix.

Replaces ``key[:8]`` log fragments with ``cred_pool[N]:<sha256_12>``
so we don't leak ``sk-ant-X`` (Anthropic key prefix + 1 byte secret
entropy) at WARNING level.
"""

from __future__ import annotations

import hashlib
import logging

import pytest

from opencomputer.agent.credential_pool import (
    CredentialPool,
    CredentialPoolExhausted,
    _safe_id,
)


def test_safe_id_returns_pool_index_and_sha256_prefix() -> None:
    key = "sk-ant-api03-abc123XYZ-very-secret-content"
    out = _safe_id(key, pool_index=3)
    expected_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    assert out == f"cred_pool[3]:{expected_hash}"


def test_safe_id_never_leaks_key_prefix() -> None:
    key = "sk-ant-api03-VERYSECRET"
    out = _safe_id(key, pool_index=0)
    assert "sk-ant-" not in out
    assert "sk-" not in out
    assert "VERYSECRET" not in out


def test_safe_id_stable_across_calls() -> None:
    key = "sk-or-v1-abc"
    a = _safe_id(key, pool_index=0)
    b = _safe_id(key, pool_index=0)
    assert a == b


def test_safe_id_different_keys_different_hash() -> None:
    a = _safe_id("key-one", pool_index=0)
    b = _safe_id("key-two", pool_index=0)
    assert a != b


def test_safe_id_empty_key_returns_marker() -> None:
    out = _safe_id("", pool_index=2)
    assert out == "cred_pool[2]:empty"


@pytest.mark.asyncio
async def test_quarantine_log_line_has_no_key_prefix(caplog) -> None:
    pool = CredentialPool(keys=["sk-ant-api03-aaa", "sk-ant-api03-bbb"])
    caplog.set_level(logging.WARNING)
    await pool.report_auth_failure("sk-ant-api03-aaa", reason="401")
    log_text = "\n".join(rec.message for rec in caplog.records)
    assert "sk-ant-" not in log_text
    assert "sk-ant-api03-aaa"[:8] not in log_text
    assert "cred_pool[" in log_text


@pytest.mark.asyncio
async def test_exhausted_error_message_has_no_key_prefix() -> None:
    pool = CredentialPool(keys=["sk-ant-api03-aaa"])
    await pool.report_auth_failure("sk-ant-api03-aaa", reason="401")
    with pytest.raises(CredentialPoolExhausted) as exc:
        await pool.acquire()
    msg = str(exc.value)
    assert "sk-ant-" not in msg
    assert "cred_pool[" in msg


def test_stats_key_preview_uses_safe_id() -> None:
    pool = CredentialPool(keys=["sk-ant-api03-aaa", "sk-ant-api03-bbb"])
    stats = pool.stats()
    for entry in stats["keys"]:
        assert "sk-" not in entry["key_preview"]
        assert entry["key_preview"].startswith("cred_pool[")
