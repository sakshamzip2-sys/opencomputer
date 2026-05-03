"""Unit tests for browser-port `session/nav_guard.py` (Wave 1a).

Covers:
  - block file://, chrome://, about:* (except about:blank)
  - block private IPv4 ranges (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16)
  - block IPv6 loopback / link-local / unique-local
  - dangerously_allow_private_network bypasses private-IP checks
  - allowed_hostnames + hostname_allowlist allow specific hosts
  - DNS resolution failure → block (fail-closed)
  - the route handler latches the first top-level deny and re-aborts
    subsequent same-nav requests
  - subframe nav errors are blocked but don't latch the top-level error
  - post-nav redirect-chain validation catches a redirect to a private IP
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from extensions.browser_control.profiles import SsrfPolicy
from extensions.browser_control.session.nav_guard import (
    InvalidBrowserNavigationUrlError,
    NavigationGuardPolicy,
    SsrfBlockedError,
    assert_browser_navigation_allowed,
    assert_navigation_result_allowed,
    install_navigation_guard,
)

# ─── fakes ────────────────────────────────────────────────────────────


class FakeFrame:
    pass


class FakeRequest:
    def __init__(
        self,
        url: str,
        *,
        frame: Any | None = None,
        is_navigation: bool = True,
        resource_type: str = "document",
        redirected_from: Any | None = None,
    ) -> None:
        self.url = url
        self.frame = frame
        self._is_navigation = is_navigation
        self.resource_type = resource_type
        self.redirected_from = redirected_from

    def is_navigation_request(self) -> bool:
        return self._is_navigation


class FakeRoute:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request
        self.continue_called = 0
        self.abort_called = 0

    async def continue_(self) -> None:
        self.continue_called += 1

    async def abort(self) -> None:
        self.abort_called += 1


class FakePage:
    def __init__(self) -> None:
        self.main_frame = FakeFrame()
        self._handler: Any | None = None

    async def route(self, _glob: str, handler: Any) -> None:
        self._handler = handler

    async def unroute(self, _glob: str, _handler: Any) -> None:
        self._handler = None


# ─── assert_browser_navigation_allowed ────────────────────────────────


def _stub_resolver(mapping: dict[str, list[str]]):
    def resolver(host: str) -> list[str]:
        return mapping.get(host.lower(), [])

    return resolver


@pytest.mark.asyncio
async def test_about_blank_allowed() -> None:
    await assert_browser_navigation_allowed("about:blank")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "chrome://settings",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
    ],
)
async def test_non_network_schemes_blocked(url: str) -> None:
    with pytest.raises(InvalidBrowserNavigationUrlError):
        await assert_browser_navigation_allowed(url)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "127.0.0.1",
        "169.254.169.254",  # AWS metadata
        "::1",
        "fe80::1",
        "fd00::1",
    ],
)
async def test_private_ip_literals_blocked(host: str) -> None:
    url = f"http://[{host}]/" if ":" in host else f"http://{host}/"
    with pytest.raises(SsrfBlockedError):
        await assert_browser_navigation_allowed(url)


@pytest.mark.asyncio
async def test_blocked_hostname_metadata() -> None:
    with pytest.raises(SsrfBlockedError):
        await assert_browser_navigation_allowed(
            "http://metadata.google.internal/computeMetadata/v1/"
        )


@pytest.mark.asyncio
async def test_dangerously_allow_private_network() -> None:
    policy = NavigationGuardPolicy(
        ssrf_policy=SsrfPolicy(dangerously_allow_private_network=True)
    )
    await assert_browser_navigation_allowed("http://10.0.0.5/", policy=policy)


@pytest.mark.asyncio
async def test_allowed_hostnames_exact_match() -> None:
    policy = NavigationGuardPolicy(
        ssrf_policy=SsrfPolicy(allowed_hostnames=["internal.example.com"])
    )
    # Even though it would resolve to a private IP, the explicit allow wins.
    await assert_browser_navigation_allowed(
        "http://internal.example.com/", policy=policy
    )


@pytest.mark.asyncio
async def test_hostname_allowlist_substring() -> None:
    policy = NavigationGuardPolicy(
        ssrf_policy=SsrfPolicy(hostname_allowlist=["example.com"])
    )
    await assert_browser_navigation_allowed("http://api.example.com/", policy=policy)


@pytest.mark.asyncio
async def test_dns_failure_blocks_fail_closed() -> None:
    policy = NavigationGuardPolicy(resolver=_stub_resolver({}))
    with pytest.raises(SsrfBlockedError, match="could not resolve"):
        await assert_browser_navigation_allowed("http://no-such-host.invalid/", policy=policy)


@pytest.mark.asyncio
async def test_dns_resolves_to_private_ip_blocks() -> None:
    policy = NavigationGuardPolicy(
        resolver=_stub_resolver({"sneaky.example.com": ["10.0.0.5"]})
    )
    with pytest.raises(SsrfBlockedError, match="resolves to private"):
        await assert_browser_navigation_allowed(
            "http://sneaky.example.com/", policy=policy
        )


@pytest.mark.asyncio
async def test_dns_resolves_to_public_ip_allowed() -> None:
    policy = NavigationGuardPolicy(
        resolver=_stub_resolver({"example.com": ["93.184.216.34"]})
    )
    await assert_browser_navigation_allowed("http://example.com/", policy=policy)


# ─── route handler ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_handler_continues_subresource() -> None:
    page = FakePage()
    state = await install_navigation_guard(page)
    # A subresource (different frame, not navigation, resource_type = "image").
    other_frame = FakeFrame()
    req = FakeRequest(
        "http://10.0.0.1/img.png",
        frame=other_frame,
        is_navigation=False,
        resource_type="image",
    )
    route = FakeRoute(req)
    await page._handler(route)
    assert route.continue_called == 1
    assert route.abort_called == 0
    assert state.blocked_error is None


@pytest.mark.asyncio
async def test_route_handler_blocks_top_level_private_ip() -> None:
    page = FakePage()
    state = await install_navigation_guard(page)
    req = FakeRequest("http://10.0.0.1/", frame=page.main_frame)
    route = FakeRoute(req)
    await page._handler(route)
    assert route.abort_called == 1
    assert isinstance(state.blocked_error, SsrfBlockedError)


@pytest.mark.asyncio
async def test_route_handler_aborts_followups_after_top_level_block() -> None:
    page = FakePage()
    state = await install_navigation_guard(page)

    req1 = FakeRequest("http://10.0.0.1/", frame=page.main_frame)
    route1 = FakeRoute(req1)
    await page._handler(route1)
    assert isinstance(state.blocked_error, SsrfBlockedError)
    assert route1.abort_called == 1

    # A subsequent request (e.g. subresource for the same nav) is aborted.
    req2 = FakeRequest(
        "http://10.0.0.1/img.png",
        frame=FakeFrame(),
        is_navigation=False,
        resource_type="image",
    )
    route2 = FakeRoute(req2)
    await page._handler(route2)
    assert route2.abort_called == 1
    assert route2.continue_called == 0


@pytest.mark.asyncio
async def test_route_handler_subframe_block_does_not_latch() -> None:
    """A blocked subframe nav aborts but doesn't latch the top-level error."""
    page = FakePage()
    state = await install_navigation_guard(page)
    sub_frame = FakeFrame()
    req = FakeRequest(
        "http://10.0.0.1/iframe", frame=sub_frame, is_navigation=True, resource_type="document"
    )
    route = FakeRoute(req)
    await page._handler(route)
    assert route.abort_called == 1
    assert state.blocked_error is None


# ─── redirect chain ───────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, request: FakeRequest) -> None:
        self.request = request


@pytest.mark.asyncio
async def test_post_nav_revalidate_catches_private_redirect() -> None:
    # Public → private redirect chain.
    initial = FakeRequest(
        "http://example.com/",
        redirected_from=None,
    )
    final = FakeRequest(
        "http://10.0.0.1/admin",
        redirected_from=initial,
    )
    resp = FakeResponse(final)
    policy = NavigationGuardPolicy(
        resolver=_stub_resolver({"example.com": ["93.184.216.34"]})
    )
    with pytest.raises(SsrfBlockedError):
        await assert_navigation_result_allowed(resp, policy=policy)


@pytest.mark.asyncio
async def test_post_nav_revalidate_allows_clean_redirect() -> None:
    initial = FakeRequest("http://example.com/", redirected_from=None)
    final = FakeRequest("http://example.org/landing", redirected_from=initial)
    resp = FakeResponse(final)
    policy = NavigationGuardPolicy(
        resolver=_stub_resolver(
            {
                "example.com": ["93.184.216.34"],
                "example.org": ["93.184.216.35"],
            }
        )
    )
    await assert_navigation_result_allowed(resp, policy=policy)


@pytest.mark.asyncio
async def test_request_with_no_frame_treated_as_top_level() -> None:
    """Fail-closed: if frame access raises, the guard runs as if it were top-level."""

    class FrameRaises:
        url = "http://10.0.0.1/"
        resource_type = "document"

        def is_navigation_request(self) -> bool:
            return True

        @property
        def frame(self) -> Any:
            raise RuntimeError("frame disposed")

    page = FakePage()
    state = await install_navigation_guard(page)
    route = FakeRoute(FrameRaises())  # type: ignore[arg-type]
    await page._handler(route)
    assert route.abort_called == 1
    assert isinstance(state.blocked_error, SsrfBlockedError)
