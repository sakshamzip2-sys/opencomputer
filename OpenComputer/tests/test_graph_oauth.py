"""Tests for opencomputer/auth/graph_oauth.py — Microsoft Graph OAuth.

Covers, with the HTTP layer mocked:

* the device-code request carries the right scopes (incl. ``offline_access``);
* token polling handles ``authorization_pending`` / ``slow_down`` / a clean
  token response;
* :func:`refresh_access_token` performs the ``grant_type=refresh_token`` grant
  and persists the rotated token;
* :func:`get_valid_access_token` refreshes proactively near expiry and does
  NOT refresh a still-fresh token;
* a missing ``OPENCOMPUTER_GRAPH_CLIENT_ID`` gives a clean error;

plus the ``oc auth login graph`` / ``oc auth logout graph`` CLI surface.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def graph_env(monkeypatch, tmp_path):
    """Isolate the token store under tmp_path and set a client id."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_GRAPH_CLIENT_ID", "test-graph-client-id")
    monkeypatch.delenv("OPENCOMPUTER_GRAPH_TENANT", raising=False)
    return tmp_path


def _ok_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _error_response(status_code: int, error: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"error": error}
    resp.text = f'{{"error": "{error}"}}'
    return resp


# =============================================================================
# Client-id / tenant resolution
# =============================================================================


def test_resolve_client_id_uses_env(graph_env):
    from opencomputer.auth.graph_oauth import resolve_client_id

    assert resolve_client_id() == "test-graph-client-id"


def test_resolve_client_id_missing_gives_clean_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCOMPUTER_GRAPH_CLIENT_ID", raising=False)
    from opencomputer.auth.graph_oauth import GraphOAuthError, resolve_client_id

    with pytest.raises(GraphOAuthError) as excinfo:
        resolve_client_id()
    msg = str(excinfo.value)
    assert "OPENCOMPUTER_GRAPH_CLIENT_ID" in msg
    assert "public-client" in msg


def test_resolve_client_id_blank_env_treated_as_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_GRAPH_CLIENT_ID", "   ")
    from opencomputer.auth.graph_oauth import GraphOAuthError, resolve_client_id

    with pytest.raises(GraphOAuthError):
        resolve_client_id()


def test_resolve_tenant_defaults_to_common(graph_env):
    from opencomputer.auth.graph_oauth import resolve_tenant

    assert resolve_tenant() == "common"


def test_resolve_tenant_env_override(graph_env, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_GRAPH_TENANT", "contoso.onmicrosoft.com")
    from opencomputer.auth.graph_oauth import resolve_tenant

    assert resolve_tenant() == "contoso.onmicrosoft.com"


def test_scope_string_includes_offline_access():
    from opencomputer.auth.graph_oauth import GRAPH_SCOPES, SCOPE_STRING

    # offline_access is mandatory — without it Graph returns no refresh token.
    assert "offline_access" in GRAPH_SCOPES
    assert "offline_access" in SCOPE_STRING.split()
    for required in ("Mail.Send", "Calendars.Read", "Files.Read"):
        assert required in SCOPE_STRING.split()


# =============================================================================
# Device-code login — begin
# =============================================================================


def test_begin_device_login_requests_correct_scopes(graph_env):
    from opencomputer.auth.graph_oauth import begin_device_login

    device_resp = _ok_response(
        {
            "device_code": "dc-123",
            "user_code": "WXYZ-1234",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
        }
    )

    with patch("httpx.post", return_value=device_resp) as posted:
        prompt = begin_device_login()

    # The device-code POST must carry the scopes incl. offline_access.
    _, kwargs = posted.call_args
    sent_scopes = kwargs["data"]["scope"].split()
    assert "offline_access" in sent_scopes
    assert "Mail.Send" in sent_scopes
    assert "Calendars.Read" in sent_scopes
    assert "Files.Read" in sent_scopes
    assert kwargs["data"]["client_id"] == "test-graph-client-id"

    # And it must hit the Microsoft devicecode endpoint with the common tenant.
    sent_url = posted.call_args[0][0]
    assert sent_url == (
        "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode"
    )

    assert prompt.user_code == "WXYZ-1234"
    assert prompt.verification_uri == "https://microsoft.com/devicelogin"
    assert prompt._device_code == "dc-123"


def test_begin_device_login_missing_client_id_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCOMPUTER_GRAPH_CLIENT_ID", raising=False)
    from opencomputer.auth.graph_oauth import GraphOAuthError, begin_device_login

    with pytest.raises(GraphOAuthError, match="OPENCOMPUTER_GRAPH_CLIENT_ID"):
        begin_device_login()


def test_begin_device_login_wraps_device_code_error(graph_env):
    from opencomputer.auth.graph_oauth import GraphOAuthError, begin_device_login

    err_resp = _error_response(400, "invalid_client")
    err_resp.text = '{"error": "invalid_client"}'

    with patch("httpx.post", return_value=err_resp):
        with pytest.raises(GraphOAuthError, match="device-code request failed"):
            begin_device_login()


# =============================================================================
# Device-code login — complete (polling)
# =============================================================================


def _make_prompt():
    from opencomputer.auth.graph_oauth import GraphLoginPrompt

    return GraphLoginPrompt(
        verification_uri="https://microsoft.com/devicelogin",
        user_code="WXYZ-1234",
        message="...",
        expires_in=900,
        interval=1,
        _device_code="dc-123",
        _client_id="test-graph-client-id",
        _tenant="common",
    )


def test_complete_device_login_handles_pending_then_success(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, complete_device_login

    pending = _error_response(400, "authorization_pending")
    success = _ok_response(
        {
            "token_type": "Bearer",
            "scope": "Mail.Send Calendars.Read Files.Read",
            "expires_in": 3599,
            "access_token": "at-fresh",
            "refresh_token": "rt-fresh",
        }
    )
    # poll_for_token sleeps before each poll — first poll pending, second ok.
    with patch("httpx.post", side_effect=[pending, success]), patch(
        "time.sleep"
    ):
        token = complete_device_login(_make_prompt())

    assert token.access_token == "at-fresh"
    assert token.refresh_token == "rt-fresh"
    assert token.provider == PROVIDER

    # Persisted under the "graph" provider key.
    stored = token_store.load_token(PROVIDER)
    assert stored is not None
    assert stored.access_token == "at-fresh"


def test_complete_device_login_handles_slow_down(graph_env):
    from opencomputer.auth.graph_oauth import complete_device_login

    slow_down = _error_response(400, "slow_down")
    success = _ok_response(
        {
            "expires_in": 3599,
            "access_token": "at-after-slowdown",
            "refresh_token": "rt-1",
        }
    )
    sleeps: list[float] = []
    with patch("httpx.post", side_effect=[slow_down, success]), patch(
        "time.sleep", side_effect=lambda s: sleeps.append(s)
    ):
        token = complete_device_login(_make_prompt())

    assert token.access_token == "at-after-slowdown"
    # slow_down bumps the interval by +5s for the next poll.
    assert sleeps[0] == 1
    assert sleeps[1] == 6


def test_complete_device_login_declined_raises(graph_env):
    from opencomputer.auth.graph_oauth import GraphOAuthError, complete_device_login

    # device_code.py treats access_denied as a terminal error.
    declined = _error_response(400, "access_denied")
    with patch("httpx.post", return_value=declined), patch("time.sleep"):
        with pytest.raises(GraphOAuthError, match="did not complete"):
            complete_device_login(_make_prompt())


def test_complete_device_login_clean_token_response_no_pending(graph_env):
    from opencomputer.auth.graph_oauth import complete_device_login

    success = _ok_response(
        {
            "expires_in": 3599,
            "access_token": "at-immediate",
            "refresh_token": "rt-immediate",
        }
    )
    with patch("httpx.post", return_value=success), patch("time.sleep"):
        token = complete_device_login(_make_prompt())

    assert token.access_token == "at-immediate"
    # expires_at should be ~now + 3599s.
    assert token.expires_at >= int(time.time()) + 3000


# =============================================================================
# refresh_access_token
# =============================================================================


def test_refresh_access_token_does_refresh_token_grant(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, refresh_access_token

    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-stale",
            refresh_token="rt-old",
            expires_at=int(time.time()) - 10,
            scope="Mail.Send Calendars.Read Files.Read",
        )
    )

    refreshed_resp = _ok_response(
        {
            "expires_in": 3599,
            "access_token": "at-new",
            "refresh_token": "rt-rotated",
            "scope": "Mail.Send Calendars.Read Files.Read",
        }
    )
    with patch("httpx.post", return_value=refreshed_resp) as posted:
        new_token = refresh_access_token()

    # The grant must be grant_type=refresh_token against the token endpoint.
    sent_url = posted.call_args[0][0]
    assert sent_url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    sent_data = posted.call_args[1]["data"]
    assert sent_data["grant_type"] == "refresh_token"
    assert sent_data["refresh_token"] == "rt-old"
    assert sent_data["client_id"] == "test-graph-client-id"

    assert new_token.access_token == "at-new"
    # Microsoft rotates the refresh token — the new one is persisted.
    assert new_token.refresh_token == "rt-rotated"
    persisted = token_store.load_token(PROVIDER)
    assert persisted is not None
    assert persisted.access_token == "at-new"
    assert persisted.refresh_token == "rt-rotated"


def test_refresh_access_token_keeps_old_refresh_when_none_returned(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, refresh_access_token

    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-stale",
            refresh_token="rt-keepme",
            expires_at=int(time.time()) - 10,
        )
    )
    resp = _ok_response({"expires_in": 3599, "access_token": "at-new"})
    with patch("httpx.post", return_value=resp):
        new_token = refresh_access_token()

    assert new_token.refresh_token == "rt-keepme"


def test_refresh_access_token_no_stored_token_raises(graph_env):
    from opencomputer.auth.graph_oauth import GraphOAuthError, refresh_access_token

    with pytest.raises(GraphOAuthError, match="not logged in"):
        refresh_access_token()


def test_refresh_access_token_no_refresh_token_raises(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import (
        PROVIDER,
        GraphOAuthError,
        refresh_access_token,
    )

    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-only",
            refresh_token=None,
            expires_at=int(time.time()) - 10,
        )
    )
    with pytest.raises(GraphOAuthError, match="offline_access"):
        refresh_access_token()


def test_refresh_access_token_rejected_raises_clean_error(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import (
        PROVIDER,
        GraphOAuthError,
        refresh_access_token,
    )

    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-stale",
            refresh_token="rt-revoked",
            expires_at=int(time.time()) - 10,
        )
    )
    rejected = _error_response(400, "invalid_grant")
    with patch("httpx.post", return_value=rejected):
        with pytest.raises(GraphOAuthError) as excinfo:
            refresh_access_token()
    msg = str(excinfo.value)
    assert "invalid_grant" in msg
    # The refresh token must NOT appear in the error message.
    assert "rt-revoked" not in msg


def test_refresh_access_token_preserves_scope_when_omitted(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, refresh_access_token

    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-stale",
            refresh_token="rt-1",
            expires_at=int(time.time()) - 10,
            scope="Mail.Send Calendars.Read Files.Read",
        )
    )
    # Response omits `scope`.
    resp = _ok_response(
        {"expires_in": 3599, "access_token": "at-new", "refresh_token": "rt-2"}
    )
    with patch("httpx.post", return_value=resp):
        new_token = refresh_access_token()

    assert new_token.scope == "Mail.Send Calendars.Read Files.Read"


# =============================================================================
# get_valid_access_token
# =============================================================================


def test_get_valid_access_token_returns_fresh_without_refresh(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, get_valid_access_token

    # Token valid for another hour — far from the 5-minute skew window.
    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-still-fresh",
            refresh_token="rt-1",
            expires_at=int(time.time()) + 3600,
        )
    )
    with patch("httpx.post") as posted:
        token = get_valid_access_token()
        assert token == "at-still-fresh"
        # A still-fresh token must NOT trigger a network refresh.
        posted.assert_not_called()


def test_get_valid_access_token_refreshes_when_near_expiry(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, get_valid_access_token

    # Expires in 60s — inside the 5-minute proactive-refresh skew.
    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-about-to-expire",
            refresh_token="rt-1",
            expires_at=int(time.time()) + 60,
        )
    )
    resp = _ok_response(
        {"expires_in": 3599, "access_token": "at-proactively-refreshed",
         "refresh_token": "rt-2"}
    )
    with patch("httpx.post", return_value=resp) as posted:
        token = get_valid_access_token()

    assert token == "at-proactively-refreshed"
    posted.assert_called_once()


def test_get_valid_access_token_refreshes_when_expired(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, get_valid_access_token

    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-expired",
            refresh_token="rt-1",
            expires_at=int(time.time()) - 100,
        )
    )
    resp = _ok_response(
        {"expires_in": 3599, "access_token": "at-renewed", "refresh_token": "rt-2"}
    )
    with patch("httpx.post", return_value=resp):
        token = get_valid_access_token()

    assert token == "at-renewed"


def test_get_valid_access_token_force_refresh(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, get_valid_access_token

    # Token is fresh, but force_refresh must still refresh it.
    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-fresh-but-rejected",
            refresh_token="rt-1",
            expires_at=int(time.time()) + 3600,
        )
    )
    resp = _ok_response(
        {"expires_in": 3599, "access_token": "at-forced", "refresh_token": "rt-2"}
    )
    with patch("httpx.post", return_value=resp) as posted:
        token = get_valid_access_token(force_refresh=True)

    assert token == "at-forced"
    posted.assert_called_once()


def test_get_valid_access_token_not_logged_in_raises(graph_env):
    from opencomputer.auth.graph_oauth import GraphOAuthError, get_valid_access_token

    with pytest.raises(GraphOAuthError, match="not logged in"):
        get_valid_access_token()


# =============================================================================
# has_stored_token / logout / stored_account_summary
# =============================================================================


def test_has_stored_token_false_then_true(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, has_stored_token

    assert has_stored_token() is False
    token_store.save_token(
        token_store.OAuthToken(provider=PROVIDER, access_token="at-1")
    )
    assert has_stored_token() is True


def test_logout_removes_token_and_is_idempotent(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import (
        PROVIDER,
        has_stored_token,
        logout,
    )

    token_store.save_token(
        token_store.OAuthToken(provider=PROVIDER, access_token="at-1")
    )
    assert logout() is True
    assert has_stored_token() is False
    # Second logout is a no-op and returns False.
    assert logout() is False


def test_stored_account_summary_none_when_logged_out(graph_env):
    from opencomputer.auth.graph_oauth import stored_account_summary

    assert stored_account_summary() is None


def test_stored_account_summary_reports_scopes_without_tokens(graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, stored_account_summary

    token_store.save_token(
        token_store.OAuthToken(
            provider=PROVIDER,
            access_token="at-secret",
            refresh_token="rt-secret",
            expires_at=int(time.time()) + 1800,
            scope="Mail.Send Calendars.Read Files.Read",
        )
    )
    summary = stored_account_summary()
    assert summary is not None
    assert "Mail.Send" in summary
    # Tokens must never leak into the human-facing summary.
    assert "at-secret" not in summary
    assert "rt-secret" not in summary


# =============================================================================
# CLI — oc auth login graph / oc auth logout graph
# =============================================================================


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def auth_app(graph_env):
    """Reload cli_auth so it picks up the tmp_path OPENCOMPUTER_HOME."""
    import importlib

    import opencomputer.cli_auth as mod

    importlib.reload(mod)
    return mod.auth_app


def test_cli_login_graph_succeeds(runner, auth_app, graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER

    device_resp = _ok_response(
        {
            "device_code": "dc-cli",
            "user_code": "CLIX-5678",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 1,
        }
    )
    token_resp = _ok_response(
        {
            "expires_in": 3599,
            "access_token": "at-cli",
            "refresh_token": "rt-cli",
            "scope": "Mail.Send Calendars.Read Files.Read",
        }
    )
    with patch("httpx.post", side_effect=[device_resp, token_resp]), patch(
        "time.sleep"
    ):
        result = runner.invoke(auth_app, ["login", "graph"])

    assert result.exit_code == 0, result.output
    # The verification URL + one-time code are shown to the user.
    assert "microsoft.com/devicelogin" in result.output
    assert "CLIX-5678" in result.output
    assert "Signed in" in result.output

    # Token persisted under the "graph" key.
    stored = token_store.load_token(PROVIDER)
    assert stored is not None
    assert stored.access_token == "at-cli"


def test_cli_login_graph_missing_client_id_errors(runner, monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.delenv("OPENCOMPUTER_GRAPH_CLIENT_ID", raising=False)
    import importlib

    import opencomputer.cli_auth as mod

    importlib.reload(mod)

    result = runner.invoke(mod.auth_app, ["login", "graph"])
    assert result.exit_code == 1
    assert "OPENCOMPUTER_GRAPH_CLIENT_ID" in result.output


def test_cli_login_unknown_provider_errors(runner, auth_app):
    result = runner.invoke(auth_app, ["login", "bogus"])
    assert result.exit_code == 2
    assert "Unknown OAuth provider" in result.output


def test_cli_logout_graph_removes_token(runner, auth_app, graph_env):
    from opencomputer.auth import token_store
    from opencomputer.auth.graph_oauth import PROVIDER, has_stored_token

    token_store.save_token(
        token_store.OAuthToken(provider=PROVIDER, access_token="at-cli")
    )
    result = runner.invoke(auth_app, ["logout", "graph"])
    assert result.exit_code == 0
    assert "Signed out" in result.output
    assert has_stored_token() is False


def test_cli_logout_graph_when_not_logged_in(runner, auth_app, graph_env):
    result = runner.invoke(auth_app, ["logout", "graph"])
    assert result.exit_code == 0
    assert "nothing to do" in result.output.lower()


def test_cli_logout_unknown_provider_errors(runner, auth_app):
    result = runner.invoke(auth_app, ["logout", "bogus"])
    assert result.exit_code == 2
    assert "Unknown OAuth provider" in result.output
