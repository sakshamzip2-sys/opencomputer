"""Anthropic client construction — single source of truth.

Every call site in OpenComputer that talks to Anthropic should build its
``Anthropic`` / ``AsyncAnthropic`` client through one of the two helpers
in this module. Before this module existed, each call site rolled its
own kwarg-building, which led to silent drift: the chat provider got
bearer-mode + base_url support, while the batch path, the ``/btw``
slash command, the profile bootstrap, and the screenshot tool's vision
sub-call did not — so users with claude-router-style proxies hit 401s
on every path except chat.

Resolved from env:

* ``ANTHROPIC_BASE_URL`` — endpoint to hit (default: direct Anthropic).
  Set this to your proxy's URL (e.g. ``https://claude-router.vercel.app``)
  and every call site routes through it.
* ``ANTHROPIC_AUTH_MODE`` — ``"x-api-key"`` / ``"api_key"`` (default,
  Anthropic native) or ``"bearer"`` (``Authorization: Bearer <key>``).
  In bearer mode we ALSO strip the SDK's auto-added ``x-api-key``
  header on the way out via an httpx event hook — some proxies forward
  ``x-api-key`` verbatim to upstream Anthropic, which then rejects the
  proxy bearer as an invalid Anthropic key.

Callers can override either env value at call time via the
``base_url=`` / ``auth_mode=`` kwargs (used by the chat provider, which
freezes the values at construction time so concurrent env mutations
don't change a live session's auth shape).
"""

from __future__ import annotations

import os

import httpx
from anthropic import Anthropic, AsyncAnthropic

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_CONNECT_TIMEOUT_S = 10.0


async def _strip_x_api_key_async(request: httpx.Request) -> None:
    """httpx event hook — remove ``x-api-key`` before sending in bearer mode.

    The Anthropic SDK auto-adds ``x-api-key`` from the constructor's
    ``api_key`` kwarg. Some proxies forward that header verbatim to
    upstream Anthropic, which then rejects the proxy bearer as an
    invalid Anthropic key. This hook runs at the last moment so the
    SDK's own auth path stays undisturbed.
    """
    request.headers.pop("x-api-key", None)


def _strip_x_api_key_sync(request: httpx.Request) -> None:
    """Sync variant of the bearer-mode header strip — used by ``Anthropic``."""
    request.headers.pop("x-api-key", None)


def _resolve_auth_mode(auth_mode: str | None) -> str:
    """Normalize an auth_mode value (caller-provided OR env-derived).

    ``api_key`` and ``x-api-key`` are aliases — same wire behavior. The
    third option is ``bearer`` for proxies that need
    ``Authorization: Bearer`` + no ``x-api-key``.

    Unknown values fall back to ``api_key`` rather than raising — so a
    typo in env doesn't kill every Anthropic-using subsystem at once.
    Callers that want strictness should validate before calling.
    """
    mode = (auth_mode or os.environ.get("ANTHROPIC_AUTH_MODE") or "api_key").strip().lower()
    if mode not in ("x-api-key", "api_key", "bearer"):
        mode = "api_key"
    return mode


def _resolve_base_url(base_url: str | None) -> str | None:
    """Return the explicit override, or the env var, or None for the SDK default."""
    if base_url:
        return base_url
    env = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    return env or None


def build_anthropic_async_client(
    api_key: str,
    *,
    base_url: str | None = None,
    auth_mode: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
) -> AsyncAnthropic:
    """Build an ``AsyncAnthropic`` honouring proxy + bearer config.

    All call sites that talk to Anthropic asynchronously should use this
    instead of constructing ``AsyncAnthropic`` directly — that ensures
    a uniform auth path across chat, batch, vision, slash commands, etc.
    """
    kwargs: dict[str, object] = {"api_key": api_key}
    base = _resolve_base_url(base_url)
    if base:
        kwargs["base_url"] = base
    mode = _resolve_auth_mode(auth_mode)
    if mode == "bearer":
        kwargs["default_headers"] = {"Authorization": f"Bearer {api_key}"}
        kwargs["http_client"] = httpx.AsyncClient(
            event_hooks={"request": [_strip_x_api_key_async]},
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
        )
    return AsyncAnthropic(**kwargs)


def build_anthropic_sync_client(
    api_key: str,
    *,
    base_url: str | None = None,
    auth_mode: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
) -> Anthropic:
    """Sync counterpart of :func:`build_anthropic_async_client`.

    Used by code that has to stay sync (e.g. profile-bootstrap's artifact
    extractor, ``Anthropic.messages.batches.*`` retrieval helpers if any
    are invoked from sync contexts).
    """
    kwargs: dict[str, object] = {"api_key": api_key}
    base = _resolve_base_url(base_url)
    if base:
        kwargs["base_url"] = base
    mode = _resolve_auth_mode(auth_mode)
    if mode == "bearer":
        kwargs["default_headers"] = {"Authorization": f"Bearer {api_key}"}
        kwargs["http_client"] = httpx.Client(
            event_hooks={"request": [_strip_x_api_key_sync]},
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
        )
    return Anthropic(**kwargs)


__all__ = [
    "build_anthropic_async_client",
    "build_anthropic_sync_client",
]
