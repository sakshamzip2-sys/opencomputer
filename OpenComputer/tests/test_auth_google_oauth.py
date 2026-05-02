"""Tests for opencomputer/auth/google_oauth.py — Google PKCE OAuth flow.

Covers credential storage (JSON, 0600 perms), refresh-token usage when the
access_token is near expiry, and the public client_id/secret resolution
from env or the shipped defaults.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_credentials_dataclass_fields():
    from opencomputer.auth.google_oauth import GoogleCredentials

    creds = GoogleCredentials(
        access_token="at-1",
        refresh_token="rt-1",
        expires_ms=int(time.time() * 1000) + 3600_000,
        email="alice@example.com",
        project_id="proj-x",
    )
    assert creds.access_token == "at-1"
    assert creds.email == "alice@example.com"


def test_is_expiring_returns_true_for_past():
    from opencomputer.auth.google_oauth import GoogleCredentials

    creds = GoogleCredentials(
        access_token="x",
        refresh_token="y",
        expires_ms=int((time.time() - 60) * 1000),
    )
    assert creds.is_expiring() is True


def test_is_expiring_returns_false_for_future():
    from opencomputer.auth.google_oauth import GoogleCredentials

    creds = GoogleCredentials(
        access_token="x",
        refresh_token="y",
        expires_ms=int((time.time() + 3600) * 1000),
    )
    assert creds.is_expiring() is False


def test_save_and_load_credentials_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import (
        GoogleCredentials,
        load_credentials,
        save_credentials,
    )

    creds = GoogleCredentials(
        access_token="at-roundtrip",
        refresh_token="rt-roundtrip",
        expires_ms=int(time.time() * 1000) + 3600_000,
        email="bob@example.com",
        project_id="proj-y",
    )
    save_credentials(creds)
    loaded = load_credentials()
    assert loaded is not None
    assert loaded.access_token == "at-roundtrip"
    assert loaded.email == "bob@example.com"
    assert loaded.project_id == "proj-y"


def test_save_credentials_uses_secure_perms(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import GoogleCredentials, save_credentials

    creds = GoogleCredentials(
        access_token="x",
        refresh_token="y",
        expires_ms=int(time.time() * 1000) + 3600_000,
    )
    save_credentials(creds)
    creds_file = tmp_path / "auth" / "google_oauth.json"
    assert creds_file.exists()
    mode = creds_file.stat().st_mode & 0o777
    assert mode == 0o600


def test_load_credentials_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import load_credentials

    assert load_credentials() is None


def test_load_credentials_returns_none_when_corrupt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "google_oauth.json").write_text("garbage-{{{")
    from opencomputer.auth.google_oauth import load_credentials

    assert load_credentials() is None


def test_resolve_client_credentials_uses_env_first(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_GEMINI_CLIENT_ID", "env-cid")
    monkeypatch.setenv("OPENCOMPUTER_GEMINI_CLIENT_SECRET", "env-cs")
    from opencomputer.auth.google_oauth import resolve_client_credentials

    cid, cs = resolve_client_credentials()
    assert cid == "env-cid"
    assert cs == "env-cs"


def test_resolve_client_credentials_falls_back_to_defaults(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_GEMINI_CLIENT_ID", raising=False)
    monkeypatch.delenv("OPENCOMPUTER_GEMINI_CLIENT_SECRET", raising=False)
    from opencomputer.auth.google_oauth import resolve_client_credentials

    cid, cs = resolve_client_credentials()
    assert cid.endswith(".apps.googleusercontent.com")
    assert cs.startswith("GOCSPX-")


def test_refresh_access_token_returns_new_creds(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import (
        GoogleCredentials,
        refresh_access_token,
        save_credentials,
    )

    save_credentials(
        GoogleCredentials(
            access_token="stale",
            refresh_token="rt-still-good",
            expires_ms=int((time.time() - 60) * 1000),
            email="alice@example.com",
            project_id="proj-1",
        )
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "fresh-at",
        "expires_in": 3600,
    }

    with patch("httpx.post", return_value=mock_response):
        new_creds = refresh_access_token()

    assert new_creds.access_token == "fresh-at"
    assert new_creds.refresh_token == "rt-still-good"
    assert new_creds.email == "alice@example.com"  # preserved


def test_refresh_access_token_raises_when_no_refresh_token(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import refresh_access_token

    with pytest.raises(RuntimeError, match="not logged in"):
        refresh_access_token()


def test_refresh_access_token_raises_when_google_returns_error(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import (
        GoogleCredentials,
        refresh_access_token,
        save_credentials,
    )

    save_credentials(
        GoogleCredentials(
            access_token="x",
            refresh_token="rt-revoked",
            expires_ms=int((time.time() - 60) * 1000),
        )
    )

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.text = '{"error": "invalid_grant"}'

    with patch("httpx.post", return_value=mock_response):
        with pytest.raises(RuntimeError, match="refresh failed"):
            refresh_access_token()


def test_get_valid_access_token_returns_cached_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import (
        GoogleCredentials,
        get_valid_access_token,
        save_credentials,
    )

    save_credentials(
        GoogleCredentials(
            access_token="cached-fresh",
            refresh_token="rt",
            expires_ms=int((time.time() + 3600) * 1000),
        )
    )

    # Should NOT call httpx.post — cached token still valid
    with patch("httpx.post") as posted:
        token = get_valid_access_token()
        assert token == "cached-fresh"
        posted.assert_not_called()


def test_get_valid_access_token_refreshes_when_expiring(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import (
        GoogleCredentials,
        get_valid_access_token,
        save_credentials,
    )

    save_credentials(
        GoogleCredentials(
            access_token="stale",
            refresh_token="rt",
            expires_ms=int((time.time() - 60) * 1000),
        )
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "freshly-refreshed",
        "expires_in": 3600,
    }

    with patch("httpx.post", return_value=mock_response):
        token = get_valid_access_token()

    assert token == "freshly-refreshed"


def test_build_auth_url_includes_pkce_challenge():
    from opencomputer.auth.google_oauth import build_auth_url

    url = build_auth_url(
        client_id="cid-abc",
        redirect_uri="http://localhost:8085/oauth2callback",
        code_challenge="challenge-xyz",
        state="state-csrf",
    )
    assert "client_id=cid-abc" in url
    assert "code_challenge=challenge-xyz" in url
    assert "code_challenge_method=S256" in url
    assert "state=state-csrf" in url
    assert "scope=" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8085%2Foauth2callback" in url


def test_exchange_code_for_tokens_returns_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import exchange_code_for_tokens

    mock_token_response = MagicMock()
    mock_token_response.status_code = 200
    mock_token_response.json.return_value = {
        "access_token": "exchanged-at",
        "refresh_token": "exchanged-rt",
        "expires_in": 3600,
    }
    mock_userinfo_response = MagicMock()
    mock_userinfo_response.status_code = 200
    mock_userinfo_response.json.return_value = {
        "email": "user@example.com",
    }

    with patch("httpx.post", return_value=mock_token_response), \
         patch("httpx.get", return_value=mock_userinfo_response):
        creds = exchange_code_for_tokens(
            code="auth-code-from-callback",
            code_verifier="verifier-xyz",
            redirect_uri="http://localhost:8085/oauth2callback",
        )

    assert creds.access_token == "exchanged-at"
    assert creds.refresh_token == "exchanged-rt"
    assert creds.email == "user@example.com"


def test_logout_deletes_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import (
        GoogleCredentials,
        load_credentials,
        logout,
        save_credentials,
    )

    save_credentials(
        GoogleCredentials(
            access_token="x", refresh_token="y", expires_ms=int(time.time() * 1000)
        )
    )
    assert load_credentials() is not None
    logout()
    assert load_credentials() is None


def test_logout_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from opencomputer.auth.google_oauth import logout

    logout()  # Should not raise even when nothing exists
