"""Navigation guard — SSRF defense for any URL the agent tries to load.

Two surfaces:

1. ``assert_browser_navigation_allowed(url, *, ssrf_policy)`` — raises
   ``InvalidBrowserNavigationUrlError`` for non-network URLs (file://,
   chrome://) and ``SsrfBlockedError`` for hosts that resolve to a private
   IP / loopback / link-local range or hit the configured blocklist.
   ``about:blank`` is the only non-network URL allowed.

2. ``install_navigation_guard(page, *, ssrf_policy)`` — registers a
   ``page.route("**/*", handler)`` that runs the guard pre-nav for every
   top-level / sub-frame document request. **Fail-closed**: if frame
   resolution throws, the request is treated as a top-level nav and the
   guard runs anyway.

Plus a ``post_nav_revalidate(response, *, ssrf_policy)`` helper that walks
``response.request.redirected_from`` to validate every redirect hop —
catches an attacker-controlled redirect that lands the agent on a private
IP after passing the initial pre-nav check.

The blocked-IP ranges replicate OpenClaw's defaults (private IPv4 RFC1918,
loopback, link-local, IPv6 unique-local + link-local). They can be
extended via ``SsrfPolicy.dangerously_allow_private_network`` (allow all
private nets) or via ``SsrfPolicy.allowed_hostnames`` exact-match
allowlist. We do NOT use OpenClaw's IPv4 numeric-literal "?"-pattern
"glob" — see BLUEPRINT §7.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from ..profiles.config import SsrfPolicy

_log = logging.getLogger("opencomputer.browser_control.session.nav_guard")

NAV_GUARD_BLOCKED_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {
        # Cloud metadata endpoints — common SSRF targets.
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "instance-data.ec2.internal",
    }
)

_ALLOWED_NAV_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})


# ─── exceptions ───────────────────────────────────────────────────────


class InvalidBrowserNavigationUrlError(Exception):
    """Raised for a URL whose scheme is not in the allow-list (and isn't about:blank)."""


class SsrfBlockedError(Exception):
    """Raised when a URL resolves to a blocked IP range or hostname."""


class NavigationBlocked(Exception):  # noqa: N818 — semantic name; aliased below
    """Raised by ``install_navigation_guard`` after a top-level block.

    Wraps the underlying ``InvalidBrowserNavigationUrlError`` /
    ``SsrfBlockedError`` so callers can distinguish "the goto raised
    because the guard fired" from "the goto raised for some other reason".
    """

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


def is_policy_deny_error(err: BaseException) -> bool:
    return isinstance(err, (InvalidBrowserNavigationUrlError, SsrfBlockedError, NavigationBlocked))


# ─── policy bundle ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NavigationGuardPolicy:
    """Effective navigation policy.

    - ``ssrf_policy``: from ResolvedBrowserConfig — controls private-network
      bypass, additional allowlists.
    - ``allow_about_blank``: always True for v0.1 (matches OpenClaw).
    - ``extra_blocked_hostnames``: union with NAV_GUARD_BLOCKED_HOSTNAMES.
    - ``resolver``: callable(host) -> list[str] of resolved IPs. Defaults to
      ``socket.getaddrinfo``; tests inject deterministic resolvers.
    """

    ssrf_policy: SsrfPolicy | None = None
    allow_about_blank: bool = True
    extra_blocked_hostnames: frozenset[str] = frozenset()
    resolver: Any = None  # callable[[str], list[str]] | None

    def all_blocked_hostnames(self) -> frozenset[str]:
        return NAV_GUARD_BLOCKED_HOSTNAMES | self.extra_blocked_hostnames


# ─── DNS resolution ───────────────────────────────────────────────────


async def _resolve_host(host: str, *, resolver: Any | None) -> list[str]:
    """Resolve `host` to a list of IP strings. Empty on failure (caller treats as block)."""
    if resolver is not None:
        try:
            result = resolver(host)
            if asyncio.iscoroutine(result):
                result = await result
            return list(result or [])
        except Exception:  # noqa: BLE001 — test resolver may raise
            return []
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if ip and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def _is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _hostname_is_explicitly_allowed(host: str, ssrf_policy: SsrfPolicy | None) -> bool:
    if ssrf_policy is None:
        return False
    h = host.lower()
    allowed = ssrf_policy.allowed_hostnames or []
    for entry in allowed:
        if isinstance(entry, str) and entry.lower() == h:
            return True
    allowlist = ssrf_policy.hostname_allowlist or []
    for entry in allowlist:
        if not isinstance(entry, str):
            continue
        # Substring contains; OpenClaw's "?"/glob is intentionally not implemented.
        if entry.lower() in h:
            return True
    return False


# ─── core gate ────────────────────────────────────────────────────────


async def assert_browser_navigation_allowed(
    url: str,
    *,
    policy: NavigationGuardPolicy | None = None,
    ssrf_policy: SsrfPolicy | None = None,
) -> None:
    """Validate ``url`` or raise.

    Raises:
      InvalidBrowserNavigationUrlError — bad scheme / unparseable url.
      SsrfBlockedError — host resolves to private/blocked range or hostname blocklist.
    """
    if policy is None:
        policy = NavigationGuardPolicy(ssrf_policy=ssrf_policy)

    if not url:
        raise InvalidBrowserNavigationUrlError("empty navigation URL")

    if url == "about:blank":
        if not policy.allow_about_blank:
            raise InvalidBrowserNavigationUrlError("about:blank not permitted by policy")
        return

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise InvalidBrowserNavigationUrlError(f"unparseable URL: {url!r}") from exc

    if parts.scheme not in _ALLOWED_NAV_SCHEMES:
        raise InvalidBrowserNavigationUrlError(
            f"scheme {parts.scheme!r} is not allowed; only http/https/about:blank"
        )

    host = (parts.hostname or "").lower()
    if not host:
        raise InvalidBrowserNavigationUrlError(f"missing hostname: {url!r}")

    if _hostname_is_explicitly_allowed(host, policy.ssrf_policy):
        return

    blocked_hosts = policy.all_blocked_hostnames()
    if host in blocked_hosts:
        raise SsrfBlockedError(f"hostname {host!r} is in the navigation block-list")

    # If the host is itself an IP literal, validate directly (skip DNS).
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None

    allow_private = bool(
        policy.ssrf_policy and policy.ssrf_policy.dangerously_allow_private_network
    )

    if addr is not None:
        if not allow_private and _is_private_ip(host):
            raise SsrfBlockedError(f"host IP {host!r} resolves to a private/loopback range")
        return

    resolved = await _resolve_host(host, resolver=policy.resolver)
    if not resolved:
        raise SsrfBlockedError(f"could not resolve host {host!r}")

    if not allow_private:
        for ip in resolved:
            if _is_private_ip(ip):
                raise SsrfBlockedError(
                    f"host {host!r} resolves to private/loopback IP {ip}"
                )


# ─── post-nav revalidation (redirect chain) ───────────────────────────


async def assert_navigation_result_allowed(
    response: Any,
    *,
    policy: NavigationGuardPolicy,
) -> None:
    """Walk the redirect chain on a Playwright Response.

    Validates each ``Request.url`` in ``response.request.redirected_from``
    chain plus the final URL. Catches a 30x sequence that ends on a
    private IP or an attacker-controlled host.
    """
    if response is None:
        return
    request = getattr(response, "request", None)
    if request is None:
        return

    chain: list[str] = []
    cur = request
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        url = getattr(cur, "url", None)
        if isinstance(url, str) and url:
            chain.append(url)
        prev = getattr(cur, "redirected_from", None)
        cur = prev

    # Walk earliest hop first so the first failure is reported as the
    # earliest violation — chain was built end→start, reverse it.
    for url in reversed(chain):
        if url == "about:blank":
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme.startswith("chrome-error"):
            continue
        await assert_browser_navigation_allowed(url, policy=policy)


# ─── route-level guard ────────────────────────────────────────────────


def _is_top_level_navigation_request(page: Any, request: Any) -> bool:
    main_frame = getattr(page, "main_frame", None)
    try:
        frame = getattr(request, "frame", None)
    except Exception:  # noqa: BLE001 — fail-closed: treat as top-level
        return True
    same_main_frame = (frame is main_frame) if (frame is not None and main_frame is not None) else True
    try:
        is_nav = request.is_navigation_request()
    except Exception:  # noqa: BLE001
        is_nav = False
    try:
        rt = getattr(request, "resource_type", None)
    except Exception:  # noqa: BLE001
        rt = None
    return same_main_frame and (is_nav or rt == "document")


def _is_subframe_document_navigation_request(page: Any, request: Any) -> bool:
    main_frame = getattr(page, "main_frame", None)
    try:
        frame = getattr(request, "frame", None)
    except Exception:  # noqa: BLE001 — fail-closed: run guard on it
        return True
    if frame is None or main_frame is None or frame is main_frame:
        return False
    try:
        is_nav = request.is_navigation_request()
    except Exception:  # noqa: BLE001
        is_nav = False
    try:
        rt = getattr(request, "resource_type", None)
    except Exception:  # noqa: BLE001
        rt = None
    return is_nav or rt == "document"


@dataclass(slots=True)
class _GuardState:
    """Mutable state carried alongside the route handler.

    ``blocked_error`` latches the first top-level guard failure so the
    outer ``goto`` caller can re-raise it after the route handler aborts
    (which would otherwise surface as a generic "page closed" error).
    """

    blocked_error: BaseException | None = None
    _handler: Any | None = None


async def install_navigation_guard(
    page: Any,
    *,
    policy: NavigationGuardPolicy | None = None,
    ssrf_policy: SsrfPolicy | None = None,
) -> _GuardState:
    """Install a ``page.route("**/*", handler)`` that gates navigations.

    Returns a ``_GuardState`` whose ``blocked_error`` will be set if a
    top-level navigation was denied. Caller is responsible for checking
    this after their ``page.goto`` returns and re-raising as a
    ``NavigationBlocked``.
    """
    if policy is None:
        policy = NavigationGuardPolicy(ssrf_policy=ssrf_policy)
    state = _GuardState()

    async def handler(route: Any) -> None:
        request = route.request

        # If we already latched a top-level block, abort EVERY subsequent
        # request in this nav (including subresources). Otherwise an
        # in-flight CSS / image fetch would race past the guard.
        if state.blocked_error is not None:
            try:
                await route.abort()
            except Exception:  # noqa: BLE001
                pass
            return

        # Subresources pass through unguarded.
        is_top = _is_top_level_navigation_request(page, request)
        is_subframe_doc = (not is_top) and _is_subframe_document_navigation_request(page, request)
        if not is_top and not is_subframe_doc:
            try:
                await route.continue_()
            except Exception:  # noqa: BLE001 — page may be tearing down
                pass
            return

        try:
            await assert_browser_navigation_allowed(request.url, policy=policy)
        except (InvalidBrowserNavigationUrlError, SsrfBlockedError) as exc:
            if is_top:
                state.blocked_error = exc
            try:
                await route.abort()
            except Exception:  # noqa: BLE001
                pass
            return
        except Exception:
            # A bug in the guard should not silently allow — re-raise so
            # tests catch it. Playwright will surface the error via the
            # route layer.
            raise

        try:
            await route.continue_()
        except Exception:  # noqa: BLE001 — page may have torn down mid-route
            pass

    await page.route("**/*", handler)
    state._handler = handler
    return state


async def uninstall_navigation_guard(page: Any, state: _GuardState) -> None:
    """Best-effort `page.unroute` for the handler we installed."""
    handler = state._handler
    if handler is None:
        return
    try:
        await page.unroute("**/*", handler)
    except Exception:  # noqa: BLE001
        pass
