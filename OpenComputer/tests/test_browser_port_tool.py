"""Tests for the Browser discriminator tool + the 11 deprecation shims."""

from __future__ import annotations

import json
import warnings
from typing import Any

import pytest
from extensions.browser_control._tool import (
    DEPRECATION_SHIMS,
    Browser,
    reset_deprecation_warnings_for_tests,
)
from extensions.browser_control.schema import (
    BrowserAction,
    BrowserActKind,
    browser_params_json_schema,
)

from plugin_sdk.core import ToolCall, ToolResult


class _FakeActions:
    """Stand-in for client.BrowserActions — records every method call."""

    def __init__(self, return_value: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.return_value = return_value or {"ok": True}

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        async def stub(*args, **kwargs):
            payload = dict(kwargs)
            if args:
                payload["__args__"] = args
            self.calls.append((name, payload))
            return self.return_value

        return stub


@pytest.fixture(autouse=True)
def _reset_warnings():
    reset_deprecation_warnings_for_tests()
    yield
    reset_deprecation_warnings_for_tests()


# ─── Schema shape ──────────────────────────────────────────────────────


def test_schema_action_enum_complete():
    # 16 v0.3 actions + 9 added in Wave 4 (adapter promotion).
    assert {a.value for a in BrowserAction} == {
        # v0.3 (16)
        "status", "start", "stop", "profiles", "tabs", "open", "focus",
        "close", "snapshot", "screenshot", "navigate", "console", "pdf",
        "upload", "dialog", "act",
        # Wave 4 (9)
        "network_start", "network_list", "network_detail",
        "resource_timing", "analyze",
        "adapter_new", "adapter_save", "adapter_validate", "verify",
    }
    assert len(BrowserAction) == 25


def test_schema_act_kind_enum_complete():
    assert {k.value for k in BrowserActKind} == {
        "click", "type", "press", "hover", "drag", "select", "fill",
        "resize", "wait", "evaluate", "close",
    }
    assert len(BrowserActKind) == 11


def test_browser_params_json_schema_shape():
    s = browser_params_json_schema()
    assert s["type"] == "object"
    assert s["required"] == ["action"]
    # action enum + nested act-kind enum present
    assert s["properties"]["action"]["enum"]
    assert "kind" in s["properties"]["request"]["properties"]


# ─── Browser tool dispatch ─────────────────────────────────────────────


class TestBrowserDispatch:
    @pytest.mark.asyncio
    async def test_status_dispatch(self):
        actions = _FakeActions(return_value={"running": True})
        tool = Browser(actions=actions)  # type: ignore[arg-type]
        result = await tool.execute(
            ToolCall(id="c1", name="Browser", arguments={"action": "status"})
        )
        assert isinstance(result, ToolResult)
        assert json.loads(result.content) == {"running": True}
        assert actions.calls == [("browser_status", {"profile": None, "base_url": None})]

    @pytest.mark.asyncio
    async def test_navigate_dispatch(self):
        actions = _FakeActions()
        tool = Browser(actions=actions)  # type: ignore[arg-type]
        await tool.execute(
            ToolCall(
                id="c1", name="Browser",
                arguments={"action": "navigate", "url": "https://example.com"},
            )
        )
        assert actions.calls[0][0] == "browser_navigate"
        assert actions.calls[0][1]["url"] == "https://example.com"

    @pytest.mark.asyncio
    async def test_act_with_nested_request(self):
        actions = _FakeActions()
        tool = Browser(actions=actions)  # type: ignore[arg-type]
        await tool.execute(
            ToolCall(
                id="c1", name="Browser",
                arguments={
                    "action": "act",
                    "request": {"kind": "click", "ref": "e12"},
                },
            )
        )
        assert actions.calls[0][0] == "browser_act"
        # browser_act takes the request positionally; _FakeActions stuffs
        # positional args into __args__ so we can introspect.
        positional = actions.calls[0][1].get("__args__", ())
        assert positional, "expected the act request as a positional arg"
        request_arg = positional[0]
        assert request_arg["kind"] == "click"
        assert request_arg["ref"] == "e12"

    @pytest.mark.asyncio
    async def test_act_with_flat_kind(self):
        actions = _FakeActions()
        tool = Browser(actions=actions)  # type: ignore[arg-type]
        await tool.execute(
            ToolCall(
                id="c1", name="Browser",
                arguments={"action": "act", "kind": "fill", "ref": "e12", "text": "hi"},
            )
        )
        assert actions.calls[0][0] == "browser_act"

    @pytest.mark.asyncio
    async def test_unknown_action_returns_error(self):
        tool = Browser(actions=_FakeActions())  # type: ignore[arg-type]
        result = await tool.execute(
            ToolCall(id="c1", name="Browser", arguments={"action": "frobulate"})
        )
        assert result.is_error is True
        assert "unknown action" in result.content.lower()

    @pytest.mark.asyncio
    async def test_missing_action_returns_error(self):
        tool = Browser(actions=_FakeActions())  # type: ignore[arg-type]
        result = await tool.execute(
            ToolCall(id="c1", name="Browser", arguments={})
        )
        assert result.is_error is True
        assert "action" in result.content.lower()

    @pytest.mark.asyncio
    async def test_open_requires_url(self):
        tool = Browser(actions=_FakeActions())  # type: ignore[arg-type]
        result = await tool.execute(
            ToolCall(id="c1", name="Browser", arguments={"action": "open"})
        )
        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_focus_requires_target_id(self):
        tool = Browser(actions=_FakeActions())  # type: ignore[arg-type]
        result = await tool.execute(
            ToolCall(id="c1", name="Browser", arguments={"action": "focus"})
        )
        assert result.is_error is True


# ─── Deprecation shims ─────────────────────────────────────────────────


class TestDeprecationShims:
    def test_eleven_shims(self):
        names = [s().schema.name for s in DEPRECATION_SHIMS]
        assert sorted(names) == sorted([
            "browser_navigate", "browser_click", "browser_fill",
            "browser_snapshot", "browser_scrape", "browser_scroll",
            "browser_back", "browser_press", "browser_get_images",
            "browser_vision", "browser_console",
        ])

    def test_each_shim_has_deprecated_in_description(self):
        for shim_cls in DEPRECATION_SHIMS:
            desc = shim_cls().schema.description
            assert "DEPRECATED" in desc, f"{shim_cls.__name__} missing DEPRECATED tag"

    @pytest.mark.asyncio
    async def test_shim_emits_warning_once_per_process(self, monkeypatch):
        """Module-level _emitted dedupe — same shim warns once."""
        # browser_navigate is the first in the tuple
        cls = next(s for s in DEPRECATION_SHIMS if s().schema.name == "browser_navigate")
        actions = _FakeActions()

        # Patch Browser to use our fake actions for this shim
        from extensions.browser_control import _tool as tool_mod

        original_browser_init = tool_mod.Browser.__init__

        def patched_init(self, *, actions: Any = None, **kw: Any) -> None:
            original_browser_init(self, actions=actions or _FakeActions(), **kw)

        # Two shim instances, should still only warn once
        monkeypatch.setattr(tool_mod.Browser, "__init__", patched_init)
        s1 = cls()
        s2 = cls()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            await s1.execute(
                ToolCall(id="c1", name="browser_navigate", arguments={"url": "http://x"})
            )
            await s2.execute(
                ToolCall(id="c2", name="browser_navigate", arguments={"url": "http://x"})
            )

        # Filter to just our DeprecationWarnings
        deprecations = [
            w for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "browser_navigate" in str(w.message)
        ]
        assert len(deprecations) == 1, (
            f"expected one deprecation warning, got {len(deprecations)}: "
            f"{[str(w.message) for w in deprecations]}"
        )

    @pytest.mark.asyncio
    async def test_shim_dispatches_to_browser(self, monkeypatch):
        from extensions.browser_control import _tool as tool_mod

        captured: dict[str, Any] = {}

        async def fake_execute(self: Any, call: ToolCall) -> ToolResult:
            captured["browser_args"] = dict(call.arguments)
            return ToolResult(tool_call_id=call.id, content="{}")

        monkeypatch.setattr(tool_mod.Browser, "execute", fake_execute)

        cls = next(s for s in DEPRECATION_SHIMS if s().schema.name == "browser_navigate")
        await cls().execute(
            ToolCall(id="c1", name="browser_navigate", arguments={"url": "http://x"})
        )
        assert captured["browser_args"]["action"] == "navigate"
        assert captured["browser_args"]["url"] == "http://x"

    @pytest.mark.asyncio
    async def test_click_shim_maps_to_act_click(self, monkeypatch):
        from extensions.browser_control import _tool as tool_mod
        captured: dict[str, Any] = {}

        async def fake_execute(self: Any, call: ToolCall) -> ToolResult:
            captured["browser_args"] = dict(call.arguments)
            return ToolResult(tool_call_id=call.id, content="{}")

        monkeypatch.setattr(tool_mod.Browser, "execute", fake_execute)

        cls = next(s for s in DEPRECATION_SHIMS if s().schema.name == "browser_click")
        await cls().execute(
            ToolCall(
                id="c1", name="browser_click",
                arguments={"url": "http://x", "selector": "#btn"},
            )
        )
        assert captured["browser_args"]["action"] == "act"
        assert captured["browser_args"]["kind"] == "click"
        assert captured["browser_args"]["selector"] == "#btn"
