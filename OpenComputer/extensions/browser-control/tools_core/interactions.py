"""Per-act-kind dispatch — the workhorse.

``execute_single_action(page, kind, params, *, ssrf_policy, ref_resolver, evaluate_enabled)``
switches over the 11 kinds and runs the right Playwright API. Each
mutating kind (click, type-submit, press, evaluate) is wrapped in
``assert_interaction_navigation_completed_safely`` so a navigation that
fires mid-action is re-validated against the SSRF policy.

``ref_resolver`` is a callable ``(ref: str) -> Locator`` injected by the
caller — typically ``functools.partial(ref_locator, page,
cache_entry=session.get_role_refs(target_id))``. Keeps this module
decoupled from server_context / playwright_session.

Wrap status (deep dive §"Wrap status reference"):

  click:        YES (full action)
  type body:    NO; type+submit press: YES (Enter only)
  press:        YES
  hover:        NO
  drag:         NO
  select:       NO
  fill:         NO
  scrollIntoView: NO (kind="wait" sub-step path)
  wait:         NO
  evaluate:     YES
  close:        NO
  resize:       NO

Returns a dict shaped per-kind:

  evaluate → {"result": <js value>}
  every other kind → {} (action-only, no return value)
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..profiles.config import SsrfPolicy
from .shared import (
    ACT_MAX_CLICK_DELAY_MS,
    ACT_MAX_WAIT_TIME_MS,
    assert_interaction_navigation_completed_safely,
    clamp_interaction_timeout,
    clamp_wait_timeout,
    normalize_timeout_ms,
    require_ref,
    require_ref_or_selector,
    resolve_bounded_delay_ms,
    to_ai_friendly_error,
)


class EvaluateDisabledError(RuntimeError):
    """The agent tried to call ``evaluate`` while ``evaluate_enabled`` is False."""


# ─── locator builder ─────────────────────────────────────────────────


def _build_locator(
    page: Any,
    *,
    ref: str | None,
    selector: str | None,
    ref_resolver: Callable[[str], Any] | None,
) -> Any:
    """Resolve the resolved-ref-or-selector pair into a Playwright ``Locator``."""
    resolved = require_ref_or_selector(ref=ref, selector=selector)
    if "ref" in resolved:
        if ref_resolver is None:
            raise RuntimeError(
                "ref provided but no ref_resolver was supplied — caller must wire one"
            )
        return ref_resolver(resolved["ref"])
    return page.locator(resolved["selector"])


# ─── per-kind helpers ─────────────────────────────────────────────────


async def click_action(
    page: Any,
    params: dict[str, Any],
    *,
    ref_resolver: Callable[[str], Any] | None,
    ssrf_policy: SsrfPolicy | None,
) -> dict[str, Any]:
    locator = _build_locator(
        page,
        ref=params.get("ref"),
        selector=params.get("selector"),
        ref_resolver=ref_resolver,
    )
    timeout = clamp_interaction_timeout(params.get("timeout_ms"))
    delay = resolve_bounded_delay_ms(
        params.get("delay_ms"), max_ms=ACT_MAX_CLICK_DELAY_MS, label="delay_ms"
    )
    button = params.get("button")
    modifiers = params.get("modifiers")
    double_click = bool(params.get("double_click"))
    previous_url = getattr(page, "url", "") or ""

    label = params.get("ref") or params.get("selector")

    async def _do() -> None:
        try:
            if delay:
                await locator.hover(timeout=timeout)
                await asyncio.sleep(delay / 1000.0)
            if double_click:
                await locator.dblclick(
                    timeout=timeout, button=button, modifiers=modifiers
                )
            else:
                await locator.click(
                    timeout=timeout, button=button, modifiers=modifiers
                )
        except Exception as exc:
            raise to_ai_friendly_error(exc, label) from exc

    await assert_interaction_navigation_completed_safely(
        _do, page=page, previous_url=previous_url, ssrf_policy=ssrf_policy
    )
    return {}


async def type_action(
    page: Any,
    params: dict[str, Any],
    *,
    ref_resolver: Callable[[str], Any] | None,
    ssrf_policy: SsrfPolicy | None,
) -> dict[str, Any]:
    locator = _build_locator(
        page,
        ref=params.get("ref"),
        selector=params.get("selector"),
        ref_resolver=ref_resolver,
    )
    text = params.get("text")
    if not isinstance(text, str):
        raise ValueError("type: text is required")
    timeout = clamp_interaction_timeout(params.get("timeout_ms"))
    slowly = bool(params.get("slowly"))
    submit = bool(params.get("submit"))
    label = params.get("ref") or params.get("selector")

    try:
        if slowly:
            await locator.click(timeout=timeout)
            press_seq = getattr(locator, "press_sequentially", None)
            if callable(press_seq):
                await press_seq(text, delay=75, timeout=timeout)
            else:
                await locator.type(text, delay=75, timeout=timeout)
        else:
            await locator.fill(text, timeout=timeout)
    except Exception as exc:
        raise to_ai_friendly_error(exc, label) from exc

    if submit:
        previous_url = getattr(page, "url", "") or ""

        async def _submit() -> None:
            try:
                await locator.press("Enter", timeout=timeout)
            except Exception as exc:
                raise to_ai_friendly_error(exc, label) from exc

        await assert_interaction_navigation_completed_safely(
            _submit, page=page, previous_url=previous_url, ssrf_policy=ssrf_policy
        )
    return {}


async def press_action(
    page: Any, params: dict[str, Any], *, ssrf_policy: SsrfPolicy | None
) -> dict[str, Any]:
    key = params.get("key")
    if not isinstance(key, str) or not key:
        raise ValueError("press: key is required")
    delay_ms = resolve_bounded_delay_ms(
        params.get("delay_ms"), max_ms=ACT_MAX_CLICK_DELAY_MS, label="delay_ms"
    )
    previous_url = getattr(page, "url", "") or ""

    async def _do() -> None:
        await page.keyboard.press(key, delay=delay_ms)

    await assert_interaction_navigation_completed_safely(
        _do, page=page, previous_url=previous_url, ssrf_policy=ssrf_policy
    )
    return {}


async def hover_action(
    page: Any,
    params: dict[str, Any],
    *,
    ref_resolver: Callable[[str], Any] | None,
) -> dict[str, Any]:
    locator = _build_locator(
        page,
        ref=params.get("ref"),
        selector=params.get("selector"),
        ref_resolver=ref_resolver,
    )
    timeout = clamp_interaction_timeout(params.get("timeout_ms"))
    label = params.get("ref") or params.get("selector")
    try:
        await locator.hover(timeout=timeout)
    except Exception as exc:
        raise to_ai_friendly_error(exc, label) from exc
    return {}


async def drag_action(
    page: Any,
    params: dict[str, Any],
    *,
    ref_resolver: Callable[[str], Any] | None,
) -> dict[str, Any]:
    start = _build_locator(
        page,
        ref=params.get("start_ref"),
        selector=params.get("start_selector"),
        ref_resolver=ref_resolver,
    )
    end = _build_locator(
        page,
        ref=params.get("end_ref"),
        selector=params.get("end_selector"),
        ref_resolver=ref_resolver,
    )
    timeout = clamp_interaction_timeout(params.get("timeout_ms"))
    label = (
        f"{params.get('start_ref') or params.get('start_selector')!r} -> "
        f"{params.get('end_ref') or params.get('end_selector')!r}"
    )
    try:
        await start.drag_to(end, timeout=timeout)
    except Exception as exc:
        raise to_ai_friendly_error(exc, label) from exc
    return {}


async def select_action(
    page: Any,
    params: dict[str, Any],
    *,
    ref_resolver: Callable[[str], Any] | None,
) -> dict[str, Any]:
    locator = _build_locator(
        page,
        ref=params.get("ref"),
        selector=params.get("selector"),
        ref_resolver=ref_resolver,
    )
    values = params.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError("select: values must be a non-empty list")
    timeout = clamp_interaction_timeout(params.get("timeout_ms"))
    label = params.get("ref") or params.get("selector")
    try:
        await locator.select_option(values, timeout=timeout)
    except Exception as exc:
        raise to_ai_friendly_error(exc, label) from exc
    return {}


async def fill_action(
    page: Any,
    params: dict[str, Any],
    *,
    ref_resolver: Callable[[str], Any] | None,
) -> dict[str, Any]:
    """Multi-field form fill.

    ``params["fields"]`` is a list of ``{ref, value, type?}`` dicts.
    Empty ``ref`` skips the field silently (deep dive gotcha #10).
    """
    fields = params.get("fields")
    if not isinstance(fields, list):
        raise ValueError("fill: fields must be a list")
    timeout = clamp_interaction_timeout(params.get("timeout_ms"))

    for field in fields:
        if not isinstance(field, dict):
            raise ValueError("fill: each field must be a dict")
        ref = (field.get("ref") or "").strip()
        if not ref:
            continue  # silent skip
        if ref_resolver is None:
            raise RuntimeError("fill: no ref_resolver supplied")
        ftype = (field.get("type") or "text").lower()
        value = field.get("value", "")
        locator = ref_resolver(ref)
        try:
            if ftype in ("checkbox", "radio"):
                checked = value if isinstance(value, bool) else str(value).lower() in (
                    "true",
                    "1",
                    "on",
                    "yes",
                )
                await locator.set_checked(checked, timeout=timeout)
            else:
                await locator.fill(str(value), timeout=timeout)
        except Exception as exc:
            raise to_ai_friendly_error(exc, ref) from exc
    return {}


async def wait_action(page: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Run each present condition in fixed order.

    Sequence (deep dive §wait): timeMs → text → textGone → selector →
    url → loadState → fn. Each step is independent (NOT short-circuit).
    """
    timeout = clamp_wait_timeout(params.get("timeout_ms"))

    if params.get("time_ms") is not None:
        time_ms = resolve_bounded_delay_ms(
            params.get("time_ms"), max_ms=ACT_MAX_WAIT_TIME_MS, label="time_ms"
        )
        await page.wait_for_timeout(time_ms)
    if params.get("text"):
        await page.get_by_text(params["text"]).first.wait_for(
            state="visible", timeout=timeout
        )
    if params.get("text_gone"):
        await page.get_by_text(params["text_gone"]).first.wait_for(
            state="hidden", timeout=timeout
        )
    if params.get("selector"):
        await page.locator(params["selector"]).first.wait_for(
            state="visible", timeout=timeout
        )
    if params.get("url"):
        await page.wait_for_url(params["url"], timeout=timeout)
    if params.get("load_state"):
        await page.wait_for_load_state(params["load_state"], timeout=timeout)
    if params.get("fn"):
        await page.wait_for_function(params["fn"], timeout=timeout)
    return {}


async def evaluate_action(
    page: Any,
    params: dict[str, Any],
    *,
    ref_resolver: Callable[[str], Any] | None,
    ssrf_policy: SsrfPolicy | None,
    evaluate_enabled: bool,
) -> dict[str, Any]:
    if not evaluate_enabled:
        raise EvaluateDisabledError("evaluate is disabled by configuration")
    fn_text = params.get("fn") or params.get("expression")
    if not isinstance(fn_text, str) or not fn_text.strip():
        raise ValueError("evaluate: fn (JS source) is required")
    outer_timeout = normalize_timeout_ms(params.get("timeout_ms"), fallback=20_000)
    inner_timeout = max(1_000, outer_timeout - 500)

    # Wrap the user's JS in a Promise.race against a setTimeout-rejector
    # so async evaluations honor the timeout in-browser.
    wrapped = (
        "((args) => { "
        f"const __candidate = ({fn_text}); "
        "const __r = (typeof __candidate === 'function') "
        "? __candidate(args && args.el) : __candidate; "
        "if (__r && typeof __r.then === 'function') { "
        "return Promise.race([__r, new Promise((_, rej) => "
        f"setTimeout(() => rej(new Error('evaluate timed out after {inner_timeout}ms')), {inner_timeout}))]); "
        "} return __r; })"
    )

    ref = params.get("ref")
    previous_url = getattr(page, "url", "") or ""

    async def _do() -> Any:
        if ref:
            if ref_resolver is None:
                raise RuntimeError("evaluate: ref provided but no ref_resolver")
            normalized = require_ref(ref)
            locator = ref_resolver(normalized)
            return await locator.evaluate(wrapped)
        return await page.evaluate(wrapped)

    # Outer asyncio timeout matches `outer_timeout` so that even if the
    # in-browser timer is suppressed, Python doesn't hang.
    async def _run() -> Any:
        return await asyncio.wait_for(_do(), timeout=outer_timeout / 1000.0)

    result = await assert_interaction_navigation_completed_safely(
        _run, page=page, previous_url=previous_url, ssrf_policy=ssrf_policy
    )
    return {"result": result}


async def close_action(page: Any, params: dict[str, Any]) -> dict[str, Any]:
    await page.close()
    return {}


async def resize_action(page: Any, params: dict[str, Any]) -> dict[str, Any]:
    width = params.get("width")
    height = params.get("height")
    if width is None or height is None:
        raise ValueError("resize: width and height are required")
    w = max(1, int(float(width)))
    h = max(1, int(float(height)))
    await page.set_viewport_size({"width": w, "height": h})
    return {}


# ─── dispatch ────────────────────────────────────────────────────────


_ACT_KINDS = (
    "click",
    "type",
    "press",
    "hover",
    "drag",
    "select",
    "fill",
    "wait",
    "evaluate",
    "close",
    "resize",
)


def is_act_kind(kind: Any) -> bool:
    return isinstance(kind, str) and kind in _ACT_KINDS


def supported_act_kinds() -> tuple[str, ...]:
    return _ACT_KINDS


async def execute_single_action(
    page: Any,
    kind: str,
    params: dict[str, Any] | None = None,
    *,
    ref_resolver: Callable[[str], Any] | None = None,
    ssrf_policy: SsrfPolicy | None = None,
    evaluate_enabled: bool = True,
) -> dict[str, Any]:
    """Dispatch ``kind`` to the right per-kind helper.

    Raises:
      ValueError — unknown kind, missing required params.
      EvaluateDisabledError — evaluate when not allowed.
      Other exceptions — Playwright surface (rewritten by ``to_ai_friendly_error``).
    """
    p = params or {}
    if kind == "click":
        return await click_action(
            page, p, ref_resolver=ref_resolver, ssrf_policy=ssrf_policy
        )
    if kind == "type":
        return await type_action(
            page, p, ref_resolver=ref_resolver, ssrf_policy=ssrf_policy
        )
    if kind == "press":
        return await press_action(page, p, ssrf_policy=ssrf_policy)
    if kind == "hover":
        return await hover_action(page, p, ref_resolver=ref_resolver)
    if kind == "drag":
        return await drag_action(page, p, ref_resolver=ref_resolver)
    if kind == "select":
        return await select_action(page, p, ref_resolver=ref_resolver)
    if kind == "fill":
        return await fill_action(page, p, ref_resolver=ref_resolver)
    if kind == "wait":
        return await wait_action(page, p)
    if kind == "evaluate":
        return await evaluate_action(
            page,
            p,
            ref_resolver=ref_resolver,
            ssrf_policy=ssrf_policy,
            evaluate_enabled=evaluate_enabled,
        )
    if kind == "close":
        return await close_action(page, p)
    if kind == "resize":
        return await resize_action(page, p)
    raise ValueError(f"unknown act kind: {kind!r}")
