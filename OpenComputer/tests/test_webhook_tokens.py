"""Tests for the webhook token registry: create / list / revoke / remove + HMAC verify."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Load the tokens module by absolute path (it's a plugin, not a package import).
_TOKENS_PATH = (
    Path(__file__).resolve().parent.parent / "extensions" / "webhook" / "tokens.py"
)


def _load() -> object:
    spec = importlib.util.spec_from_file_location(
        "webhook_tokens_under_test", _TOKENS_PATH
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def tokens_mod():
    return _load()


class TestCreateToken:
    def test_creates_with_id_and_secret(self, tokens_mod) -> None:
        tid, secret = tokens_mod.create_token(name="t1")
        assert len(tid) == 32  # 16 bytes hex
        assert len(secret) == 64  # 32 bytes hex
        assert tokens_mod.get_token(tid)["name"] == "t1"

    def test_unique_ids(self, tokens_mod) -> None:
        a, _ = tokens_mod.create_token(name="a")
        b, _ = tokens_mod.create_token(name="b")
        assert a != b

    def test_persists_across_loads(self, tokens_mod) -> None:
        tid, secret = tokens_mod.create_token(name="persist", scopes=["x", "y"], notify="telegram")
        # Re-load and verify it sticks
        loaded = tokens_mod.get_token(tid)
        assert loaded["secret"] == secret
        assert loaded["scopes"] == ["x", "y"]
        assert loaded["notify"] == "telegram"
        assert loaded["revoked"] is False


class TestListTokens:
    def test_excludes_revoked(self, tokens_mod) -> None:
        a_id, _ = tokens_mod.create_token(name="a")
        b_id, _ = tokens_mod.create_token(name="b")
        tokens_mod.revoke_token(b_id)
        listed = tokens_mod.list_tokens()
        assert len(listed) == 1
        assert listed[0]["token_id"] == a_id
        assert len(tokens_mod.list_tokens(include_revoked=True)) == 2

    def test_strips_secret_from_listing(self, tokens_mod) -> None:
        tid, _ = tokens_mod.create_token(name="redact-me")
        listed = tokens_mod.list_tokens()
        assert "secret" not in listed[0]
        assert listed[0]["token_id"] == tid


class TestRevokeRemove:
    def test_revoke_marks_flag(self, tokens_mod) -> None:
        tid, _ = tokens_mod.create_token(name="r")
        assert tokens_mod.revoke_token(tid) is True
        assert tokens_mod.get_token(tid)["revoked"] is True

    def test_revoke_missing_returns_false(self, tokens_mod) -> None:
        assert tokens_mod.revoke_token("nonexistent") is False

    def test_remove_deletes_entry(self, tokens_mod) -> None:
        tid, _ = tokens_mod.create_token(name="rm")
        assert tokens_mod.remove_token(tid) is True
        assert tokens_mod.get_token(tid) is None

    def test_remove_missing_returns_false(self, tokens_mod) -> None:
        assert tokens_mod.remove_token("nonexistent") is False


class TestMarkUsed:
    def test_updates_last_used_at(self, tokens_mod) -> None:
        tid, _ = tokens_mod.create_token(name="u")
        assert tokens_mod.get_token(tid)["last_used_at"] is None
        tokens_mod.mark_used(tid)
        assert tokens_mod.get_token(tid)["last_used_at"] is not None


class TestHMACVerify:
    def test_valid_signature_passes(self, tokens_mod) -> None:
        import hashlib
        import hmac

        secret = "deadbeef" * 8  # 64 chars
        body = b'{"text":"hello"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        header = f"sha256={sig}"
        assert tokens_mod.verify_signature(body=body, signature_header=header, secret=secret)

    def test_wrong_signature_fails(self, tokens_mod) -> None:
        secret = "deadbeef" * 8
        body = b'{"text":"hello"}'
        # Use a different body's signature
        import hashlib
        import hmac

        wrong_sig = hmac.new(secret.encode(), b"different", hashlib.sha256).hexdigest()
        header = f"sha256={wrong_sig}"
        assert not tokens_mod.verify_signature(body=body, signature_header=header, secret=secret)

    def test_missing_prefix_fails(self, tokens_mod) -> None:
        assert not tokens_mod.verify_signature(body=b"x", signature_header="abcdef", secret="s")

    def test_empty_header_fails(self, tokens_mod) -> None:
        assert not tokens_mod.verify_signature(body=b"x", signature_header="", secret="s")


class TestStorageHygiene:
    def test_secure_permissions(self, tokens_mod, tmp_path: Path) -> None:
        tokens_mod.create_token(name="x")
        f = tokens_mod.tokens_file()
        assert f.exists()
        if os.name != "nt":
            assert oct(f.stat().st_mode)[-3:] == "600"

    def test_profile_isolated(self, tokens_mod, tmp_path: Path) -> None:
        tokens_mod.create_token(name="a")
        assert (tmp_path / "webhook_tokens.json").exists()
