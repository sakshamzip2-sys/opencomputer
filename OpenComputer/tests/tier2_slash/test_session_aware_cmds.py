"""Tests for session-aware slash commands: /title, /history."""
from types import SimpleNamespace

import pytest

from opencomputer.agent.slash_commands_impl.history_cmd import HistoryCommand
from opencomputer.agent.slash_commands_impl.title_cmd import TitleCommand
from plugin_sdk.runtime_context import RuntimeContext


class _FakeDB:
    """Minimal SessionDB stand-in for these tests."""

    def __init__(self) -> None:
        self.titles: dict[str, str | None] = {}
        self.messages: dict[str, list] = {}

    def get_session_title(self, sid: str) -> str | None:
        return self.titles.get(sid)

    def set_session_title(self, sid: str, title: str) -> None:
        self.titles[sid] = title

    def get_messages(self, sid: str) -> list:
        return list(self.messages.get(sid, []))


def _runtime_with_session(sid: str, db: _FakeDB) -> RuntimeContext:
    return RuntimeContext(custom={"session_id": sid, "session_db": db})


def _runtime_no_session() -> RuntimeContext:
    return RuntimeContext(custom={})


# --- /title ---


@pytest.mark.asyncio
async def test_title_get_when_unset():
    db = _FakeDB()
    rt = _runtime_with_session("s1", db)
    result = await TitleCommand().execute("", rt)
    assert "no title" in result.output.lower()


@pytest.mark.asyncio
async def test_title_set_then_get():
    db = _FakeDB()
    rt = _runtime_with_session("s1", db)
    set_result = await TitleCommand().execute("my-debug", rt)
    assert "titled" in set_result.output.lower()
    assert db.titles["s1"] == "my-debug"
    get_result = await TitleCommand().execute("", rt)
    assert "my-debug" in get_result.output


@pytest.mark.asyncio
async def test_title_no_session_in_runtime():
    rt = _runtime_no_session()
    result = await TitleCommand().execute("foo", rt)
    assert "no active session" in result.output.lower()


@pytest.mark.asyncio
async def test_title_too_long_rejected():
    db = _FakeDB()
    rt = _runtime_with_session("s1", db)
    huge = "x" * 250
    result = await TitleCommand().execute(huge, rt)
    assert "too long" in result.output.lower()
    assert "s1" not in db.titles


# --- /history ---


@pytest.mark.asyncio
async def test_history_empty():
    db = _FakeDB()
    rt = _runtime_with_session("s1", db)
    result = await HistoryCommand().execute("", rt)
    assert "no messages" in result.output.lower()


@pytest.mark.asyncio
async def test_history_renders_messages():
    db = _FakeDB()
    db.messages["s1"] = [
        SimpleNamespace(role="user", content="hello"),
        SimpleNamespace(role="assistant", content="hi there!"),
    ]
    rt = _runtime_with_session("s1", db)
    result = await HistoryCommand().execute("", rt)
    assert "hello" in result.output
    assert "hi there" in result.output


@pytest.mark.asyncio
async def test_history_truncates_long_content():
    db = _FakeDB()
    long_content = "x" * 1000
    db.messages["s1"] = [SimpleNamespace(role="user", content=long_content)]
    rt = _runtime_with_session("s1", db)
    result = await HistoryCommand().execute("", rt)
    assert "x" * 240 in result.output  # PREVIEW_CHARS
    assert "…" in result.output


@pytest.mark.asyncio
async def test_history_respects_n_arg():
    db = _FakeDB()
    db.messages["s1"] = [
        SimpleNamespace(role="user", content=f"msg {i}") for i in range(20)
    ]
    rt = _runtime_with_session("s1", db)
    result = await HistoryCommand().execute("3", rt)
    # Only last 3 should appear
    assert "msg 19" in result.output
    assert "msg 17" in result.output
    assert "msg 15" not in result.output


@pytest.mark.asyncio
async def test_history_invalid_n_shows_usage():
    db = _FakeDB()
    rt = _runtime_with_session("s1", db)
    result = await HistoryCommand().execute("not-a-number", rt)
    assert "Usage" in result.output


@pytest.mark.asyncio
async def test_history_no_session():
    rt = _runtime_no_session()
    result = await HistoryCommand().execute("", rt)
    assert "no active session" in result.output.lower()


@pytest.mark.asyncio
async def test_history_handles_multimodal_content():
    """Content can be a list of blocks for multimodal turns."""
    db = _FakeDB()
    db.messages["s1"] = [
        SimpleNamespace(role="user", content=[
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {"data": "..."}},
        ]),
    ]
    rt = _runtime_with_session("s1", db)
    result = await HistoryCommand().execute("", rt)
    assert "look at this" in result.output
    assert "[image]" in result.output


def test_metadata():
    for cls in (TitleCommand, HistoryCommand):
        cmd = cls()
        assert cmd.name
        assert cmd.description
