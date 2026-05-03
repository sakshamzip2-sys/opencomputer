"""Unit tests for `session/helpers.py` (Wave 1a).

Covers:
  - redact_cdp_url strips user:pass; falls back safely on garbage input
  - normalize_cdp_url strips trailing slash
  - normalize_cdp_http_base coerces ws→http and strips /devtools/browser/<id>
    and /cdp suffixes
  - target_key composition is reversible (composes with the documented `::` delimiter)
  - CdpTimeouts.for_profile clamps loopback HTTP timeout to 300ms by default
  - no_proxy_lease is reference-counted: nested loopback leases share env state;
    NO_PROXY restored only if our value is still in place after a single lease pair
"""

from __future__ import annotations

import asyncio
import os

import pytest
from extensions.browser_control.session import helpers as helpers_mod
from extensions.browser_control.session.helpers import (
    CDP_HTTP_REQUEST_TIMEOUT_MS,
    CDP_WS_HANDSHAKE_TIMEOUT_MS,
    PROFILE_HTTP_REACHABILITY_TIMEOUT_MS,
    CdpTimeouts,
    no_proxy_lease,
    normalize_cdp_http_base,
    normalize_cdp_url,
    redact_cdp_url,
    target_key,
)


def test_redact_strips_credentials() -> None:
    assert redact_cdp_url("http://user:pass@127.0.0.1:18800") == "http://127.0.0.1:18800"


def test_redact_passes_through_clean_url() -> None:
    assert redact_cdp_url("http://127.0.0.1:18800") == "http://127.0.0.1:18800"


def test_redact_handles_none() -> None:
    assert redact_cdp_url(None) is None


def test_redact_handles_garbage() -> None:
    assert redact_cdp_url("not a url at all") == "not a url at all"


def test_normalize_strips_trailing_slash() -> None:
    assert normalize_cdp_url("http://127.0.0.1:18800/") == "http://127.0.0.1:18800"
    assert normalize_cdp_url("http://127.0.0.1:18800") == "http://127.0.0.1:18800"


@pytest.mark.parametrize(
    "src,expected",
    [
        ("ws://127.0.0.1:18800", "http://127.0.0.1:18800"),
        ("wss://example.com:9000", "https://example.com:9000"),
        (
            "ws://127.0.0.1:18800/devtools/browser/abc-123",
            "http://127.0.0.1:18800",
        ),
        (
            "http://127.0.0.1:18800/cdp",
            "http://127.0.0.1:18800",
        ),
        (
            "http://127.0.0.1:18800/cdp/",
            "http://127.0.0.1:18800",
        ),
        (
            "http://user:pw@host.example:9000/devtools/browser/xx?t=1",
            "http://user:pw@host.example:9000?t=1",
        ),
    ],
)
def test_normalize_cdp_http_base(src: str, expected: str) -> None:
    assert normalize_cdp_http_base(src) == expected


def test_target_key_composes_with_delimiter() -> None:
    key = target_key("http://127.0.0.1:18800/", "T1")
    assert key == "http://127.0.0.1:18800::T1"
    # Prefix-scan invariant: filtering by "<normalized>::" enumerates a CDP URL.
    assert key.startswith("http://127.0.0.1:18800::")


def test_cdp_timeouts_loopback_clamps_short() -> None:
    t = CdpTimeouts.for_profile(is_loopback=True)
    assert t.http_timeout_ms == PROFILE_HTTP_REACHABILITY_TIMEOUT_MS
    # WS = max(200, min(2000, 2*300)) = 600.
    assert t.ws_handshake_timeout_ms == 600


def test_cdp_timeouts_remote_uses_caller_floor() -> None:
    t = CdpTimeouts.for_profile(
        is_loopback=False,
        remote_http_timeout_ms=CDP_HTTP_REQUEST_TIMEOUT_MS,
        remote_handshake_timeout_ms=CDP_WS_HANDSHAKE_TIMEOUT_MS,
        override_http_ms=4000,
    )
    assert t.http_timeout_ms == 4000
    # WS = max(remote_handshake, http*2) = max(5000, 8000) = 8000.
    assert t.ws_handshake_timeout_ms == 8000


# ─── no_proxy_lease ───────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_lease_state(monkeypatch) -> None:
    """Each test starts with a clean global lease + a NO_PROXY snapshot."""
    helpers_mod._lease.count = 0
    helpers_mod._lease.snapshot = None
    helpers_mod._lease.applied = None
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


@pytest.mark.asyncio
async def test_no_proxy_lease_no_op_for_non_loopback() -> None:
    async with no_proxy_lease("http://example.com") as acquired:
        assert acquired is False
        assert os.environ.get("NO_PROXY") is None


@pytest.mark.asyncio
async def test_no_proxy_lease_sets_and_restores_for_loopback() -> None:
    assert os.environ.get("NO_PROXY") is None
    async with no_proxy_lease("http://127.0.0.1:18800") as acquired:
        assert acquired is True
        cur = os.environ.get("NO_PROXY")
        assert cur is not None and "127.0.0.1" in cur and "localhost" in cur
    # Restored.
    assert os.environ.get("NO_PROXY") is None


@pytest.mark.asyncio
async def test_no_proxy_lease_reference_counted() -> None:
    """Two concurrent leases share the same applied snapshot; restore happens once."""
    enter_inner = asyncio.Event()
    exit_outer = asyncio.Event()

    async def outer() -> None:
        async with no_proxy_lease("http://127.0.0.1:18800"):
            enter_inner.set()
            await exit_outer.wait()

    async def inner() -> None:
        await enter_inner.wait()
        async with no_proxy_lease("http://127.0.0.1:18800"):
            assert os.environ.get("NO_PROXY") is not None
        # After inner releases, NO_PROXY should still be set (outer holds it).
        assert os.environ.get("NO_PROXY") is not None
        exit_outer.set()

    await asyncio.gather(outer(), inner())
    # After both released — env restored.
    assert os.environ.get("NO_PROXY") is None


@pytest.mark.asyncio
async def test_no_proxy_lease_external_mutation_preserved() -> None:
    """If user mutates NO_PROXY mid-flight, the lease leaves their value alone."""

    async with no_proxy_lease("http://127.0.0.1:18800"):
        # Simulate external mutation.
        os.environ["NO_PROXY"] = "user-set-value.com"
    # The lease saw "user-set-value.com" not match what it applied, so it
    # didn't restore — user's value survives.
    assert os.environ["NO_PROXY"] == "user-set-value.com"
