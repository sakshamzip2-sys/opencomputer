"""Unit tests for ``tools_core.shared`` — timeout clamps, ref/selector
normalization, error rewriter, and the 3-phase nav-guard observer."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from extensions.browser_control.profiles.config import SsrfPolicy
from extensions.browser_control.tools_core.shared import (
    ACT_DEFAULT_INTERACTION_TIMEOUT_MS,
    ACT_MAX_INTERACTION_TIMEOUT_MS,
    ACT_MAX_WAIT_TIMEOUT_MS,
    ACT_MIN_TIMEOUT_MS,
    INTERACTION_NAVIGATION_GRACE_MS,
    assert_interaction_navigation_completed_safely,
    clamp_interaction_timeout,
    clamp_wait_timeout,
    normalize_timeout_ms,
    require_ref,
    require_ref_or_selector,
    resolve_bounded_delay_ms,
    to_ai_friendly_error,
)

# ─── timeout clamps ──────────────────────────────────────────────────


def test_normalize_timeout_clamps_high() -> None:
    assert normalize_timeout_ms(999_999) == ACT_MAX_WAIT_TIMEOUT_MS


def test_normalize_timeout_clamps_low() -> None:
    assert normalize_timeout_ms(10) == ACT_MIN_TIMEOUT_MS


def test_normalize_timeout_uses_fallback_on_none() -> None:
    assert normalize_timeout_ms(None, fallback=12_345) == 12_345


def test_normalize_timeout_uses_fallback_on_zero() -> None:
    assert normalize_timeout_ms(0, fallback=7777) == 7777


def test_clamp_interaction_timeout_default() -> None:
    assert clamp_interaction_timeout(None) == ACT_DEFAULT_INTERACTION_TIMEOUT_MS


def test_clamp_interaction_timeout_caps_at_60s() -> None:
    assert clamp_interaction_timeout(999_999) == ACT_MAX_INTERACTION_TIMEOUT_MS


def test_clamp_wait_timeout_caps_at_120s() -> None:
    assert clamp_wait_timeout(999_999) == ACT_MAX_WAIT_TIMEOUT_MS


def test_resolve_bounded_delay_zero_when_none() -> None:
    assert resolve_bounded_delay_ms(None, max_ms=5000, label="x") == 0


def test_resolve_bounded_delay_rejects_negative() -> None:
    with pytest.raises(ValueError):
        resolve_bounded_delay_ms(-1, max_ms=5000, label="x")


def test_resolve_bounded_delay_rejects_above_max() -> None:
    with pytest.raises(ValueError):
        resolve_bounded_delay_ms(10_000, max_ms=5000, label="x")


# ─── ref / selector ──────────────────────────────────────────────────


def test_require_ref_strips_at() -> None:
    assert require_ref("@e7") == "e7"


def test_require_ref_strips_ref_prefix() -> None:
    assert require_ref("ref=e7") == "e7"


def test_require_ref_rejects_blank() -> None:
    with pytest.raises(ValueError):
        require_ref("   ")


def test_require_ref_or_selector_ref_wins() -> None:
    out = require_ref_or_selector(ref="e7", selector=".foo")
    assert out == {"ref": "e7"}


def test_require_ref_or_selector_selector_when_no_ref() -> None:
    out = require_ref_or_selector(selector=".foo")
    assert out == {"selector": ".foo"}


def test_require_ref_or_selector_both_blank_raises() -> None:
    with pytest.raises(ValueError):
        require_ref_or_selector(ref=" ", selector=" ")


# ─── error rewriter ──────────────────────────────────────────────────


def test_to_ai_friendly_error_strict_mode() -> None:
    err = to_ai_friendly_error(Exception("strict mode violation: matched 3 elements"), "e1")
    assert "matched multiple elements" in str(err)
    assert "e1" in str(err)


def test_to_ai_friendly_error_pointer_intercept() -> None:
    err = to_ai_friendly_error(
        Exception("element intercepts pointer events"), "e2"
    )
    assert "not interactable" in str(err)


def test_to_ai_friendly_error_passthrough() -> None:
    err = to_ai_friendly_error(Exception("some random error"), None)
    assert "some random error" in str(err)


# ─── nav-guard 3-phase observer ──────────────────────────────────────


class _Frame:
    def __init__(self, url: str) -> None:
        self.url = url


class _MockPage:
    """Minimal ``Page`` shim: tracks event listeners + URL state."""

    def __init__(self, *, url: str = "about:blank") -> None:
        self.url = url
        self._listeners: dict[str, list[Any]] = {}
        self.main_frame = _Frame(url)

    def on(self, event: str, handler: Any) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        try:
            self._listeners.get(event, []).remove(handler)
        except ValueError:
            pass

    off = remove_listener

    def fire_framenavigated(self, url: str, *, sub_frame: bool = False) -> None:
        frame = _Frame(url) if sub_frame else self.main_frame
        if not sub_frame:
            self.main_frame = frame
            frame.url = url
            self.url = url
        for h in list(self._listeners.get("framenavigated", [])):
            h(frame)


@pytest.mark.asyncio
async def test_nav_guard_no_policy_passthrough() -> None:
    page = _MockPage()
    ran: list[bool] = []

    async def action() -> str:
        ran.append(True)
        return "ok"

    out = await assert_interaction_navigation_completed_safely(
        action, page=page, previous_url="about:blank", ssrf_policy=None
    )
    assert out == "ok"
    assert ran == [True]


@pytest.mark.asyncio
async def test_nav_guard_validates_during_action() -> None:
    """Listener must catch a framenavigated fired during the action."""
    page = _MockPage()
    policy = SsrfPolicy()  # default — private nets blocked

    async def action() -> None:
        # Simulate the click triggering a nav to a private IP.
        page.fire_framenavigated("http://10.0.0.1/secret")

    from extensions.browser_control.session.nav_guard import SsrfBlockedError

    with pytest.raises(SsrfBlockedError):
        await assert_interaction_navigation_completed_safely(
            action,
            page=page,
            previous_url="https://example.com/start",
            ssrf_policy=policy,
        )


@pytest.mark.asyncio
async def test_nav_guard_subframe_error_wins() -> None:
    """If both subframe and main-frame fire blocks, subframe error wins."""
    page = _MockPage()
    policy = SsrfPolicy()

    async def action() -> None:
        page.fire_framenavigated("http://192.168.0.1/iframe", sub_frame=True)
        page.fire_framenavigated("http://10.0.0.1/main")

    from extensions.browser_control.session.nav_guard import SsrfBlockedError

    with pytest.raises(SsrfBlockedError) as exc_info:
        await assert_interaction_navigation_completed_safely(
            action,
            page=page,
            previous_url="https://example.com/",
            ssrf_policy=policy,
        )
    # Subframe URL ("192.168.x") wins over main-frame ("10.0.0.1") per
    # deep dive gotcha #3.
    assert "192.168" in str(exc_info.value)


@pytest.mark.asyncio
async def test_nav_guard_phase3_delayed_observer_after_error() -> None:
    """If the action errors AND no nav was observed, the 250ms grace
    observer still runs — a delayed SSRF block wins over the action error."""
    page = _MockPage()
    policy = SsrfPolicy()

    async def action() -> None:
        async def _delayed_fire() -> None:
            await asyncio.sleep(0.05)
            page.fire_framenavigated("http://10.0.0.1/late")

        asyncio.create_task(_delayed_fire())
        raise RuntimeError("click failed for unrelated reason")

    from extensions.browser_control.session.nav_guard import SsrfBlockedError

    # The delayed nav observer (phase 3) must catch the SSRF and raise it
    # in preference to the action error.
    with pytest.raises(SsrfBlockedError):
        await assert_interaction_navigation_completed_safely(
            action,
            page=page,
            previous_url="about:blank",
            ssrf_policy=policy,
            grace_ms=300,
        )


@pytest.mark.asyncio
async def test_nav_guard_no_nav_lets_action_error_through() -> None:
    page = _MockPage()
    policy = SsrfPolicy()

    async def action() -> None:
        raise ValueError("real bug")

    with pytest.raises(ValueError, match="real bug"):
        await assert_interaction_navigation_completed_safely(
            action,
            page=page,
            previous_url="about:blank",
            ssrf_policy=policy,
            grace_ms=50,
        )


@pytest.mark.asyncio
async def test_nav_guard_passes_when_nav_is_allowed() -> None:
    page = _MockPage()
    policy = SsrfPolicy()

    async def action() -> str:
        page.fire_framenavigated("https://example.com/ok")
        return "good"

    # Default resolver will try DNS for example.com — for tests we override.
    from extensions.browser_control.session.nav_guard import (
        NavigationGuardPolicy,
        assert_browser_navigation_allowed,
    )

    # Patch the policy's resolver via a context-local subclass approach:
    # easiest is to monkeypatch _resolve_host. But the SsrfPolicy itself
    # accepts an allowed_hostnames pre-DNS shortcut.
    policy_with_allowlist = SsrfPolicy(allowed_hostnames=["example.com"])

    out = await assert_interaction_navigation_completed_safely(
        action,
        page=page,
        previous_url="about:blank",
        ssrf_policy=policy_with_allowlist,
    )
    assert out == "good"
    # Spot-check the helper used inside is the same one we just imported.
    assert callable(assert_browser_navigation_allowed)
    assert NavigationGuardPolicy is not None
