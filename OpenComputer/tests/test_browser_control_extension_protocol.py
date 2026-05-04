"""Wire-protocol round-trip tests for the control-extension daemon.

Covers ``extensions/browser-control/control_protocol.py`` — Command
serialization (snake_case → camelCase), Result parsing, and the
v0.6 supported-actions gate.

Wave 6 — these are pure-Python tests; no real WS / extension needed.
"""

from __future__ import annotations

import pytest
from extensions.browser_control.control_protocol import (
    DEFAULT_COMMAND_TIMEOUT_S,
    DEFAULT_CONTROL_DAEMON_PORT,
    DEFAULT_CONTROL_PING_PATH,
    DEFAULT_CONTROL_WS_PATH,
    SUPPORTED_ACTIONS_V0_6,
    Command,
    ConnectedExtension,
    HelloMessage,
    LogMessage,
    Result,
)


class TestCommandToWire:
    """Command.to_wire() — snake_case Python → camelCase JSON for the extension."""

    def test_minimal_command(self) -> None:
        cmd = Command(id="abc", action="navigate", url="https://example.com")
        wire = cmd.to_wire()
        assert wire == {
            "id": "abc",
            "action": "navigate",
            "url": "https://example.com",
        }

    def test_omits_none_fields(self) -> None:
        """None-valued optional fields must not appear on the wire."""
        cmd = Command(id="x", action="exec", code="window.location.href")
        wire = cmd.to_wire()
        assert "url" not in wire
        assert "page" not in wire
        assert "matchDomain" not in wire
        assert wire["code"] == "window.location.href"

    def test_snake_to_camel_field_renames(self) -> None:
        cmd = Command(
            id="y",
            action="bind",
            workspace="bound:learnx",
            match_domain="learnx.atriauniversity.in",
            match_path_prefix="/learn",
            full_page=True,
            cdp_method="Page.navigate",
            cdp_params={"url": "https://example.com"},
            window_focused=True,
            idle_timeout=600,
            allow_bound_navigation=False,
            frame_index=0,
            context_id="user",
        )
        wire = cmd.to_wire()
        # Verify each renamed key is present in camelCase form.
        assert wire["matchDomain"] == "learnx.atriauniversity.in"
        assert wire["matchPathPrefix"] == "/learn"
        assert wire["fullPage"] is True
        assert wire["cdpMethod"] == "Page.navigate"
        assert wire["cdpParams"] == {"url": "https://example.com"}
        assert wire["windowFocused"] is True
        assert wire["idleTimeout"] == 600
        assert wire["allowBoundNavigation"] is False
        assert wire["frameIndex"] == 0
        assert wire["contextId"] == "user"
        # Verify no snake_case key leaked through.
        for snake_key in (
            "match_domain",
            "match_path_prefix",
            "full_page",
            "cdp_method",
            "cdp_params",
            "window_focused",
            "idle_timeout",
            "allow_bound_navigation",
            "frame_index",
            "context_id",
        ):
            assert snake_key not in wire, f"snake_case {snake_key!r} leaked to wire"

    def test_tabs_op_select(self) -> None:
        cmd = Command(id="t1", action="tabs", op="select", index=3)
        wire = cmd.to_wire()
        assert wire["op"] == "select"
        assert wire["index"] == 3

    def test_screenshot_jpeg(self) -> None:
        cmd = Command(id="s1", action="screenshot", format="jpeg", quality=80, full_page=True)
        wire = cmd.to_wire()
        assert wire["format"] == "jpeg"
        assert wire["quality"] == 80
        assert wire["fullPage"] is True


class TestResultFromWire:
    """Result.from_wire() — JSON Result from the extension → Python dataclass."""

    def test_success_minimal(self) -> None:
        wire = {"id": "abc", "ok": True, "data": [{"row": 1}]}
        r = Result.from_wire(wire)
        assert r.id == "abc"
        assert r.ok is True
        assert r.data == [{"row": 1}]
        assert r.error is None
        assert r.error_code is None
        assert r.error_hint is None
        assert r.page is None

    def test_failure_with_error_metadata(self) -> None:
        wire = {
            "id": "bad",
            "ok": False,
            "error": "tab closed",
            "errorCode": "tab_gone",
            "errorHint": "open the page first",
        }
        r = Result.from_wire(wire)
        assert r.ok is False
        assert r.error == "tab closed"
        assert r.error_code == "tab_gone"
        assert r.error_hint == "open the page first"

    def test_page_scoped_response(self) -> None:
        wire = {"id": "p1", "ok": True, "data": None, "page": "TARGET-UUID"}
        r = Result.from_wire(wire)
        assert r.page == "TARGET-UUID"

    def test_string_coercion_on_id(self) -> None:
        # Defensive — extension shouldn't send int but if it does, don't crash.
        wire = {"id": 42, "ok": True}  # type: ignore[dict-item]
        r = Result.from_wire(wire)
        assert r.id == "42"
        assert r.ok is True

    def test_non_string_error_dropped(self) -> None:
        # If extension sends ``error: null`` or non-string, we treat as None.
        wire = {"id": "x", "ok": False, "error": None}
        r = Result.from_wire(wire)
        assert r.error is None


class TestHelloMessage:
    def test_round_trip(self) -> None:
        wire = {
            "type": "hello",
            "contextId": "user",
            "version": "0.6.0",
            "compatRange": "^0.6.0",
        }
        h = HelloMessage.from_wire(wire)
        assert h.context_id == "user"
        assert h.version == "0.6.0"
        assert h.compat_range == "^0.6.0"

    def test_missing_fields_default_to_empty_strings(self) -> None:
        h = HelloMessage.from_wire({"type": "hello"})
        assert h.context_id == ""
        assert h.version == ""
        assert h.compat_range == ""


class TestLogMessage:
    def test_levels(self) -> None:
        for level in ("info", "warn", "error"):
            wire = {"type": "log", "level": level, "msg": "hi", "ts": 1234}
            m = LogMessage.from_wire(wire)
            assert m.level == level
            assert m.msg == "hi"
            assert m.ts == 1234

    def test_invalid_level_falls_back_to_info(self) -> None:
        wire = {"type": "log", "level": "panic", "msg": "?", "ts": 0}
        m = LogMessage.from_wire(wire)
        assert m.level == "info"


class TestSupportedActions:
    """v0.6 ships 8 of 14 actions; daemon gates the unsupported ones."""

    def test_supported_actions_count(self) -> None:
        assert len(SUPPORTED_ACTIONS_V0_6) == 8

    def test_supported_set_matches_blueprint(self) -> None:
        expected = {
            "exec",
            "navigate",
            "tabs",
            "cookies",
            "screenshot",
            "network-capture-start",
            "network-capture-read",
            "cdp",
        }
        assert expected == SUPPORTED_ACTIONS_V0_6

    def test_unsupported_actions_in_v0_6(self) -> None:
        """Sanity: actions documented as v0.6.x are NOT in the v0.6 set."""
        for unsupported in ("set-file-input", "insert-text", "bind", "frames", "sessions", "close-window"):
            assert unsupported not in SUPPORTED_ACTIONS_V0_6


class TestProtocolDefaults:
    def test_daemon_port_matches_browser_control(self) -> None:
        # Should match DEFAULT_BROWSER_CONTROL_PORT in profiles/config.py
        assert DEFAULT_CONTROL_DAEMON_PORT == 18792

    def test_paths(self) -> None:
        assert DEFAULT_CONTROL_WS_PATH == "/ext"
        assert DEFAULT_CONTROL_PING_PATH == "/ping"

    def test_command_timeout(self) -> None:
        assert DEFAULT_COMMAND_TIMEOUT_S == 30.0


class TestConnectedExtension:
    def test_default_pending_is_empty_dict(self) -> None:
        ext = ConnectedExtension(
            context_id="user",
            extension_version="0.6.0",
            compat_range="^0.6.0",
        )
        assert ext.pending == {}
        # Default-factory protection: each instance gets its own dict.
        ext2 = ConnectedExtension(
            context_id="opencomputer",
            extension_version="0.6.0",
            compat_range="^0.6.0",
        )
        ext.pending["x"] = 1
        assert ext2.pending == {}


# Pytest's "collection" check — every test class above should be discovered.
def test_smoke_imports() -> None:
    """If any of the protocol module's exports break, this fails fast."""
    assert Command is not None
    assert Result is not None
    assert HelloMessage is not None
    assert LogMessage is not None
    assert ConnectedExtension is not None
    assert SUPPORTED_ACTIONS_V0_6
