"""Telegram IP-fallback transport tests (PR 4.1).

Covers:

1. ``parse_fallback_ip`` validates IPv4, rejects IPv6 / private /
   loopback / link-local / multicast / reserved.
2. ``parse_fallback_ip_env`` parses comma-separated lists, treats
   ``"auto"`` as a sentinel returning ``[]``, drops invalid entries.
3. ``TelegramFallbackTransport`` rewrites the URL host + Host header
   + TLS SNI extension when a connect failure forces a fallback.
4. Sticky-IP behaviour: once a fallback IP succeeds, subsequent
   requests reuse it without re-trying the original host first.
5. Sticky-IP busts on failure and re-discovers a working route.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from extensions.telegram.network import (
    TelegramFallbackTransport,
    parse_fallback_ip,
    parse_fallback_ip_env,
)

# ---------------------------------------------------------------------------
# parse_fallback_ip
# ---------------------------------------------------------------------------


class TestParseFallbackIp:
    def test_valid_public_ipv4(self) -> None:
        assert parse_fallback_ip("149.154.167.220") == "149.154.167.220"

    def test_rejects_ipv6(self) -> None:
        assert parse_fallback_ip("2001:db8::1") is None

    def test_rejects_private(self) -> None:
        assert parse_fallback_ip("10.0.0.1") is None
        assert parse_fallback_ip("192.168.1.1") is None
        assert parse_fallback_ip("172.16.0.1") is None

    def test_rejects_loopback(self) -> None:
        assert parse_fallback_ip("127.0.0.1") is None

    def test_rejects_link_local(self) -> None:
        assert parse_fallback_ip("169.254.1.1") is None

    def test_rejects_multicast(self) -> None:
        assert parse_fallback_ip("224.0.0.1") is None

    def test_rejects_reserved(self) -> None:
        # 240.0.0.0/4 is "reserved for future use"
        assert parse_fallback_ip("240.0.0.1") is None

    def test_rejects_unspecified(self) -> None:
        assert parse_fallback_ip("0.0.0.0") is None

    def test_rejects_garbage(self) -> None:
        assert parse_fallback_ip("not-an-ip") is None
        assert parse_fallback_ip("") is None
        assert parse_fallback_ip("999.999.999.999") is None


# ---------------------------------------------------------------------------
# parse_fallback_ip_env
# ---------------------------------------------------------------------------


class TestParseFallbackIpEnv:
    def test_empty_returns_empty(self) -> None:
        assert parse_fallback_ip_env("") == []

    def test_auto_returns_empty_sentinel(self) -> None:
        # "auto" is a sentinel: the caller invokes discover_fallback_ips
        # separately. parse_fallback_ip_env returns [] for it.
        assert parse_fallback_ip_env("auto") == []
        assert parse_fallback_ip_env("AUTO") == []
        assert parse_fallback_ip_env(" auto ") == []

    def test_single_ip(self) -> None:
        assert parse_fallback_ip_env("149.154.167.220") == ["149.154.167.220"]

    def test_comma_separated(self) -> None:
        out = parse_fallback_ip_env("149.154.167.220,149.154.175.50")
        assert out == ["149.154.167.220", "149.154.175.50"]

    def test_strips_whitespace(self) -> None:
        out = parse_fallback_ip_env("  149.154.167.220 ,  149.154.175.50  ")
        assert out == ["149.154.167.220", "149.154.175.50"]

    def test_drops_invalid(self) -> None:
        out = parse_fallback_ip_env("149.154.167.220,10.0.0.1,not-an-ip")
        assert out == ["149.154.167.220"]


# ---------------------------------------------------------------------------
# TelegramFallbackTransport — URL rewrite + sticky behaviour
# ---------------------------------------------------------------------------


def _ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(status_code=200, request=request, json={"ok": True})


def _make_inner_mock(
    behaviour: list,  # list of "ok" | "fail"
) -> MagicMock:
    """Inner transport mock: each call consumes one entry from behaviour."""
    inner = MagicMock(spec=httpx.AsyncBaseTransport)
    calls: list[httpx.Request] = []

    async def _handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if not behaviour:
            return _ok_response(request)
        action = behaviour.pop(0)
        if action == "fail":
            raise httpx.ConnectError("mock connect failure")
        return _ok_response(request)

    inner.handle_async_request = AsyncMock(side_effect=_handle)
    inner.aclose = AsyncMock()
    inner._calls = calls  # type: ignore[attr-defined]
    return inner


class TestFallbackTransport:
    @pytest.mark.asyncio
    async def test_primary_host_success_no_rewrite(self) -> None:
        inner = _make_inner_mock(["ok"])
        t = TelegramFallbackTransport(["149.154.167.220"], inner=inner)
        req = httpx.Request("GET", "https://api.telegram.org/bot/getMe")
        resp = await t.handle_async_request(req)
        assert resp.status_code == 200
        # Inner saw the original request unchanged.
        assert len(inner._calls) == 1
        assert inner._calls[0].url.host == "api.telegram.org"
        assert "sni_hostname" not in inner._calls[0].extensions

    @pytest.mark.asyncio
    async def test_fallback_rewrites_host_and_sni(self) -> None:
        # Primary fails, first fallback IP succeeds.
        inner = _make_inner_mock(["fail", "ok"])
        t = TelegramFallbackTransport(["149.154.167.220"], inner=inner)
        req = httpx.Request("GET", "https://api.telegram.org/bot/getMe")
        resp = await t.handle_async_request(req)
        assert resp.status_code == 200
        assert len(inner._calls) == 2
        # Primary attempt — host unchanged.
        assert inner._calls[0].url.host == "api.telegram.org"
        # Fallback attempt — host is the IP; Host header and SNI
        # preserve api.telegram.org so cert + vhost still work.
        assert inner._calls[1].url.host == "149.154.167.220"
        assert inner._calls[1].headers["host"] == "api.telegram.org"
        assert (
            inner._calls[1].extensions.get("sni_hostname")
            == "api.telegram.org"
        )

    @pytest.mark.asyncio
    async def test_sticky_ip_used_on_subsequent_request(self) -> None:
        # First request: primary fails, fallback succeeds → sticky.
        # Second request: should go DIRECTLY to the sticky IP (no
        # primary-host attempt).
        inner = _make_inner_mock(["fail", "ok", "ok"])
        t = TelegramFallbackTransport(["149.154.167.220"], inner=inner)
        req1 = httpx.Request("GET", "https://api.telegram.org/bot/getMe")
        await t.handle_async_request(req1)
        req2 = httpx.Request("GET", "https://api.telegram.org/bot/getUpdates")
        await t.handle_async_request(req2)
        # Total 3 inner calls: req1-primary (fail), req1-fallback (ok),
        # req2-sticky (ok). The second request did NOT re-try primary.
        assert len(inner._calls) == 3
        assert inner._calls[2].url.host == "149.154.167.220"

    @pytest.mark.asyncio
    async def test_sticky_busts_on_failure(self) -> None:
        # Establish stickiness first.
        inner = _make_inner_mock(["fail", "ok"])
        t = TelegramFallbackTransport(["149.154.167.220"], inner=inner)
        await t.handle_async_request(
            httpx.Request("GET", "https://api.telegram.org/bot/getMe")
        )
        assert t._sticky_ip == "149.154.167.220"
        # Now make sticky fail; primary succeeds — sticky should be busted.
        inner._calls.clear()
        inner.handle_async_request.side_effect = None

        async def _handle(request: httpx.Request) -> httpx.Response:
            inner._calls.append(request)
            # Sticky attempt fails; subsequent (primary) succeeds.
            if (
                request.url.host == "149.154.167.220"
                and len(inner._calls) == 1
            ):
                raise httpx.ConnectError("sticky now down")
            return _ok_response(request)

        inner.handle_async_request.side_effect = _handle
        await t.handle_async_request(
            httpx.Request("GET", "https://api.telegram.org/bot/getUpdates")
        )
        # Sticky was busted by the failed sticky attempt.
        assert t._sticky_ip is None

    @pytest.mark.asyncio
    async def test_all_fallbacks_exhausted_raises(self) -> None:
        inner = _make_inner_mock(["fail", "fail", "fail"])
        t = TelegramFallbackTransport(
            ["149.154.167.220", "149.154.175.50"], inner=inner
        )
        with pytest.raises(httpx.ConnectError):
            await t.handle_async_request(
                httpx.Request("GET", "https://api.telegram.org/bot/getMe")
            )

    @pytest.mark.asyncio
    async def test_aclose_propagates_to_inner(self) -> None:
        inner = _make_inner_mock([])
        t = TelegramFallbackTransport(["149.154.167.220"], inner=inner)
        await t.aclose()
        inner.aclose.assert_awaited_once()
