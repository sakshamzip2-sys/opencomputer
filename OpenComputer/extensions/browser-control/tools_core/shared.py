"""Shared helpers for the act-kind workhorse.

Three categories:

  - Timeout clamps + constants — match OpenClaw's act-policy.ts ranges so
    the model sees identical bounds across implementations.
  - Ref/selector normalization — strip ``@``/``ref=``, error if both empty,
    prefer ref when both supplied.
  - ``assert_interaction_navigation_completed_safely`` — the 3-phase
    nav-guard observer. Phase 1: keep a ``framenavigated`` listener
    attached for the entire duration of ``action()``. Phase 2 (success):
    schedule a 250ms post-action observer for delayed navs. Phase 3
    (error): same delayed observer — a delayed SSRF block wins over the
    action error so the agent sees the *real* reason.

Plus ``to_ai_friendly_error`` which rewrites Playwright error strings
into messages an agent can act on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar
from urllib.parse import urlsplit

from ..session.nav_guard import (
    InvalidBrowserNavigationUrlError,
    NavigationGuardPolicy,
    SsrfBlockedError,
    assert_browser_navigation_allowed,
)
from ..profiles.config import SsrfPolicy

T = TypeVar("T")

# ─── act-policy constants (mirror act-policy.ts) ─────────────────────

ACT_MAX_BATCH_ACTIONS = 100
ACT_MAX_BATCH_DEPTH = 5
ACT_MAX_CLICK_DELAY_MS = 5_000
ACT_MAX_WAIT_TIME_MS = 30_000
ACT_MIN_TIMEOUT_MS = 500
ACT_MAX_INTERACTION_TIMEOUT_MS = 60_000
ACT_MAX_WAIT_TIMEOUT_MS = 120_000
ACT_DEFAULT_INTERACTION_TIMEOUT_MS = 8_000
ACT_DEFAULT_WAIT_TIMEOUT_MS = 20_000
ACT_DEFAULT_GENERIC_TIMEOUT_MS = 20_000

INTERACTION_NAVIGATION_GRACE_MS = 250


# ─── timeout helpers ─────────────────────────────────────────────────


def normalize_timeout_ms(
    value: int | float | None,
    *,
    fallback: int = ACT_DEFAULT_GENERIC_TIMEOUT_MS,
    min_ms: int = ACT_MIN_TIMEOUT_MS,
    max_ms: int = ACT_MAX_WAIT_TIMEOUT_MS,
) -> int:
    """Clamp to ``[min_ms, max_ms]``. ``None``/non-positive → fallback."""
    if value is None:
        candidate = fallback
    else:
        try:
            candidate = int(value)
        except (TypeError, ValueError):
            candidate = fallback
    if candidate <= 0:
        candidate = fallback
    if candidate < min_ms:
        return min_ms
    if candidate > max_ms:
        return max_ms
    return candidate


def clamp_interaction_timeout(value: int | float | None) -> int:
    return normalize_timeout_ms(
        value,
        fallback=ACT_DEFAULT_INTERACTION_TIMEOUT_MS,
        min_ms=ACT_MIN_TIMEOUT_MS,
        max_ms=ACT_MAX_INTERACTION_TIMEOUT_MS,
    )


def clamp_wait_timeout(value: int | float | None) -> int:
    return normalize_timeout_ms(
        value,
        fallback=ACT_DEFAULT_WAIT_TIMEOUT_MS,
        min_ms=ACT_MIN_TIMEOUT_MS,
        max_ms=ACT_MAX_WAIT_TIMEOUT_MS,
    )


def resolve_bounded_delay_ms(value: int | float | None, *, max_ms: int, label: str) -> int:
    if value is None:
        return 0
    try:
        candidate = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: must be an integer in milliseconds") from exc
    if candidate < 0:
        raise ValueError(f"{label}: must be >= 0")
    if candidate > max_ms:
        raise ValueError(f"{label}: must be <= {max_ms}")
    return candidate


# ─── ref/selector normalization ──────────────────────────────────────


def require_ref(value: Any) -> str:
    """Strip ``@``/``ref=`` prefix; throw on blank."""
    if value is None:
        raise ValueError("ref is required")
    if not isinstance(value, str):
        raise ValueError("ref must be a string")
    s = value.strip()
    if s.startswith("ref="):
        s = s[4:]
    if s.startswith("@"):
        s = s[1:]
    if not s:
        raise ValueError("ref is required")
    return s


def require_ref_or_selector(
    *, ref: str | None = None, selector: str | None = None
) -> dict[str, str]:
    """Trim both. Throw if both empty. ``ref`` wins when both provided."""
    r = (ref or "").strip()
    s = (selector or "").strip()
    if not r and not s:
        raise ValueError("ref or selector is required")
    if r:
        return {"ref": require_ref(r)}
    return {"selector": s}


# ─── error rewriter ──────────────────────────────────────────────────


def to_ai_friendly_error(err: BaseException, label: str | None = None) -> Exception:
    """Pattern-match Playwright error strings → agent-readable messages.

    Translation table (deep dive §1):
      - "strict mode violation" / "matched N elements" → "matched N
        elements; run a new snapshot before retrying"
      - "Timeout" + "element is not visible" / "waiting for selector" →
        "not found or not visible (selector=<label>)"
      - "intercepts pointer events" → "not interactable (hidden or covered)"
      - else: pass through with selector/ref label.
    """
    msg = str(err)
    lower = msg.lower()
    suffix = f" (target={label!r})" if label else ""

    if "strict mode violation" in lower or "matched " in lower and " elements" in lower:
        return RuntimeError(
            f"matched multiple elements; run a new snapshot before retrying{suffix}"
        )
    if "timeout" in lower and (
        "element is not visible" in lower
        or "waiting for selector" in lower
        or "to be visible" in lower
    ):
        return RuntimeError(f"not found or not visible{suffix}")
    if "intercepts pointer events" in lower or "pointer-events" in lower:
        return RuntimeError(f"not interactable (hidden or covered by another element){suffix}")
    return RuntimeError(f"{msg}{suffix}" if label else msg)


# ─── nav-guard observer ──────────────────────────────────────────────


def _did_cross_document_url_change(current: str, previous: str) -> bool:
    """True if the current URL differs from previous in origin/path/search.

    Hash-only changes do NOT count as cross-document. Same URL → False
    (no change at all).
    """
    if current == previous:
        return False
    try:
        cur = urlsplit(current)
        prev = urlsplit(previous)
    except ValueError:
        return current != previous
    return (
        cur.scheme != prev.scheme
        or cur.netloc != prev.netloc
        or cur.path != prev.path
        or cur.query != prev.query
    )


@dataclass(slots=True)
class _ObservedNavs:
    main_frame_navigated: str | None = None
    subframes: list[str] = field(default_factory=list)


def _is_main_frame_navigation(page: Any, frame: Any) -> bool:
    main = getattr(page, "main_frame", None)
    if frame is None or main is None:
        return True  # fail-open for tests with mock pages
    return frame is main


async def _observe_delayed_navigation(
    page: Any,
    *,
    grace_ms: int = INTERACTION_NAVIGATION_GRACE_MS,
) -> _ObservedNavs:
    """Listen for ``framenavigated`` for ``grace_ms`` and collect URLs."""
    out = _ObservedNavs()
    done = asyncio.Event()

    def listener(frame: Any) -> None:
        try:
            url = getattr(frame, "url", None)
            if not isinstance(url, str):
                return
            if _is_main_frame_navigation(page, frame):
                if out.main_frame_navigated is None:
                    out.main_frame_navigated = url
                    done.set()
            else:
                out.subframes.append(url)
        except Exception:
            pass

    on = getattr(page, "on", None)
    off = getattr(page, "remove_listener", None) or getattr(page, "off", None)
    if not callable(on):
        return out
    on("framenavigated", listener)
    try:
        try:
            await asyncio.wait_for(done.wait(), timeout=grace_ms / 1000.0)
        except asyncio.TimeoutError:
            pass
    finally:
        if callable(off):
            try:
                off("framenavigated", listener)
            except Exception:
                pass
    return out


def _build_policy(ssrf_policy: SsrfPolicy | None) -> NavigationGuardPolicy:
    return NavigationGuardPolicy(ssrf_policy=ssrf_policy)


async def _validate_navigations(
    observed: _ObservedNavs,
    *,
    policy: NavigationGuardPolicy,
) -> None:
    """Subframe error wins over main-frame error (deep dive gotcha #3).

    Walk subframes first; collect first failure. Then validate main-frame
    nav. If both fail, raise the subframe error.
    """
    subframe_err: BaseException | None = None
    for url in observed.subframes:
        if not url or url == "about:blank" or url.startswith("about:srcdoc"):
            continue
        try:
            await assert_browser_navigation_allowed(url, policy=policy)
        except (InvalidBrowserNavigationUrlError, SsrfBlockedError) as exc:
            subframe_err = exc
            break

    if observed.main_frame_navigated:
        try:
            await assert_browser_navigation_allowed(
                observed.main_frame_navigated, policy=policy
            )
        except (InvalidBrowserNavigationUrlError, SsrfBlockedError) as exc:
            if subframe_err is not None:
                raise subframe_err from exc
            raise

    if subframe_err is not None:
        raise subframe_err


async def assert_interaction_navigation_completed_safely(
    action: Callable[[], Awaitable[T]],
    *,
    page: Any,
    previous_url: str,
    ssrf_policy: SsrfPolicy | None = None,
    grace_ms: int = INTERACTION_NAVIGATION_GRACE_MS,
) -> T:
    """Run ``action()`` with a navigation listener attached.

    3-phase guard (deep dive §1.10 + gotcha #1):

      Phase 1 — During the action: keep a ``framenavigated`` listener
        attached. Collect main-frame and subframe nav URLs.
      Phase 2 — After the action succeeds: if any nav fired (or URL
        diff observed), validate. Otherwise schedule a 250ms delayed
        observer in case the action resolved before the nav event.
      Phase 3 — After the action raises: ALSO run the delayed observer.
        A delayed SSRF block wins over the action error.

    If ``ssrf_policy`` is None the wrap is a transparent passthrough —
    the action runs but no validation is performed. This matches OpenClaw's
    ``ssrfPolicy?: undefined → no-op`` semantics.
    """
    if ssrf_policy is None and not callable(getattr(page, "on", None)):
        return await action()

    policy = _build_policy(ssrf_policy)
    observed = _ObservedNavs()

    def listener(frame: Any) -> None:
        try:
            url = getattr(frame, "url", None)
            if not isinstance(url, str):
                return
            if _is_main_frame_navigation(page, frame):
                if observed.main_frame_navigated is None:
                    observed.main_frame_navigated = url
            else:
                observed.subframes.append(url)
        except Exception:
            pass

    on = getattr(page, "on", None)
    off = getattr(page, "remove_listener", None) or getattr(page, "off", None)
    listener_attached = False
    if callable(on):
        try:
            on("framenavigated", listener)
            listener_attached = True
        except Exception:
            pass

    action_error: BaseException | None = None
    result: Any = None
    try:
        result = await action()
    except BaseException as exc:
        action_error = exc
    finally:
        if listener_attached and callable(off):
            try:
                off("framenavigated", listener)
            except Exception:
                pass

    # Phase 2/3: post-action validation.
    if ssrf_policy is not None:
        navigated_during = (
            observed.main_frame_navigated is not None or bool(observed.subframes)
        )
        try:
            current_url = getattr(page, "url", "") or ""
        except Exception:
            current_url = ""

        if navigated_during or _did_cross_document_url_change(current_url, previous_url):
            try:
                await _validate_navigations(observed, policy=policy)
            except (InvalidBrowserNavigationUrlError, SsrfBlockedError) as exc:
                # SSRF wins over action error.
                raise exc from action_error
        else:
            # Schedule delayed observer (250ms grace).
            delayed = await _observe_delayed_navigation(page, grace_ms=grace_ms)
            if delayed.main_frame_navigated or delayed.subframes:
                try:
                    await _validate_navigations(delayed, policy=policy)
                except (InvalidBrowserNavigationUrlError, SsrfBlockedError) as exc:
                    raise exc from action_error

    if action_error is not None:
        raise action_error
    return result  # type: ignore[no-any-return]


# ─── small Playwright helpers ────────────────────────────────────────


async def scroll_into_view_best_effort(locator: Any, *, timeout_ms: int) -> None:
    """Run ``scrollIntoViewIfNeeded`` swallowing all errors.

    Used as a stabilization step before some interactions; missing
    scroll-into-view should not fail the action — Playwright's auto-wait
    catches the visibility issue instead.
    """
    try:
        await locator.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        pass
