"""Tests for browser-control client/auth.py — loopback detection +
auth-header injection."""

from __future__ import annotations

import pytest
from extensions.browser_control.client.auth import (
    BrowserAuth,
    inject_auth_headers,
    is_loopback_host,
    is_loopback_url,
)


class TestIsLoopbackHost:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("localhost", True),
            ("LOCALHOST", True),  # case-insensitive
            ("127.0.0.1", True),
            ("127.5.5.5", True),  # 127.0.0.0/8
            ("::1", True),
            ("[::1]", True),  # bracketed IPv6
            ("0.0.0.0", False),  # unspecified, not loopback
            ("8.8.8.8", False),
            ("evil.com", False),
            ("", False),
        ],
    )
    def test_table(self, host: str, expected: bool):
        assert is_loopback_host(host) is expected

    def test_ipv4_mapped_ipv6(self):
        # ::ffff:127.0.0.1 wraps a loopback v4
        assert is_loopback_host("::ffff:127.0.0.1") is True


class TestIsLoopbackUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("http://127.0.0.1:18888/", True),
            ("http://localhost:8080/snapshot", True),
            ("https://localhost/", True),
            ("http://[::1]:18000/", True),
            ("http://example.com/", False),
            ("ftp://localhost/", False),  # wrong scheme
            ("/path-only", False),  # not absolute
            ("", False),
        ],
    )
    def test_table(self, url: str, expected: bool):
        assert is_loopback_url(url) is expected


class TestInjectAuthHeaders:
    def test_loopback_token_attached(self):
        auth = BrowserAuth(token="abc")
        out = inject_auth_headers(
            None, auth=auth, url="http://127.0.0.1:18888/snapshot"
        )
        assert out["Authorization"] == "Bearer abc"

    def test_loopback_password_attached_when_no_token(self):
        auth = BrowserAuth(password="hunter2")
        out = inject_auth_headers(
            None, auth=auth, url="http://localhost:18888/snapshot"
        )
        assert out["X-OpenComputer-Password"] == "hunter2"

    def test_token_preferred_over_password(self):
        auth = BrowserAuth(token="abc", password="hunter2")
        out = inject_auth_headers(
            None, auth=auth, url="http://127.0.0.1:18888/"
        )
        assert "Authorization" in out
        assert "X-OpenComputer-Password" not in out

    def test_non_loopback_skipped(self):
        """Critical: non-loopback URL must NOT get auth, even if configured."""
        auth = BrowserAuth(token="abc")
        out = inject_auth_headers(
            None, auth=auth, url="https://evil.com/api"
        )
        assert "Authorization" not in out

    def test_caller_supplied_auth_wins(self):
        auth = BrowserAuth(token="abc")
        out = inject_auth_headers(
            {"Authorization": "Bearer custom"}, auth=auth, url="http://127.0.0.1:1/"
        )
        assert out["Authorization"] == "Bearer custom"

    def test_no_auth_no_op(self):
        out = inject_auth_headers(
            {"X-Custom": "value"}, auth=None, url="http://127.0.0.1:1/"
        )
        assert "Authorization" not in out
        assert out["X-Custom"] == "value"

    def test_anonymous_auth_no_op(self):
        # anonymous BrowserAuth (no token, no password) → no header
        out = inject_auth_headers(
            None, auth=BrowserAuth(), url="http://127.0.0.1:1/"
        )
        assert "Authorization" not in out
        assert "X-OpenComputer-Password" not in out
