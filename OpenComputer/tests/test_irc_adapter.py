"""Tests for the IRC channel adapter."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent


def _load_adapter():
    """Load the IRC adapter via spec_from_file_location to match the
    plugin loader's pattern."""
    sys.modules.pop("adapter", None)
    spec = importlib.util.spec_from_file_location(
        "adapter", _REPO / "extensions" / "irc" / "adapter.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_parse_irc_line_with_prefix():
    mod = _load_adapter()
    prefix, cmd, params = mod._parse_irc_line(":alice!a@host PRIVMSG #foo :hi there\r\n")
    assert prefix == "alice!a@host"
    assert cmd == "PRIVMSG"
    assert params == ["#foo", "hi there"]


def test_parse_irc_line_without_prefix():
    mod = _load_adapter()
    prefix, cmd, params = mod._parse_irc_line("PING :server.example.com")
    assert prefix is None
    assert cmd == "PING"
    assert params == ["server.example.com"]


def test_nick_from_prefix():
    mod = _load_adapter()
    assert mod._nick_from_prefix("alice!a@host") == "alice"
    assert mod._nick_from_prefix("alice") == "alice"
    assert mod._nick_from_prefix(None) == ""


def test_chunk_text_short_returns_single():
    mod = _load_adapter()
    assert mod._chunk_text("short", 100) == ["short"]


def test_chunk_text_long_breaks_at_word_boundary():
    mod = _load_adapter()
    text = "this is a long line that needs splitting into pieces"
    chunks = mod._chunk_text(text, 20)
    assert all(len(c) <= 20 for c in chunks)
    # Reconstruct to verify content preserved
    assert " ".join(chunks).replace("  ", " ").strip() == text


def test_adapter_class_attributes():
    mod = _load_adapter()
    from plugin_sdk.core import Platform
    assert mod.IRCAdapter.platform == Platform.IRC


def test_adapter_init_reads_env_vars(monkeypatch):
    mod = _load_adapter()
    monkeypatch.setenv("IRC_SERVER", "irc.example.com:6667")
    monkeypatch.setenv("IRC_NICK", "testbot")
    monkeypatch.setenv("IRC_CHANNELS", "#chan1,#chan2")
    monkeypatch.setenv("IRC_TLS", "0")

    adapter = mod.IRCAdapter()
    assert adapter._host == "irc.example.com"
    assert adapter._port == 6667
    assert adapter._nick == "testbot"
    assert adapter._channels == ["#chan1", "#chan2"]
    assert adapter._tls is False


def test_adapter_init_falls_back_to_defaults(monkeypatch):
    mod = _load_adapter()
    monkeypatch.delenv("IRC_SERVER", raising=False)
    monkeypatch.delenv("IRC_NICK", raising=False)
    monkeypatch.delenv("IRC_CHANNELS", raising=False)

    adapter = mod.IRCAdapter()
    assert "libera" in adapter._host
    assert adapter._nick == "opencomputer"
    assert adapter._channels == []


@pytest.mark.asyncio
async def test_send_returns_failure_when_not_connected():
    mod = _load_adapter()
    adapter = mod.IRCAdapter({"server": "x:6667", "nick": "n"})
    result = await adapter.send("#foo", "hi")
    assert result.success is False


@pytest.mark.asyncio
async def test_connect_writes_nick_user_join(monkeypatch):
    mod = _load_adapter()

    # Mock asyncio.open_connection to return mock streams
    written: list[str] = []

    class MockWriter:
        def __init__(self):
            self._closing = False

        def write(self, data):
            written.append(data.decode())

        async def drain(self):
            pass

        def is_closing(self):
            return self._closing

        def close(self):
            self._closing = True

        async def wait_closed(self):
            pass

    mock_reader = MagicMock()
    mock_reader.at_eof.return_value = True
    mock_reader.readline = AsyncMock(return_value=b"")

    async def fake_open_connection(*args, **kwargs):
        return mock_reader, MockWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    adapter = mod.IRCAdapter({
        "server": "irc.example.com:6667",
        "nick": "testbot",
        "channels": "#test",
        "tls": "0",
    })
    success = await adapter.connect()
    assert success is True
    # Cancel the read loop so the test can exit cleanly
    if adapter._read_task:
        adapter._read_task.cancel()

    # Expected protocol writes
    flat = "".join(written)
    assert "NICK testbot" in flat
    assert "USER testbot 0 *" in flat
    assert "JOIN #test" in flat


@pytest.mark.asyncio
async def test_read_loop_responds_to_ping(monkeypatch):
    mod = _load_adapter()

    written: list[str] = []

    class MockWriter:
        _closing = False
        def write(self, data):
            written.append(data.decode())
        async def drain(self):
            pass
        def is_closing(self):
            return self._closing
        def close(self):
            self._closing = True
        async def wait_closed(self):
            pass

    # Reader yields one PING line then EOF
    lines_to_read = [b"PING :server.example.com\r\n", b""]
    line_idx = [0]

    async def fake_readline():
        idx = line_idx[0]
        line_idx[0] += 1
        if idx >= len(lines_to_read):
            return b""
        return lines_to_read[idx]

    mock_reader = MagicMock()
    eof_idx = [False]

    def fake_at_eof():
        result = eof_idx[0]
        eof_idx[0] = line_idx[0] >= len(lines_to_read)
        return result

    mock_reader.at_eof = fake_at_eof
    mock_reader.readline = fake_readline

    async def fake_open_connection(*args, **kwargs):
        return mock_reader, MockWriter()

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    adapter = mod.IRCAdapter({"server": "x:6667", "nick": "n", "tls": "0"})
    await adapter.connect()
    # Wait briefly for read loop to consume the PING
    await asyncio.sleep(0.05)
    if adapter._read_task:
        adapter._read_task.cancel()
        try:
            await adapter._read_task
        except asyncio.CancelledError:
            pass

    flat = "".join(written)
    assert "PONG :server.example.com" in flat


def test_plugin_manifest():
    manifest = json.loads(
        (_REPO / "extensions" / "irc" / "plugin.json").read_text()
    )
    assert manifest["entry"] == "plugin"
    assert manifest["kind"] == "channel"
    setup = manifest["setup"]["channels"][0]
    assert setup["id"] == "irc"
    assert "IRC_SERVER" in setup["env_vars"]


def test_appears_in_wizard_discovery():
    from opencomputer.cli_setup.section_handlers.messaging_platforms import (
        _discover_platforms,
    )
    ids = {p["name"] for p in _discover_platforms()}
    assert "irc" in ids
