"""Tests for /btw — ephemeral side-question.

Post-aux-llm refactor: /btw routes through ``opencomputer.agent.aux_llm.complete_text``
which dispatches to the user's configured provider. Tests mock that helper
rather than the Anthropic SDK directly — so /btw is provider-agnostic by
construction.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opencomputer.agent.slash_commands_impl.btw_cmd import (
    BtwCommand,
    _build_messages_payload,
    _flatten_content,
)
from plugin_sdk.runtime_context import RuntimeContext


def _patch_complete_text(monkeypatch, response_text: str = "Quick answer."):
    """Patch the auxiliary-LLM helper to return canned text and capture
    the arguments the /btw command passed in. Returns the AsyncMock so
    tests can inspect ``call_args`` afterwards.
    """
    mock = AsyncMock(return_value=response_text)
    monkeypatch.setattr(
        "opencomputer.agent.slash_commands_impl.btw_cmd.complete_text",
        mock,
        raising=False,
    )
    # Some tests import `complete_text` lazily inside execute(); also patch
    # the source module so a fresh import resolves to the mock.
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text",
        mock,
        raising=False,
    )
    return mock


class _FakeDB:
    def __init__(self) -> None:
        self.messages: dict[str, list] = {}

    def get_messages(self, sid):
        return list(self.messages.get(sid, []))


def _runtime(sid: str = "s1", db: _FakeDB | None = None) -> RuntimeContext:
    return RuntimeContext(custom={"session_id": sid, "session_db": db or _FakeDB()})


# ---------- helpers ----------


def test_flatten_content_string():
    assert _flatten_content("hello") == "hello"


def test_flatten_content_list_text_only():
    blocks = [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]
    assert _flatten_content(blocks) == "first\nsecond"


def test_flatten_content_strips_non_text_blocks():
    blocks = [
        {"type": "text", "text": "the prompt"},
        {"type": "image", "source": {}},
        {"type": "tool_use", "name": "Bash"},
        {"type": "tool_result", "content": "..."},
    ]
    result = _flatten_content(blocks)
    assert result == "the prompt"
    assert "image" not in result
    assert "tool" not in result


def test_build_messages_payload_appends_question():
    parents = [
        SimpleNamespace(role="user", content="parent 1"),
        SimpleNamespace(role="assistant", content="parent reply 1"),
    ]
    payload = _build_messages_payload(parents, "the btw question")
    assert payload[-1] == {"role": "user", "content": "the btw question"}
    assert len(payload) == 3


def test_build_messages_payload_caps_at_30():
    parents = [
        SimpleNamespace(role="user" if i % 2 == 0 else "assistant", content=f"m{i}")
        for i in range(50)
    ]
    payload = _build_messages_payload(parents, "btw?")
    assert len(payload) <= 31
    assert payload[-2]["content"] == "m49"


def test_build_messages_payload_filters_unknown_roles():
    parents = [
        SimpleNamespace(role="system", content="sys"),
        SimpleNamespace(role="user", content="u"),
        SimpleNamespace(role="assistant", content="a"),
        SimpleNamespace(role="tool", content="t"),
    ]
    payload = _build_messages_payload(parents, "btw?")
    roles = [m["role"] for m in payload]
    assert roles == ["user", "assistant", "user"]


def test_build_messages_payload_drops_empty_messages():
    parents = [
        SimpleNamespace(role="user", content=""),
        SimpleNamespace(role="user", content="real msg"),
    ]
    payload = _build_messages_payload(parents, "btw?")
    assert len(payload) == 2
    assert payload[0]["content"] == "real msg"


# ---------- command behavior ----------


@pytest.mark.asyncio
async def test_empty_args_shows_usage():
    cmd = BtwCommand(api_key="test")
    result = await cmd.execute("", _runtime())
    assert "Usage" in result.output


@pytest.mark.asyncio
async def test_basic_call_returns_response(monkeypatch):
    """Critical: /btw works on whatever provider the user configured.
    Test mocks the provider-agnostic helper, NOT Anthropic specifically.
    """
    _patch_complete_text(monkeypatch, "The mTLS difference: ...")
    cmd = BtwCommand(api_key="test")
    result = await cmd.execute("what's mTLS?", _runtime())
    assert "mTLS" in result.output
    assert "ephemeral" in result.output  # marker that result was non-persisted


@pytest.mark.asyncio
async def test_includes_parent_session_context(monkeypatch):
    mock = _patch_complete_text(monkeypatch)
    db = _FakeDB()
    db.messages["s1"] = [
        SimpleNamespace(role="user", content="we were debugging the auth flow"),
        SimpleNamespace(role="assistant", content="yes the JWT was expired"),
    ]
    cmd = BtwCommand(api_key="test")
    await cmd.execute("what was wrong with the JWT?", _runtime("s1", db))
    # Parent messages should be in the kwargs passed to complete_text
    sent_messages = mock.call_args.kwargs["messages"]
    contents = [m["content"] for m in sent_messages]
    assert any("auth flow" in c for c in contents)
    assert any("JWT was expired" in c for c in contents)
    assert sent_messages[-1]["content"] == "what was wrong with the JWT?"


@pytest.mark.asyncio
async def test_request_does_not_include_tools(monkeypatch):
    """The whole POINT of /btw is no tools — the helper signature must
    not even surface a `tools=` kwarg.
    """
    mock = _patch_complete_text(monkeypatch)
    cmd = BtwCommand(api_key="test")
    await cmd.execute("anything", _runtime())
    assert "tools" not in mock.call_args.kwargs


@pytest.mark.asyncio
async def test_works_outside_agent_loop_turn(monkeypatch):
    """No session context — still works, just without history."""
    _patch_complete_text(monkeypatch, "answer without context")
    cmd = BtwCommand(api_key="test")
    rt = RuntimeContext(custom={})
    result = await cmd.execute("hi", rt)
    assert "answer without context" in result.output


@pytest.mark.asyncio
async def test_does_not_persist_to_session_db(monkeypatch):
    """/btw must NOT call db.append_message — the result is ephemeral."""
    _patch_complete_text(monkeypatch)
    db = _FakeDB()
    db.messages["s1"] = [SimpleNamespace(role="user", content="prior")]
    initial_msg_count = len(db.messages["s1"])
    cmd = BtwCommand(api_key="test")
    await cmd.execute("question", _runtime("s1", db))
    assert len(db.messages["s1"]) == initial_msg_count


@pytest.mark.asyncio
async def test_api_failure_surfaces_error(monkeypatch):
    """Provider error → user-facing message, not a stack trace."""
    async def boom(**kwargs):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(
        "opencomputer.agent.slash_commands_impl.btw_cmd.complete_text",
        boom,
        raising=False,
    )
    monkeypatch.setattr(
        "opencomputer.agent.aux_llm.complete_text",
        boom,
        raising=False,
    )
    cmd = BtwCommand(api_key="test")
    result = await cmd.execute("anything", _runtime())
    assert "failed" in result.output.lower() or "RuntimeError" in result.output


@pytest.mark.asyncio
async def test_empty_response_is_surfaced_as_error(monkeypatch):
    """Provider returned empty text — surface a clear error."""
    _patch_complete_text(monkeypatch, "")
    cmd = BtwCommand(api_key="test")
    result = await cmd.execute("anything", _runtime())
    assert "no text content" in result.output.lower()


@pytest.mark.asyncio
async def test_works_with_non_anthropic_provider(monkeypatch):
    """Regression: simulate an OpenAI-only user (no ANTHROPIC_API_KEY).
    The helper resolves whatever provider the config points to; /btw
    succeeds because it never imports anthropic_client.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _patch_complete_text(monkeypatch, "from openai")
    cmd = BtwCommand(api_key=None)
    result = await cmd.execute("hi", _runtime())
    assert "from openai" in result.output


def test_metadata():
    cmd = BtwCommand(api_key="x")
    assert cmd.name == "btw"
    assert "ephemeral" in cmd.description.lower() or "side" in cmd.description.lower()
