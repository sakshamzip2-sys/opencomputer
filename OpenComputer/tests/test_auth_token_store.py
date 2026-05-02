"""Tests for opencomputer/auth/token_store.py — OAuth token persistence."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def test_save_and_load_roundtrips_token(tmp_path):
    from opencomputer.auth.token_store import OAuthToken, load_token, save_token

    store = tmp_path / "tokens.json"
    token = OAuthToken(
        provider="nous-portal",
        access_token="at-xyz",
        refresh_token="rt-xyz",
        expires_at=int(time.time()) + 3600,
        scope="read write",
    )
    save_token(token, store_path=store)

    loaded = load_token("nous-portal", store_path=store)
    assert loaded is not None
    assert loaded.access_token == "at-xyz"
    assert loaded.refresh_token == "rt-xyz"
    assert loaded.scope == "read write"


def test_load_returns_none_for_missing_provider(tmp_path):
    from opencomputer.auth.token_store import load_token

    store = tmp_path / "tokens.json"
    assert load_token("nonexistent", store_path=store) is None


def test_load_returns_none_when_store_missing(tmp_path):
    from opencomputer.auth.token_store import load_token

    store = tmp_path / "missing.json"
    assert load_token("anything", store_path=store) is None


def test_save_creates_store_with_0600_perms(tmp_path):
    from opencomputer.auth.token_store import OAuthToken, save_token

    store = tmp_path / "tokens.json"
    save_token(
        OAuthToken(provider="p", access_token="t", expires_at=0),
        store_path=store,
    )

    assert (store.stat().st_mode & 0o777) == 0o600


def test_save_preserves_other_providers(tmp_path):
    from opencomputer.auth.token_store import OAuthToken, load_token, save_token

    store = tmp_path / "tokens.json"
    save_token(OAuthToken(provider="a", access_token="ta", expires_at=0), store_path=store)
    save_token(OAuthToken(provider="b", access_token="tb", expires_at=0), store_path=store)

    assert load_token("a", store_path=store).access_token == "ta"
    assert load_token("b", store_path=store).access_token == "tb"


def test_save_overwrites_existing_provider_token(tmp_path):
    from opencomputer.auth.token_store import OAuthToken, load_token, save_token

    store = tmp_path / "tokens.json"
    save_token(OAuthToken(provider="x", access_token="old", expires_at=0), store_path=store)
    save_token(OAuthToken(provider="x", access_token="new", expires_at=0), store_path=store)

    assert load_token("x", store_path=store).access_token == "new"


def test_delete_removes_provider_token(tmp_path):
    from opencomputer.auth.token_store import OAuthToken, delete_token, load_token, save_token

    store = tmp_path / "tokens.json"
    save_token(OAuthToken(provider="a", access_token="t", expires_at=0), store_path=store)
    save_token(OAuthToken(provider="b", access_token="t", expires_at=0), store_path=store)

    delete_token("a", store_path=store)

    assert load_token("a", store_path=store) is None
    assert load_token("b", store_path=store) is not None


def test_token_is_expired_logic():
    from opencomputer.auth.token_store import OAuthToken

    past = OAuthToken(provider="p", access_token="t", expires_at=int(time.time()) - 60)
    future = OAuthToken(provider="p", access_token="t", expires_at=int(time.time()) + 3600)
    no_expiry = OAuthToken(provider="p", access_token="t", expires_at=0)

    assert past.is_expired() is True
    assert future.is_expired() is False
    # expires_at=0 means "no known expiry" — treated as not expired
    assert no_expiry.is_expired() is False


def test_token_expires_soon_with_skew(tmp_path):
    """A token expiring in 30s should report expires_soon=True with 60s skew."""
    from opencomputer.auth.token_store import OAuthToken

    soon = OAuthToken(provider="p", access_token="t", expires_at=int(time.time()) + 30)
    later = OAuthToken(provider="p", access_token="t", expires_at=int(time.time()) + 3600)

    assert soon.expires_soon(skew_seconds=60) is True
    assert later.expires_soon(skew_seconds=60) is False


def test_default_store_path_uses_oc_home(monkeypatch, tmp_path):
    from opencomputer.auth.token_store import default_store_path

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert default_store_path() == tmp_path / "auth_tokens.json"


def test_default_store_path_falls_back_to_home(monkeypatch):
    from opencomputer.auth.token_store import default_store_path

    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    result = default_store_path()
    assert result == Path.home() / ".opencomputer" / "auth_tokens.json"


def test_corrupt_store_returns_none_does_not_raise(tmp_path):
    """Garbled JSON in the store shouldn't crash callers."""
    from opencomputer.auth.token_store import load_token

    store = tmp_path / "tokens.json"
    store.write_text("not valid json{{{")
    store.chmod(0o600)

    assert load_token("anything", store_path=store) is None
