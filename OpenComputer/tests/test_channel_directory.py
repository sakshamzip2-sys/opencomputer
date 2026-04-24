"""Tests for II.3 — channel directory enumeration.

Covers the persistent {(platform, chat_id) -> display_name, last_seen}
cache at ``opencomputer/gateway/channel_directory.py``. Mirrors Hermes'
pattern at ``sources/hermes-agent/gateway/channel_directory.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ─── record + retrieve roundtrip ────────────────────────────────────────


def test_record_and_get_roundtrip(tmp_path: Path) -> None:
    from opencomputer.gateway.channel_directory import ChannelDirectory

    path = tmp_path / "channels.json"
    d = ChannelDirectory(path=path)
    d.record("telegram", "12345", display_name="Saksham")
    entry = d.get("telegram", "12345")
    assert entry is not None
    assert entry.platform == "telegram"
    assert entry.chat_id == "12345"
    assert entry.display_name == "Saksham"
    assert entry.last_seen > 0


def test_record_updates_last_seen(tmp_path: Path) -> None:
    """Subsequent records for same (platform, chat_id) bump ``last_seen``."""
    from opencomputer.gateway.channel_directory import ChannelDirectory

    d = ChannelDirectory(path=tmp_path / "channels.json")
    d.record("telegram", "1", display_name="Alice")
    first = d.get("telegram", "1")
    assert first is not None
    # Sleep a hair so monotonic clock ticks
    time.sleep(0.01)
    d.record("telegram", "1")  # no display name — shouldn't erase
    second = d.get("telegram", "1")
    assert second is not None
    assert second.display_name == "Alice"  # preserved when record omits name
    assert second.last_seen > first.last_seen


def test_record_updates_display_name_when_provided(tmp_path: Path) -> None:
    """Re-recording with a new display_name overwrites the previous one."""
    from opencomputer.gateway.channel_directory import ChannelDirectory

    d = ChannelDirectory(path=tmp_path / "channels.json")
    d.record("telegram", "1", display_name="Alice")
    d.record("telegram", "1", display_name="Alice Smith")
    entry = d.get("telegram", "1")
    assert entry is not None
    assert entry.display_name == "Alice Smith"


def test_get_returns_none_for_unknown(tmp_path: Path) -> None:
    from opencomputer.gateway.channel_directory import ChannelDirectory

    d = ChannelDirectory(path=tmp_path / "channels.json")
    assert d.get("telegram", "missing") is None


# ─── multi-platform isolation ───────────────────────────────────────────


def test_multiple_platforms_do_not_collide(tmp_path: Path) -> None:
    """Same chat_id under different platforms is keyed independently."""
    from opencomputer.gateway.channel_directory import ChannelDirectory

    d = ChannelDirectory(path=tmp_path / "channels.json")
    d.record("telegram", "123", display_name="TG-chat")
    d.record("discord", "123", display_name="DC-chan")

    tg = d.get("telegram", "123")
    dc = d.get("discord", "123")
    assert tg is not None and tg.display_name == "TG-chat"
    assert dc is not None and dc.display_name == "DC-chan"


def test_list_all_sorts_by_most_recent_last_seen(tmp_path: Path) -> None:
    from opencomputer.gateway.channel_directory import ChannelDirectory

    d = ChannelDirectory(path=tmp_path / "channels.json")
    d.record("telegram", "a", display_name="First")
    time.sleep(0.01)
    d.record("discord", "b", display_name="Second")
    time.sleep(0.01)
    d.record("telegram", "c", display_name="Third")

    entries = d.list_all()
    assert len(entries) == 3
    # Most recent first
    ids = [e.chat_id for e in entries]
    assert ids == ["c", "b", "a"]


# ─── persistence ────────────────────────────────────────────────────────


def test_save_and_load_existing_file(tmp_path: Path) -> None:
    from opencomputer.gateway.channel_directory import ChannelDirectory

    path = tmp_path / "channels.json"
    d1 = ChannelDirectory(path=path)
    d1.record("telegram", "1", display_name="Alice")
    d1.record("discord", "2", display_name="Bob")
    # A fresh instance should read them back.
    d2 = ChannelDirectory(path=path)
    alice = d2.get("telegram", "1")
    bob = d2.get("discord", "2")
    assert alice is not None and alice.display_name == "Alice"
    assert bob is not None and bob.display_name == "Bob"


def test_default_path_uses_home(monkeypatch, tmp_path: Path) -> None:
    """Default path is ``$OPENCOMPUTER_HOME/channel_directory.json`` or
    ``~/.opencomputer/channel_directory.json``.
    """
    from opencomputer.gateway.channel_directory import ChannelDirectory

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / ".opencomputer"))
    d = ChannelDirectory()
    # Expected path under our sandboxed home
    expected = tmp_path / ".opencomputer" / "channel_directory.json"
    assert d.path == expected


# ─── atomicity ──────────────────────────────────────────────────────────


def test_atomic_write_leaves_no_tmp_file(tmp_path: Path) -> None:
    """After a successful save, the ``.tmp`` sibling must not exist."""
    from opencomputer.gateway.channel_directory import ChannelDirectory

    path = tmp_path / "channels.json"
    d = ChannelDirectory(path=path)
    d.record("telegram", "1", display_name="Alice")

    assert path.exists()
    # The atomic-write pattern writes to ``<path>.tmp`` then os.replace()s.
    # On success the tmp file is gone.
    assert not (path.with_suffix(path.suffix + ".tmp")).exists()


def test_interrupted_write_does_not_corrupt_primary(tmp_path: Path, monkeypatch) -> None:
    """Simulate a crash between tmp-write and os.replace: tmp file may
    exist but the primary file is untouched (or absent on first run).
    """
    import os

    from opencomputer.gateway.channel_directory import ChannelDirectory

    path = tmp_path / "channels.json"
    d = ChannelDirectory(path=path)
    # First successful record lands on disk.
    d.record("telegram", "1", display_name="Alice")
    primary_before = path.read_text()

    # Patch os.replace so the second "record" crashes after writing
    # the tmp file but before swapping it in.
    orig_replace = os.replace

    def boom(src, dst):  # noqa: ARG001
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError):
        d.record("telegram", "2", display_name="Bob")

    # Primary file is unchanged (os.replace never ran).
    assert path.read_text() == primary_before

    # Restore + verify we can still read-load the untouched primary.
    monkeypatch.setattr(os, "replace", orig_replace)
    fresh = ChannelDirectory(path=path)
    assert fresh.get("telegram", "1") is not None
    assert fresh.get("telegram", "2") is None


def test_malformed_json_yields_empty_directory(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupted file on disk must NOT crash load; emit WARNING + empty dir."""
    from opencomputer.gateway.channel_directory import ChannelDirectory

    path = tmp_path / "channels.json"
    path.write_text("{ not valid json ]]]")

    with caplog.at_level(logging.WARNING, logger="opencomputer.gateway.channel_directory"):
        d = ChannelDirectory(path=path)

    assert d.list_all() == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("channel_directory" in (r.name or "") for r in warnings)


def test_load_tolerates_missing_file(tmp_path: Path) -> None:
    from opencomputer.gateway.channel_directory import ChannelDirectory

    d = ChannelDirectory(path=tmp_path / "does-not-exist.json")
    assert d.list_all() == []


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    from opencomputer.gateway.channel_directory import ChannelDirectory

    nested = tmp_path / "a" / "b" / "c" / "channels.json"
    d = ChannelDirectory(path=nested)
    d.record("telegram", "1", display_name="Alice")
    assert nested.exists()
    data = json.loads(nested.read_text())
    # Internal shape: mapping from composite key to entry dict.
    assert isinstance(data, dict)


# ─── dispatch integration ──────────────────────────────────────────────


def test_dispatch_records_incoming_events(tmp_path: Path) -> None:
    """``Dispatch.handle_message`` records each incoming event into the
    directory. First message seeds the entry; subsequent messages update
    ``last_seen``.
    """
    from opencomputer.agent.loop import ConversationResult
    from opencomputer.gateway.channel_directory import ChannelDirectory
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import Message, MessageEvent, Platform

    directory = ChannelDirectory(path=tmp_path / "channels.json")

    final = Message(role="assistant", content="hi back")
    result = ConversationResult(
        final_message=final,
        messages=[final],
        session_id="s",
        iterations=1,
        input_tokens=0,
        output_tokens=0,
    )
    mock_loop = MagicMock()
    mock_loop.run_conversation = AsyncMock(return_value=result)

    d = Dispatch(mock_loop, channel_directory=directory)
    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="555",
        user_id="u",
        text="hi",
        timestamp=0.0,
        metadata={"display_name": "Saksham"},
    )

    asyncio.run(d.handle_message(event))

    entry = directory.get("telegram", "555")
    assert entry is not None
    assert entry.display_name == "Saksham"


def test_dispatch_without_directory_is_backwards_compatible() -> None:
    """Existing callers that don't pass ``channel_directory`` still work."""
    from opencomputer.agent.loop import ConversationResult
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import Message, MessageEvent, Platform

    final = Message(role="assistant", content="world")
    result = ConversationResult(
        final_message=final,
        messages=[final],
        session_id="s",
        iterations=1,
        input_tokens=0,
        output_tokens=0,
    )
    mock_loop = MagicMock()
    mock_loop.run_conversation = AsyncMock(return_value=result)

    d = Dispatch(mock_loop)  # no channel_directory kwarg
    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="1",
        user_id="u",
        text="hello",
        timestamp=0.0,
    )
    out = asyncio.run(d.handle_message(event))
    assert out == "world"


# ─── CLI ───────────────────────────────────────────────────────────────


def test_cli_channels_list_empty(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from opencomputer.cli import app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / ".opencomputer"))

    runner = CliRunner()
    result = runner.invoke(app, ["channels", "list"])
    assert result.exit_code == 0
    assert "no channels recorded" in result.stdout


def test_cli_channels_list_renders_table(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from opencomputer.cli import app
    from opencomputer.gateway.channel_directory import ChannelDirectory

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / ".opencomputer"))

    d = ChannelDirectory()
    d.record("telegram", "42", display_name="Saksham")
    d.record("discord", "99", display_name="Alice")

    runner = CliRunner()
    result = runner.invoke(app, ["channels", "list"])
    assert result.exit_code == 0
    # Rich renders a table; both entries should appear in the output.
    assert "telegram" in result.stdout
    assert "Saksham" in result.stdout
    assert "discord" in result.stdout
    assert "Alice" in result.stdout


def test_dispatch_record_failure_does_not_break_reply(tmp_path: Path) -> None:
    """If the directory write fails, dispatch still returns the reply."""
    from opencomputer.agent.loop import ConversationResult
    from opencomputer.gateway.channel_directory import ChannelDirectory
    from opencomputer.gateway.dispatch import Dispatch
    from plugin_sdk.core import Message, MessageEvent, Platform

    directory = ChannelDirectory(path=tmp_path / "channels.json")
    # Poison the record method to raise.
    original = directory.record

    def broken(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("disk on fire")

    directory.record = broken  # type: ignore[method-assign]

    final = Message(role="assistant", content="reply")
    result = ConversationResult(
        final_message=final,
        messages=[final],
        session_id="s",
        iterations=1,
        input_tokens=0,
        output_tokens=0,
    )
    mock_loop = MagicMock()
    mock_loop.run_conversation = AsyncMock(return_value=result)
    d = Dispatch(mock_loop, channel_directory=directory)
    event = MessageEvent(
        platform=Platform.TELEGRAM,
        chat_id="1",
        user_id="u",
        text="hi",
        timestamp=0.0,
    )
    out = asyncio.run(d.handle_message(event))
    assert out == "reply"

    # Restore to keep any teardown sane.
    directory.record = original  # type: ignore[method-assign]
