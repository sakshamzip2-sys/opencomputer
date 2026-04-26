"""Tests for opencomputer.mcp.oauth_pkce — Round 2 P-6 PKCE flow.

These tests exercise the full PKCE dance without ever launching a real
browser. ``webbrowser.open`` is monkeypatched; the OAuth callback is
triggered by POSTing (well, GETting) to the ephemeral redirect URL from
a side thread.

Hard requirements verified:
    - Verifier is high-entropy (``secrets.token_urlsafe(64)``, ≥86 chars).
    - Challenge derives from verifier via S256 (sha256 then b64url).
    - State is high-entropy (``secrets.token_urlsafe(32)``, ≥43 chars).
    - State validation uses constant-time compare (mismatch → raise).
    - Callback server binds to ``127.0.0.1`` ONLY.
    - 5-minute default timeout (overridable for fast tests).
    - Browser-open failure surfaces the URL (no crash).
    - Malformed callback (missing code) raises ``OAuthCallbackError``.
"""

from __future__ import annotations

import base64
import hashlib
import threading
import time
import urllib.parse
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from opencomputer.mcp.oauth_pkce import (
    OAuthCallbackError,
    OAuthFlowTimeout,
    OAuthStateMismatch,
    _build_callback_server,
    _make_challenge,
    _make_state,
    _make_verifier,
    run_pkce_flow,
)

# ─── PKCE primitives ─────────────────────────────────────────────


class TestPkcePrimitives:
    def test_verifier_meets_minimum_length(self) -> None:
        # secrets.token_urlsafe(64) yields ~86 URL-safe base64 chars.
        v = _make_verifier()
        assert len(v) >= 86, f"verifier too short ({len(v)} chars)"
        # All chars are URL-safe base64 alphabet (no padding).
        allowed = set(
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        )
        assert set(v) <= allowed

    def test_verifier_unique_each_call(self) -> None:
        # Vanishingly small chance of collision with 256 bits of entropy.
        assert _make_verifier() != _make_verifier()

    def test_challenge_matches_s256_spec(self) -> None:
        v = _make_verifier()
        c = _make_challenge(v)
        # Independent recomputation per RFC 7636 §4.2.
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(v.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        assert c == expected

    def test_challenge_known_vector(self) -> None:
        # RFC 7636 Appendix B test vector.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        expected_challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
        assert _make_challenge(verifier) == expected_challenge

    def test_state_meets_minimum_length(self) -> None:
        # secrets.token_urlsafe(32) yields ~43 URL-safe base64 chars.
        s = _make_state()
        assert len(s) >= 43

    def test_state_unique_each_call(self) -> None:
        assert _make_state() != _make_state()


# ─── Callback server bind verification ───────────────────────────


class TestCallbackServerBind:
    def test_binds_to_127_0_0_1_only(self) -> None:
        import queue as _queue

        q: _queue.Queue[Any] = _queue.Queue(maxsize=1)
        server = _build_callback_server(q)
        try:
            host, port = server.server_address[0], server.server_address[1]
            # Hard requirement: never 0.0.0.0, never localhost (IPv6 ambiguity).
            assert host == "127.0.0.1", f"server bound to {host!r}, must be 127.0.0.1"
            assert port > 0
        finally:
            server.server_close()

    def test_uses_ephemeral_port(self) -> None:
        import queue as _queue

        q: _queue.Queue[Any] = _queue.Queue(maxsize=1)
        s1 = _build_callback_server(q)
        s2 = _build_callback_server(q)
        try:
            # Two ephemeral binds should land on different ports almost always.
            assert s1.server_address[1] != s2.server_address[1]
        finally:
            s1.server_close()
            s2.server_close()


# ─── Helpers for tests that drive the callback ────────────────────


def _trigger_callback(url: str) -> None:
    """GET the redirect URL from a worker thread (simulates the browser)."""
    # Small delay so the server thread enters serve_forever first; the
    # ephemeral port is bound the moment _build_callback_server returns,
    # so we can connect immediately, but waiting a touch keeps timing
    # robust against slower CI runners.
    time.sleep(0.05)
    try:
        httpx.get(url, timeout=5.0)
    except Exception:
        pass


# ─── Happy path ──────────────────────────────────────────────────


class TestHappyPath:
    def test_returns_token_response(self) -> None:
        captured_urls: list[str] = []

        def _fake_open(url: str, *_a: object, **_k: object) -> bool:
            captured_urls.append(url)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect_uri = params["redirect_uri"][0]
            state = params["state"][0]

            def _go() -> None:
                _trigger_callback(
                    f"{redirect_uri}?code=auth123&state={urllib.parse.quote(state)}"
                )

            threading.Thread(target=_go, daemon=True).start()
            return True

        token_payload = {
            "access_token": "ya29.test",
            "refresh_token": "rt-test",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "read write",
        }

        with (
            patch("opencomputer.mcp.oauth_pkce.webbrowser.open", _fake_open),
            patch("opencomputer.mcp.oauth_pkce.httpx.post") as mock_post,
        ):
            mock_post.return_value = httpx.Response(
                200, json=token_payload, request=httpx.Request("POST", "http://x")
            )
            result = run_pkce_flow(
                authorization_url="https://example.com/authorize",
                token_url="https://example.com/token",
                client_id="cid",
                scope="read write",
                timeout_s=10,
            )

        assert result == token_payload
        assert captured_urls, "webbrowser.open should have been called"
        # The authorize URL must include the PKCE + state params.
        params = urllib.parse.parse_qs(urllib.parse.urlparse(captured_urls[0]).query)
        assert params["response_type"] == ["code"]
        assert params["code_challenge_method"] == ["S256"]
        assert params["client_id"] == ["cid"]
        assert "code_challenge" in params
        assert "state" in params
        assert params["redirect_uri"][0].startswith("http://127.0.0.1:")

        # Token endpoint POST got the correct grant payload.
        post_args, post_kwargs = mock_post.call_args
        assert post_args[0] == "https://example.com/token"
        sent_data = post_kwargs["data"]
        assert sent_data["grant_type"] == "authorization_code"
        assert sent_data["code"] == "auth123"
        assert sent_data["client_id"] == "cid"
        assert sent_data["code_verifier"]  # present + non-empty


# ─── Failure modes ───────────────────────────────────────────────


class TestStateMismatch:
    def test_state_mismatch_raises(self) -> None:
        def _fake_open(url: str, *_a: object, **_k: object) -> bool:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect_uri = params["redirect_uri"][0]
            # Echo a *wrong* state — CSRF defense must fire.

            def _go() -> None:
                _trigger_callback(
                    f"{redirect_uri}?code=evil&state=attacker-controlled"
                )

            threading.Thread(target=_go, daemon=True).start()
            return True

        with (
            patch("opencomputer.mcp.oauth_pkce.webbrowser.open", _fake_open),
            patch("opencomputer.mcp.oauth_pkce.httpx.post") as mock_post,
            pytest.raises(OAuthStateMismatch),
        ):
            run_pkce_flow(
                authorization_url="https://example.com/authorize",
                token_url="https://example.com/token",
                client_id="cid",
                timeout_s=10,
            )

        # Critical: the token endpoint must NOT be called when CSRF check fails.
        mock_post.assert_not_called()


class TestTimeout:
    def test_timeout_raises(self) -> None:
        # webbrowser.open returns True but no callback is ever delivered.
        def _fake_open(url: str, *_a: object, **_k: object) -> bool:
            return True

        with (
            patch("opencomputer.mcp.oauth_pkce.webbrowser.open", _fake_open),
            patch("opencomputer.mcp.oauth_pkce.httpx.post") as mock_post,
            pytest.raises(OAuthFlowTimeout),
        ):
            run_pkce_flow(
                authorization_url="https://example.com/authorize",
                token_url="https://example.com/token",
                client_id="cid",
                timeout_s=1,
            )

        mock_post.assert_not_called()


class TestBrowserOpenFallback:
    def test_browser_open_returns_false_prints_url(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Browser fails to open → we must print the URL (no crash). To
        # avoid hanging on the queue.get, we still need a callback to
        # fire from a side thread.
        captured_urls: list[str] = []

        def _fake_open(url: str, *_a: object, **_k: object) -> bool:
            captured_urls.append(url)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect_uri = params["redirect_uri"][0]
            state = params["state"][0]

            def _go() -> None:
                _trigger_callback(
                    f"{redirect_uri}?code=ok&state={urllib.parse.quote(state)}"
                )

            threading.Thread(target=_go, daemon=True).start()
            return False

        token_payload = {"access_token": "abc", "token_type": "Bearer"}
        with (
            patch("opencomputer.mcp.oauth_pkce.webbrowser.open", _fake_open),
            patch("opencomputer.mcp.oauth_pkce.httpx.post") as mock_post,
        ):
            mock_post.return_value = httpx.Response(
                200, json=token_payload, request=httpx.Request("POST", "http://x")
            )
            result = run_pkce_flow(
                authorization_url="https://example.com/authorize",
                token_url="https://example.com/token",
                client_id="cid",
                timeout_s=10,
            )

        out = capsys.readouterr().out
        assert "Could not auto-open" in out
        assert captured_urls[0] in out
        assert result == token_payload


class TestMalformedCallback:
    def test_missing_code_raises(self) -> None:
        def _fake_open(url: str, *_a: object, **_k: object) -> bool:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect_uri = params["redirect_uri"][0]
            state = params["state"][0]

            def _go() -> None:
                # NO code param — provider returned an error.
                _trigger_callback(
                    f"{redirect_uri}?error=access_denied&state={urllib.parse.quote(state)}"
                )

            threading.Thread(target=_go, daemon=True).start()
            return True

        with (
            patch("opencomputer.mcp.oauth_pkce.webbrowser.open", _fake_open),
            patch("opencomputer.mcp.oauth_pkce.httpx.post") as mock_post,
            pytest.raises(OAuthCallbackError),
        ):
            run_pkce_flow(
                authorization_url="https://example.com/authorize",
                token_url="https://example.com/token",
                client_id="cid",
                timeout_s=10,
            )

        mock_post.assert_not_called()


# ─── Authorize URL construction ──────────────────────────────────


class TestAuthorizeUrlConstruction:
    def test_scope_omitted_when_empty(self) -> None:
        captured_urls: list[str] = []

        def _fake_open(url: str, *_a: object, **_k: object) -> bool:
            captured_urls.append(url)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect_uri = params["redirect_uri"][0]
            state = params["state"][0]

            def _go() -> None:
                _trigger_callback(
                    f"{redirect_uri}?code=ok&state={urllib.parse.quote(state)}"
                )

            threading.Thread(target=_go, daemon=True).start()
            return True

        with (
            patch("opencomputer.mcp.oauth_pkce.webbrowser.open", _fake_open),
            patch("opencomputer.mcp.oauth_pkce.httpx.post") as mock_post,
        ):
            mock_post.return_value = httpx.Response(
                200,
                json={"access_token": "x", "token_type": "Bearer"},
                request=httpx.Request("POST", "http://x"),
            )
            run_pkce_flow(
                authorization_url="https://example.com/authorize",
                token_url="https://example.com/token",
                client_id="cid",
                timeout_s=10,
            )

        params = urllib.parse.parse_qs(urllib.parse.urlparse(captured_urls[0]).query)
        assert "scope" not in params

    def test_extra_authorize_params_included(self) -> None:
        captured_urls: list[str] = []

        def _fake_open(url: str, *_a: object, **_k: object) -> bool:
            captured_urls.append(url)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect_uri = params["redirect_uri"][0]
            state = params["state"][0]

            def _go() -> None:
                _trigger_callback(
                    f"{redirect_uri}?code=ok&state={urllib.parse.quote(state)}"
                )

            threading.Thread(target=_go, daemon=True).start()
            return True

        with (
            patch("opencomputer.mcp.oauth_pkce.webbrowser.open", _fake_open),
            patch("opencomputer.mcp.oauth_pkce.httpx.post") as mock_post,
        ):
            mock_post.return_value = httpx.Response(
                200,
                json={"access_token": "x", "token_type": "Bearer"},
                request=httpx.Request("POST", "http://x"),
            )
            run_pkce_flow(
                authorization_url="https://example.com/authorize",
                token_url="https://example.com/token",
                client_id="cid",
                extra_authorize_params={"audience": "api://default"},
                timeout_s=10,
            )

        params = urllib.parse.parse_qs(urllib.parse.urlparse(captured_urls[0]).query)
        assert params["audience"] == ["api://default"]


class TestInputValidation:
    def test_zero_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="timeout_s"):
            run_pkce_flow(
                authorization_url="https://x",
                token_url="https://y",
                client_id="cid",
                timeout_s=0,
            )

    def test_negative_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match="timeout_s"):
            run_pkce_flow(
                authorization_url="https://x",
                token_url="https://y",
                client_id="cid",
                timeout_s=-5,
            )


# ─── CLI integration ─────────────────────────────────────────────


class TestCli:
    def test_oauth_login_persists_token(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from opencomputer.cli_mcp import mcp_app
        from opencomputer.mcp.oauth import OAuthTokenStore

        monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

        captured_urls: list[str] = []

        def _fake_open(url: str, *_a: object, **_k: object) -> bool:
            captured_urls.append(url)
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect_uri = params["redirect_uri"][0]
            state = params["state"][0]

            def _go() -> None:
                _trigger_callback(
                    f"{redirect_uri}?code=ok&state={urllib.parse.quote(state)}"
                )

            threading.Thread(target=_go, daemon=True).start()
            return True

        token_payload = {
            "access_token": "tok-123",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "repo",
        }

        with (
            patch("opencomputer.mcp.oauth_pkce.webbrowser.open", _fake_open),
            patch("opencomputer.mcp.oauth_pkce.httpx.post") as mock_post,
        ):
            mock_post.return_value = httpx.Response(
                200,
                json=token_payload,
                request=httpx.Request("POST", "http://x"),
            )
            result = CliRunner().invoke(
                mcp_app,
                [
                    "oauth-login",
                    "github",
                    "--authorization-url",
                    "https://example.com/authorize",
                    "--token-url",
                    "https://example.com/token",
                    "--client-id",
                    "cid",
                    "--scope",
                    "repo",
                    "--timeout",
                    "10",
                ],
            )

        assert result.exit_code == 0, result.stdout
        loaded = OAuthTokenStore().get("github")
        assert loaded is not None
        assert loaded.access_token == "tok-123"
        assert loaded.scope == "repo"
        assert loaded.token_type == "Bearer"
        # expires_at was derived from expires_in (~now + 3600s).
        assert loaded.expires_at is not None
        assert loaded.expires_at > time.time() + 3500

    def test_oauth_login_state_mismatch_exits_nonzero(
        self, tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from opencomputer.cli_mcp import mcp_app
        from opencomputer.mcp.oauth import OAuthTokenStore

        monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

        def _fake_open(url: str, *_a: object, **_k: object) -> bool:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            redirect_uri = params["redirect_uri"][0]

            def _go() -> None:
                _trigger_callback(
                    f"{redirect_uri}?code=evil&state=attacker"
                )

            threading.Thread(target=_go, daemon=True).start()
            return True

        with patch("opencomputer.mcp.oauth_pkce.webbrowser.open", _fake_open):
            result = CliRunner().invoke(
                mcp_app,
                [
                    "oauth-login",
                    "github",
                    "--authorization-url",
                    "https://example.com/authorize",
                    "--token-url",
                    "https://example.com/token",
                    "--client-id",
                    "cid",
                    "--timeout",
                    "10",
                ],
            )

        # Exit code 3 = OAuthStateMismatch per cli_mcp mapping.
        assert result.exit_code == 3, result.stdout
        # Nothing should be persisted on CSRF failure.
        assert OAuthTokenStore().get("github") is None
