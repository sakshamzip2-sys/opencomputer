"""Unit tests for ``tools_core.interactions.execute_single_action``.

The mocks model the smallest possible Locator/Page surface that the
per-kind helpers exercise. No real Playwright is used — these tests
verify the dispatch logic and per-kind plumbing, not Playwright itself.
"""

from __future__ import annotations

from typing import Any

import pytest
from extensions.browser_control.tools_core.interactions import (
    EvaluateDisabledError,
    execute_single_action,
    is_act_kind,
    supported_act_kinds,
)


class _Locator:
    def __init__(self, label: str = "loc") -> None:
        self.label = label
        self.calls: list[tuple[str, Any]] = []

    async def click(self, **kw: Any) -> None:
        self.calls.append(("click", kw))

    async def dblclick(self, **kw: Any) -> None:
        self.calls.append(("dblclick", kw))

    async def hover(self, **kw: Any) -> None:
        self.calls.append(("hover", kw))

    async def fill(self, value: str, **kw: Any) -> None:
        self.calls.append(("fill", (value, kw)))

    async def press(self, key: str, **kw: Any) -> None:
        self.calls.append(("press", (key, kw)))

    async def type(self, text: str, **kw: Any) -> None:
        self.calls.append(("type", (text, kw)))

    async def press_sequentially(self, text: str, **kw: Any) -> None:
        self.calls.append(("press_sequentially", (text, kw)))

    async def select_option(self, values: list[str], **kw: Any) -> None:
        self.calls.append(("select_option", (values, kw)))

    async def set_checked(self, checked: bool, **kw: Any) -> None:
        self.calls.append(("set_checked", (checked, kw)))

    async def drag_to(self, other: _Locator, **kw: Any) -> None:
        self.calls.append(("drag_to", (other.label, kw)))

    async def evaluate(self, fn: str, *args: Any) -> Any:
        self.calls.append(("evaluate", (fn, args)))
        return "ref-result"


class _Keyboard:
    def __init__(self) -> None:
        self.pressed: list[tuple[str, dict[str, Any]]] = []

    async def press(self, key: str, **kw: Any) -> None:
        self.pressed.append((key, kw))


class _Page:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Any]] = {}
        self.keyboard = _Keyboard()
        self.url = "https://start.example/"
        self.viewport: dict[str, int] | None = None
        self.closed = False
        self.evals: list[str] = []
        self.waited_for: list[tuple[str, Any]] = []
        self.locator_calls: list[str] = []

    def on(self, event: str, h: Any) -> None:
        self._listeners.setdefault(event, []).append(h)

    def remove_listener(self, event: str, h: Any) -> None:
        try:
            self._listeners[event].remove(h)
        except (KeyError, ValueError):
            pass

    off = remove_listener
    main_frame = property(lambda self: None)

    def locator(self, sel: str) -> _Locator:
        self.locator_calls.append(sel)
        return _Locator(sel)

    def get_by_text(self, t: str) -> _TextRef:
        return _TextRef(t)

    async def wait_for_timeout(self, ms: int) -> None:
        self.waited_for.append(("timeout", ms))

    async def wait_for_url(self, url: str, **kw: Any) -> None:
        self.waited_for.append(("url", url))

    async def wait_for_load_state(self, state: str, **kw: Any) -> None:
        self.waited_for.append(("load_state", state))

    async def wait_for_function(self, fn: str, **kw: Any) -> None:
        self.waited_for.append(("function", fn))

    async def evaluate(self, fn: str, *args: Any) -> Any:
        self.evals.append(fn)
        return "page-result"

    async def close(self) -> None:
        self.closed = True

    async def set_viewport_size(self, viewport: dict[str, int]) -> None:
        self.viewport = viewport


class _TextRef:
    def __init__(self, t: str) -> None:
        self.t = t
        self.first = self

    async def wait_for(self, **kw: Any) -> None:
        # state="visible" or "hidden"
        return None


# ─── per-kind tests ──────────────────────────────────────────────────


def test_supported_kinds_match_brief() -> None:
    assert set(supported_act_kinds()) == {
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
    }
    assert len(supported_act_kinds()) == 11


def test_is_act_kind_check() -> None:
    assert is_act_kind("click")
    assert not is_act_kind("clack")
    assert not is_act_kind(7)


@pytest.mark.asyncio
async def test_click_via_selector() -> None:
    page = _Page()
    out = await execute_single_action(page, "click", {"selector": ".btn"})
    assert out == {}
    assert page.locator_calls == [".btn"]


@pytest.mark.asyncio
async def test_click_via_ref() -> None:
    page = _Page()
    fake = _Locator("ref-loc")
    await execute_single_action(
        page, "click", {"ref": "e1"}, ref_resolver=lambda r: fake
    )
    assert any(c[0] == "click" for c in fake.calls)


@pytest.mark.asyncio
async def test_click_double() -> None:
    page = _Page()
    fake = _Locator("e2")
    await execute_single_action(
        page,
        "click",
        {"ref": "e2", "double_click": True},
        ref_resolver=lambda r: fake,
    )
    assert any(c[0] == "dblclick" for c in fake.calls)


@pytest.mark.asyncio
async def test_type_fill_no_submit() -> None:
    page = _Page()
    fake = _Locator("input")
    await execute_single_action(
        page,
        "type",
        {"ref": "e3", "text": "hello"},
        ref_resolver=lambda r: fake,
    )
    fill_calls = [c for c in fake.calls if c[0] == "fill"]
    assert fill_calls and fill_calls[0][1][0] == "hello"


@pytest.mark.asyncio
async def test_type_with_submit_presses_enter() -> None:
    page = _Page()
    fake = _Locator("input")
    await execute_single_action(
        page,
        "type",
        {"ref": "e3", "text": "hi", "submit": True},
        ref_resolver=lambda r: fake,
    )
    presses = [c for c in fake.calls if c[0] == "press"]
    assert any(p[1][0] == "Enter" for p in presses)


@pytest.mark.asyncio
async def test_type_slowly_uses_press_sequentially() -> None:
    page = _Page()
    fake = _Locator("input")
    await execute_single_action(
        page,
        "type",
        {"ref": "e3", "text": "abc", "slowly": True},
        ref_resolver=lambda r: fake,
    )
    seqs = [c for c in fake.calls if c[0] == "press_sequentially"]
    assert seqs, "slowly=True should call press_sequentially"


@pytest.mark.asyncio
async def test_press_uses_page_keyboard() -> None:
    page = _Page()
    await execute_single_action(page, "press", {"key": "Enter"})
    assert page.keyboard.pressed == [("Enter", {"delay": 0})]


@pytest.mark.asyncio
async def test_hover_no_nav_wrap() -> None:
    page = _Page()
    fake = _Locator("h")
    await execute_single_action(
        page, "hover", {"ref": "e4"}, ref_resolver=lambda r: fake
    )
    assert any(c[0] == "hover" for c in fake.calls)


@pytest.mark.asyncio
async def test_drag_calls_drag_to() -> None:
    page = _Page()
    a, b = _Locator("a"), _Locator("b")
    refs = {"a1": a, "b1": b}
    await execute_single_action(
        page,
        "drag",
        {"start_ref": "a1", "end_ref": "b1"},
        ref_resolver=lambda r: refs[r],
    )
    assert any(c[0] == "drag_to" and c[1][0] == "b" for c in a.calls)


@pytest.mark.asyncio
async def test_select_requires_values() -> None:
    page = _Page()
    fake = _Locator("sel")
    with pytest.raises(ValueError):
        await execute_single_action(
            page,
            "select",
            {"ref": "e5", "values": []},
            ref_resolver=lambda r: fake,
        )


@pytest.mark.asyncio
async def test_select_passes_values() -> None:
    page = _Page()
    fake = _Locator("sel")
    await execute_single_action(
        page,
        "select",
        {"ref": "e5", "values": ["a", "b"]},
        ref_resolver=lambda r: fake,
    )
    sel = [c for c in fake.calls if c[0] == "select_option"]
    assert sel and sel[0][1][0] == ["a", "b"]


@pytest.mark.asyncio
async def test_fill_skips_empty_refs() -> None:
    """Empty ref must be silently skipped (deep dive gotcha #10)."""
    page = _Page()
    seen: list[str] = []

    def resolver(r: str) -> _Locator:
        seen.append(r)
        return _Locator(r)

    await execute_single_action(
        page,
        "fill",
        {
            "fields": [
                {"ref": "", "value": "skip-me"},
                {"ref": "e1", "value": "kept"},
            ]
        },
        ref_resolver=resolver,
    )
    assert seen == ["e1"]


@pytest.mark.asyncio
async def test_fill_checkbox_uses_set_checked() -> None:
    page = _Page()
    fake = _Locator("cb")
    await execute_single_action(
        page,
        "fill",
        {"fields": [{"ref": "e1", "type": "checkbox", "value": "true"}]},
        ref_resolver=lambda r: fake,
    )
    chks = [c for c in fake.calls if c[0] == "set_checked"]
    assert chks and chks[0][1][0] is True


@pytest.mark.asyncio
async def test_wait_runs_steps_in_order() -> None:
    page = _Page()
    await execute_single_action(
        page,
        "wait",
        {"time_ms": 1, "url": "https://x.com", "load_state": "load"},
    )
    kinds = [k for k, _ in page.waited_for]
    assert kinds == ["timeout", "url", "load_state"]


@pytest.mark.asyncio
async def test_evaluate_disabled_raises() -> None:
    page = _Page()
    with pytest.raises(EvaluateDisabledError):
        await execute_single_action(
            page, "evaluate", {"fn": "() => 1"}, evaluate_enabled=False
        )


@pytest.mark.asyncio
async def test_evaluate_returns_result_dict() -> None:
    page = _Page()
    out = await execute_single_action(
        page, "evaluate", {"fn": "() => 42", "timeout_ms": 1000}
    )
    assert out == {"result": "page-result"}
    # The wrapper-string injects the user's fn body inside a Promise.race.
    assert page.evals and "Promise.race" in page.evals[0]


@pytest.mark.asyncio
async def test_evaluate_with_ref_uses_locator_evaluate() -> None:
    page = _Page()
    fake = _Locator("el")
    out = await execute_single_action(
        page,
        "evaluate",
        {"fn": "(el) => el", "ref": "e1", "timeout_ms": 1000},
        ref_resolver=lambda r: fake,
    )
    assert out == {"result": "ref-result"}
    assert any(c[0] == "evaluate" for c in fake.calls)


@pytest.mark.asyncio
async def test_close_calls_page_close() -> None:
    page = _Page()
    await execute_single_action(page, "close", {})
    assert page.closed


@pytest.mark.asyncio
async def test_resize_floors_to_one_min() -> None:
    page = _Page()
    await execute_single_action(page, "resize", {"width": 0.4, "height": -7})
    assert page.viewport == {"width": 1, "height": 1}


@pytest.mark.asyncio
async def test_unknown_kind_raises() -> None:
    page = _Page()
    with pytest.raises(ValueError):
        await execute_single_action(page, "frobnicate", {})
