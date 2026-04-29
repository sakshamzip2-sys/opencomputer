"""Tests for /copy slash command."""
import base64
import io

import pytest

from opencomputer.agent.slash_commands_impl.copy_cmd import (
    CopyCommand,
    _osc52_payload,
)
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT


def test_osc52_payload_format():
    payload = _osc52_payload("hello")
    assert payload.startswith("\x1b]52;c;")
    assert payload.endswith("\x07")
    # Decode the middle and check
    body = payload[7:-1]  # strip ESC]52;c; and BEL
    assert base64.b64decode(body) == b"hello"


def test_osc52_unicode():
    payload = _osc52_payload("日本語 🎌")
    body = payload[7:-1]
    assert base64.b64decode(body).decode("utf-8") == "日本語 🎌"


@pytest.mark.asyncio
async def test_copy_with_text(monkeypatch):
    """Successful copy emits OSC-52 to stdout."""
    captured = io.StringIO()
    monkeypatch.setattr("sys.stdout", captured)
    cmd = CopyCommand()
    result = await cmd.execute("hello world", DEFAULT_RUNTIME_CONTEXT)
    assert result.handled is True
    assert "Copied" in result.output
    assert "11" in result.output  # "hello world" is 11 chars
    output = captured.getvalue()
    assert output.startswith("\x1b]52;c;")
    assert output.endswith("\x07")
    body = output[7:-1]
    assert base64.b64decode(body) == b"hello world"


@pytest.mark.asyncio
async def test_copy_empty_args_shows_usage():
    cmd = CopyCommand()
    result = await cmd.execute("", DEFAULT_RUNTIME_CONTEXT)
    assert result.handled is True
    assert "Usage" in result.output
    assert "/copy" in result.output


@pytest.mark.asyncio
async def test_copy_whitespace_only_shows_usage():
    cmd = CopyCommand()
    result = await cmd.execute("   \t  ", DEFAULT_RUNTIME_CONTEXT)
    assert result.handled is True
    assert "Usage" in result.output


@pytest.mark.asyncio
async def test_copy_truncates_oversized_text(monkeypatch):
    """Text whose base64 encoding exceeds 4KB should be truncated with a notice."""
    captured = io.StringIO()
    monkeypatch.setattr("sys.stdout", captured)
    cmd = CopyCommand()
    huge = "A" * 10_000  # base64-encoded ≈ 13_336 bytes > 4096
    result = await cmd.execute(huge, DEFAULT_RUNTIME_CONTEXT)
    assert result.handled is True
    assert "truncated" in result.output.lower()
    output = captured.getvalue()
    body = output[7:-1]
    decoded = base64.b64decode(body)
    # Truncated copy should be shorter than the original
    assert len(decoded) < len(huge)


@pytest.mark.asyncio
async def test_copy_text_with_newlines(monkeypatch):
    """Newlines should round-trip through base64."""
    captured = io.StringIO()
    monkeypatch.setattr("sys.stdout", captured)
    cmd = CopyCommand()
    text = "line one\nline two\nline three"
    result = await cmd.execute(text, DEFAULT_RUNTIME_CONTEXT)
    assert result.handled is True
    body = captured.getvalue()[7:-1]
    assert base64.b64decode(body).decode("utf-8") == text


def test_command_metadata():
    cmd = CopyCommand()
    assert cmd.name == "copy"
    assert "clipboard" in cmd.description.lower()
