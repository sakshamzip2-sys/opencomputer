"""Tests for opencomputer/auth/external.py — browser-redirect OAuth helper.

Covers PKCE pair generation, redirect_uri validation, and the local
HTTP callback server (start, capture code, timeout, error response).
"""
from __future__ import annotations

import threading
import time
import urllib.request
from urllib.parse import urlencode

import pytest


def test_generate_pkce_pair_returns_verifier_and_challenge():
    from opencomputer.auth.external import generate_pkce_pair

    pair = generate_pkce_pair()
    assert len(pair.verifier) >= 43  # RFC 7636 minimum
    assert len(pair.verifier) <= 128
    assert pair.challenge
    # S256 = base64url(sha256(verifier))
    assert pair.method == "S256"


def test_generate_pkce_pair_produces_unique_pairs():
    from opencomputer.auth.external import generate_pkce_pair

    a = generate_pkce_pair()
    b = generate_pkce_pair()
    assert a.verifier != b.verifier


def test_validate_redirect_uri_accepts_localhost():
    from opencomputer.auth.external import validate_redirect_uri

    host, port, path = validate_redirect_uri("http://localhost:8765/callback")
    assert host == "localhost"
    assert port == 8765
    assert path == "/callback"


def test_validate_redirect_uri_accepts_127_0_0_1():
    from opencomputer.auth.external import validate_redirect_uri

    host, port, path = validate_redirect_uri("http://127.0.0.1:8765/cb")
    assert host == "127.0.0.1"
    assert port == 8765


def test_validate_redirect_uri_rejects_https():
    from opencomputer.auth.external import validate_redirect_uri

    with pytest.raises(ValueError, match="http://"):
        validate_redirect_uri("https://localhost:8765/callback")


def test_validate_redirect_uri_rejects_non_loopback():
    from opencomputer.auth.external import validate_redirect_uri

    with pytest.raises(ValueError, match="loopback"):
        validate_redirect_uri("http://my.example.com:8765/callback")


def test_validate_redirect_uri_requires_explicit_port():
    from opencomputer.auth.external import validate_redirect_uri

    with pytest.raises(ValueError, match="port"):
        validate_redirect_uri("http://localhost/callback")


def test_validate_redirect_uri_defaults_path_to_root():
    from opencomputer.auth.external import validate_redirect_uri

    _, _, path = validate_redirect_uri("http://localhost:8765")
    assert path == "/"


def test_wait_for_redirect_captures_code(unused_tcp_port):
    """Spin up the local server, send a callback, expect the code captured."""
    from opencomputer.auth.external import wait_for_redirect_callback

    redirect_uri = f"http://localhost:{unused_tcp_port}/callback"
    captured = {}

    def hit_callback():
        time.sleep(0.2)  # wait for server to bind
        params = urlencode({"code": "auth-code-xyz", "state": "csrf-123"})
        url = f"{redirect_uri}?{params}"
        try:
            urllib.request.urlopen(url, timeout=5)  # noqa: S310 - localhost only
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=hit_callback, daemon=True).start()

    captured = wait_for_redirect_callback(redirect_uri, timeout_seconds=10.0)
    assert captured["code"] == "auth-code-xyz"
    assert captured["state"] == "csrf-123"
    assert captured.get("error") is None


def test_wait_for_redirect_captures_error(unused_tcp_port):
    from opencomputer.auth.external import wait_for_redirect_callback

    redirect_uri = f"http://localhost:{unused_tcp_port}/cb"

    def hit_callback():
        time.sleep(0.2)
        params = urlencode({"error": "access_denied", "error_description": "user-cancel"})
        try:
            urllib.request.urlopen(  # noqa: S310 - localhost only
                f"{redirect_uri}?{params}", timeout=5
            )
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=hit_callback, daemon=True).start()

    captured = wait_for_redirect_callback(redirect_uri, timeout_seconds=10.0)
    assert captured["code"] is None
    assert captured["error"] == "access_denied"
    assert captured["error_description"] == "user-cancel"


def test_wait_for_redirect_times_out(unused_tcp_port):
    from opencomputer.auth.external import wait_for_redirect_callback

    redirect_uri = f"http://localhost:{unused_tcp_port}/cb"
    with pytest.raises(TimeoutError):
        wait_for_redirect_callback(redirect_uri, timeout_seconds=0.5)


def test_wait_for_redirect_ignores_other_paths(unused_tcp_port):
    """A request to /unrelated should not satisfy /callback."""
    from opencomputer.auth.external import wait_for_redirect_callback

    redirect_uri = f"http://localhost:{unused_tcp_port}/callback"

    def hit_wrong_path():
        time.sleep(0.2)
        params = urlencode({"code": "should-be-ignored"})
        try:
            urllib.request.urlopen(  # noqa: S310 - localhost only
                f"http://localhost:{unused_tcp_port}/wrong-path?{params}", timeout=5
            )
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=hit_wrong_path, daemon=True).start()
    with pytest.raises(TimeoutError):
        wait_for_redirect_callback(redirect_uri, timeout_seconds=1.5)


@pytest.fixture
def unused_tcp_port():
    """Find an available port for tests."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
