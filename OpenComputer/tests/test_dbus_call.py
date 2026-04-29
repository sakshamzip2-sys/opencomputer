"""DBusCall tool — Linux-only D-Bus method invocation via dbus-send."""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

from plugin_sdk.core import ToolCall


def _make_tool():
    from opencomputer.tools.dbus_call import DBusCallTool
    return DBusCallTool()


def test_schema_name_and_linux_only_doc() -> None:
    tool = _make_tool()
    assert tool.schema.name == "DBusCall"
    assert "linux" in tool.schema.description.lower()


def test_capability_id_uses_gui_namespace() -> None:
    """Match gui.applescript_run / gui.powershell_run namespace."""
    tool = _make_tool()
    assert tool.capability_claims[0].capability_id == "gui.dbus_call"


def test_parallel_safe_false() -> None:
    """D-Bus methods can mutate desktop state — not parallel-safe."""
    tool = _make_tool()
    assert tool.parallel_safe is False


def test_returns_error_on_non_linux() -> None:
    tool = _make_tool()
    if sys.platform.startswith("linux"):
        pytest.skip("only tests the non-linux guard")
    call = ToolCall(
        id="t1", name="DBusCall",
        arguments={
            "bus": "session",
            "destination": "org.freedesktop.Notifications",
            "object_path": "/org/freedesktop/Notifications",
            "interface": "org.freedesktop.Notifications",
            "method": "GetCapabilities",
        },
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "linux" in result.content.lower()


def test_constructs_dbus_send_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux, DBusCall builds the right ``dbus-send`` argv."""
    monkeypatch.setattr("sys.platform", "linux")
    fake_run = MagicMock(return_value=MagicMock(returncode=0, stdout="ok", stderr=""))

    with patch("opencomputer.tools.dbus_call.shutil.which", return_value="/usr/bin/dbus-send"):
        with patch("opencomputer.tools.dbus_call.subprocess.run", fake_run):
            tool = _make_tool()
            call = ToolCall(
                id="t1", name="DBusCall",
                arguments={
                    "bus": "session",
                    "destination": "org.gnome.Shell",
                    "object_path": "/org/gnome/Shell",
                    "interface": "org.gnome.Shell",
                    "method": "Eval",
                    "args": ["string:1+1"],
                },
            )
            result = asyncio.run(tool.execute(call))

    args, _ = fake_run.call_args
    argv = args[0]
    assert argv[0] == "/usr/bin/dbus-send"
    assert "--session" in argv
    assert "--dest=org.gnome.Shell" in argv
    assert "--type=method_call" in argv
    assert "--print-reply" in argv
    assert "/org/gnome/Shell" in argv
    assert "org.gnome.Shell.Eval" in argv
    assert "string:1+1" in argv
    assert "ok" in result.content
    assert result.is_error is False


def test_invalid_bus_kind_rejected() -> None:
    tool = _make_tool()
    call = ToolCall(
        id="t1", name="DBusCall",
        arguments={
            "bus": "weird",  # only "session" or "system" allowed
            "destination": "org.x",
            "object_path": "/x",
            "interface": "org.x",
            "method": "X",
        },
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error is True
    assert "bus" in result.content.lower()
