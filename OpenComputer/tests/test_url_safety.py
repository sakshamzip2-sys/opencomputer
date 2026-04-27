"""TS-T4 — URL safety / SSRF tests.

Cover the IP-class block list, the always-blocked metadata endpoints,
the env-var toggle, the CGNAT range that ``ipaddress.is_private`` misses,
and the fail-closed behaviour for DNS errors / malformed URLs.
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from opencomputer.security.url_safety import (
    _reset_allow_private_cache,
    is_safe_url,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset the global ``_global_allow_private_urls`` cache between tests
    so monkeypatched env vars take effect on the very next call."""
    _reset_allow_private_cache()
    yield
    _reset_allow_private_cache()


def _fake_addr_info(ip: str):
    """Mimic ``socket.getaddrinfo`` for a single IPv4/IPv6 literal."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 0, "", (ip, 0))]


def test_safe_external_url_passes(monkeypatch):
    """A real public hostname (google.com) resolves to a public IP and passes."""
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    # Use a fake resolver to keep the test offline-stable.
    with patch(
        "opencomputer.security.url_safety.socket.getaddrinfo",
        return_value=_fake_addr_info("142.250.80.46"),
    ):
        assert is_safe_url("https://www.google.com") is True


def test_localhost_blocked(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    with patch(
        "opencomputer.security.url_safety.socket.getaddrinfo",
        return_value=_fake_addr_info("127.0.0.1"),
    ):
        assert is_safe_url("http://localhost:8080") is False
        assert is_safe_url("http://127.0.0.1") is False


def test_169_254_169_254_always_blocked(monkeypatch):
    """Even with the toggle on, cloud metadata is ALWAYS blocked."""
    monkeypatch.setenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", "true")
    with patch(
        "opencomputer.security.url_safety.socket.getaddrinfo",
        return_value=_fake_addr_info("169.254.169.254"),
    ):
        assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False


def test_metadata_google_internal_always_blocked(monkeypatch):
    """The hostname is on the always-blocked list — short-circuits before DNS."""
    monkeypatch.setenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", "true")
    assert is_safe_url("http://metadata.google.internal/") is False


def test_rfc1918_blocked_by_default(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    for ip in ("10.0.0.1", "192.168.1.1", "172.16.0.1"):
        with patch(
            "opencomputer.security.url_safety.socket.getaddrinfo",
            return_value=_fake_addr_info(ip),
        ):
            assert is_safe_url(f"http://{ip}") is False, ip


def test_cgnat_range_blocked(monkeypatch):
    """100.64.0.0/10 is NOT covered by ``ip.is_private`` — must be explicit."""
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    with patch(
        "opencomputer.security.url_safety.socket.getaddrinfo",
        return_value=_fake_addr_info("100.64.0.1"),
    ):
        assert is_safe_url("http://100.64.0.1") is False


def test_dns_failure_blocks_request(monkeypatch):
    """DNS resolution failure is fail-closed: blocked."""
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    with patch(
        "opencomputer.security.url_safety.socket.getaddrinfo",
        side_effect=socket.gaierror("[Errno 8] nodename nor servname provided"),
    ):
        assert is_safe_url("http://this-domain-does-not-resolve-12345.invalid") is False


def test_env_toggle_allows_private(monkeypatch):
    """With ``OPENCOMPUTER_ALLOW_PRIVATE_URLS=true`` an RFC1918 IP is allowed."""
    monkeypatch.setenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", "true")
    with patch(
        "opencomputer.security.url_safety.socket.getaddrinfo",
        return_value=_fake_addr_info("10.0.0.1"),
    ):
        assert is_safe_url("http://10.0.0.1") is True


def test_env_toggle_false_blocks_private(monkeypatch):
    """An explicit ``false`` short-circuits before reading config.yaml,
    so private IPs stay blocked even if a stray config file says otherwise."""
    monkeypatch.setenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", "false")
    with patch(
        "opencomputer.security.url_safety.socket.getaddrinfo",
        return_value=_fake_addr_info("10.0.0.1"),
    ):
        assert is_safe_url("http://10.0.0.1") is False


def test_malformed_url_blocked(monkeypatch):
    """Empty / unparseable URLs hit the fail-closed exception path."""
    monkeypatch.delenv("OPENCOMPUTER_ALLOW_PRIVATE_URLS", raising=False)
    assert is_safe_url("not-a-url") is False
    assert is_safe_url("") is False
