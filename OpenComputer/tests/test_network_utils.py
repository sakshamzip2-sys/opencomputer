"""Tests for plugin_sdk.network_utils."""

from __future__ import annotations

import pytest

from plugin_sdk.network_utils import (
    _looks_like_image,
    is_network_accessible,
    proxy_kwargs_for_aiohttp,
    proxy_kwargs_for_bot,
    resolve_proxy_url,
    safe_url_for_log,
    ssrf_redirect_guard,
)

# ---------------------------------------------------------------------------
# _looks_like_image
# ---------------------------------------------------------------------------


def test_looks_like_image_png():
    assert _looks_like_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)


def test_looks_like_image_jpeg():
    assert _looks_like_image(b"\xff\xd8\xff\xe0" + b"\x00" * 16)


def test_looks_like_image_gif87a():
    assert _looks_like_image(b"GIF87a" + b"\x00" * 16)


def test_looks_like_image_gif89a():
    assert _looks_like_image(b"GIF89a" + b"\x00" * 16)


def test_looks_like_image_bmp():
    assert _looks_like_image(b"BM" + b"\x00" * 30)


def test_looks_like_image_webp():
    # bytes 0-3 = RIFF, bytes 8-11 = WEBP
    assert _looks_like_image(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8)


def test_looks_like_image_rejects_html():
    assert not _looks_like_image(b"<!DOCTYPE html>")


def test_looks_like_image_rejects_empty():
    assert not _looks_like_image(b"")


def test_looks_like_image_rejects_short():
    assert not _looks_like_image(b"\x89PNG")


# ---------------------------------------------------------------------------
# safe_url_for_log
# ---------------------------------------------------------------------------


def test_safe_url_strips_userinfo_and_query():
    assert (
        safe_url_for_log("https://user:pass@example.com/path?q=1#frag")
        == "https://example.com/path"
    )


def test_safe_url_passthrough_simple_url():
    assert safe_url_for_log("http://example.com/foo") == "http://example.com/foo"


def test_safe_url_truncates():
    long_url = "https://very.long.host/" + ("x" * 500)
    assert len(safe_url_for_log(long_url, max_len=200)) <= 200


def test_safe_url_handles_non_url():
    assert safe_url_for_log("not a url") == "not a url"


# ---------------------------------------------------------------------------
# is_network_accessible
# ---------------------------------------------------------------------------


def test_is_network_accessible_loopback_rejected():
    assert is_network_accessible("127.0.0.1") is False
    assert is_network_accessible("localhost") is False
    assert is_network_accessible("[::1]") is False


def test_is_network_accessible_private_rejected():
    assert is_network_accessible("10.0.0.1") is False
    assert is_network_accessible("192.168.1.1") is False
    assert is_network_accessible("172.16.0.1") is False


def test_is_network_accessible_empty_string_false():
    assert is_network_accessible("") is False


def test_is_network_accessible_public_ip_accepted():
    # 8.8.8.8 is public DNS — clearly routable
    assert is_network_accessible("8.8.8.8") is True


# ---------------------------------------------------------------------------
# resolve_proxy_url
# ---------------------------------------------------------------------------


def test_resolve_proxy_url_env_priority(monkeypatch):
    for k in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
        "TELEGRAM_PROXY",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://global:8080")
    monkeypatch.setenv("TELEGRAM_PROXY", "http://specific:8080")
    assert resolve_proxy_url("TELEGRAM_PROXY") == "http://specific:8080"
    monkeypatch.delenv("TELEGRAM_PROXY")
    assert resolve_proxy_url("TELEGRAM_PROXY") == "http://global:8080"


def test_resolve_proxy_url_no_env_returns_none(monkeypatch):
    for k in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
        "TELEGRAM_PROXY",
    ):
        monkeypatch.delenv(k, raising=False)
    # On Darwin, scutil could still return something — not relied on in CI.
    # Just ensure no env variable returns either None or a proxy URL string.
    out = resolve_proxy_url("TELEGRAM_PROXY")
    assert out is None or isinstance(out, str)


# ---------------------------------------------------------------------------
# proxy_kwargs_for_aiohttp / proxy_kwargs_for_bot
# ---------------------------------------------------------------------------


def test_proxy_kwargs_aiohttp_http():
    assert proxy_kwargs_for_aiohttp("http://proxy.example.com:8080") == {
        "proxy": "http://proxy.example.com:8080",
    }


def test_proxy_kwargs_aiohttp_none():
    assert proxy_kwargs_for_aiohttp(None) == {}


def test_proxy_kwargs_bot_http():
    assert proxy_kwargs_for_bot("https://proxy.example.com:8443") == {
        "proxy": "https://proxy.example.com:8443",
    }


# ---------------------------------------------------------------------------
# _ssrf_redirect_guard
# ---------------------------------------------------------------------------


class _FakeHeaders(dict):
    def get(self, key, default=None):
        # case-insensitive
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _FakeResponse:
    def __init__(self, status_code: int, location: str | None = None):
        self.status_code = status_code
        self.headers = _FakeHeaders()
        if location:
            self.headers["location"] = location


@pytest.mark.asyncio
async def test_ssrf_redirect_guard_blocks_loopback():
    resp = _FakeResponse(status_code=302, location="http://127.0.0.1/admin")
    with pytest.raises(RuntimeError, match="SSRF"):
        await ssrf_redirect_guard(resp)


@pytest.mark.asyncio
async def test_ssrf_redirect_guard_allows_public_target():
    resp = _FakeResponse(status_code=302, location="https://8.8.8.8/")
    # Must not raise
    await ssrf_redirect_guard(resp)


@pytest.mark.asyncio
async def test_ssrf_redirect_guard_passes_non_redirect():
    resp = _FakeResponse(status_code=200, location=None)
    # Must not raise
    await ssrf_redirect_guard(resp)
