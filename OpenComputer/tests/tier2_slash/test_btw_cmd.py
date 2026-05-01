"""Tests for /btw — ephemeral side-question."""
import json
from types import SimpleNamespace

import httpx
import pytest

from opencomputer.agent.slash_commands_impl.btw_cmd import (
    BtwCommand,
    _build_messages_payload,
    _flatten_content,
)
from plugin_sdk.runtime_context import RuntimeContext


def _mock_response(text: str = "Quick answer.") -> dict:
    return {
        "id": "msg_x",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }


def _make_transport(response_text: str = "Quick answer.") -> httpx.MockTransport:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_mock_response(response_text))

    transport = httpx.MockTransport(handler)
    transport.captured = captured  # type: ignore[attr-defined]
    return transport


def _patch_btw_anthropic_client(monkeypatch, transport: httpx.MockTransport) -> None:
    """Stub the shared Anthropic client builder /btw uses to talk to the API.

    Post-refactor /btw goes through the same ``AsyncAnthropic`` SDK as
    chat (via ``opencomputer.agent.anthropic_client``), so tests inject
    the ``MockTransport`` into the SDK's internal httpx client by
    replacing the builder.
    """
    from anthropic import AsyncAnthropic

    def _stub(api_key: str, **_kwargs) -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key=api_key,
            http_client=httpx.AsyncClient(transport=transport, timeout=60.0),
        )

    monkeypatch.setattr(
        "opencomputer.agent.anthropic_client.build_anthropic_async_client",
        _stub,
    )


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
    # Critically — no 'image' / 'tool_use' / 'tool_result' label leaked
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
    # 30 parent + 1 btw = 31
    assert len(payload) <= 31
    # Most recent kept
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
    # Empty filtered out, real one kept + btw appended
    assert len(payload) == 2
    assert payload[0]["content"] == "real msg"


# ---------- command behavior ----------


@pytest.mark.asyncio
async def test_empty_args_shows_usage():
    cmd = BtwCommand(api_key="test")
    result = await cmd.execute("", _runtime())
    assert "Usage" in result.output


@pytest.mark.asyncio
async def test_no_api_key_returns_clear_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cmd = BtwCommand(api_key=None)
    result = await cmd.execute("a question", _runtime())
    assert "ANTHROPIC_API_KEY" in result.output


@pytest.mark.asyncio
async def test_basic_call_returns_response(monkeypatch):
    transport = _make_transport("The mTLS difference: ...")
    _patch_btw_anthropic_client(monkeypatch, transport)
    cmd = BtwCommand(api_key="test")
    result = await cmd.execute("what's mTLS?", _runtime())
    assert "mTLS" in result.output
    assert "ephemeral" in result.output  # marker that result was non-persisted


@pytest.mark.asyncio
async def test_includes_parent_session_context(monkeypatch):
    transport = _make_transport()
    _patch_btw_anthropic_client(monkeypatch, transport)
    db = _FakeDB()
    db.messages["s1"] = [
        SimpleNamespace(role="user", content="we were debugging the auth flow"),
        SimpleNamespace(role="assistant", content="yes the JWT was expired"),
    ]
    cmd = BtwCommand(api_key="test")
    await cmd.execute("what was wrong with the JWT?", _runtime("s1", db))
    sent = transport.captured["body"]
    # Parent messages should be in the request
    contents = [m["content"] for m in sent["messages"]]
    assert any("auth flow" in c for c in contents)
    assert any("JWT was expired" in c for c in contents)
    # And the btw question appended
    assert sent["messages"][-1]["content"] == "what was wrong with the JWT?"


@pytest.mark.asyncio
async def test_request_body_does_not_include_tools(monkeypatch):
    """The whole POINT of /btw is no tools."""
    transport = _make_transport()
    _patch_btw_anthropic_client(monkeypatch, transport)
    cmd = BtwCommand(api_key="test")
    await cmd.execute("anything", _runtime())
    sent = transport.captured["body"]
    assert "tools" not in sent


@pytest.mark.asyncio
async def test_works_outside_agent_loop_turn(monkeypatch):
    """No session context — still works, just without history."""
    transport = _make_transport("answer without context")
    _patch_btw_anthropic_client(monkeypatch, transport)
    cmd = BtwCommand(api_key="test")
    rt = RuntimeContext(custom={})  # no session_id/session_db
    result = await cmd.execute("hi", rt)
    assert "answer without context" in result.output


@pytest.mark.asyncio
async def test_does_not_persist_to_session_db(monkeypatch):
    """/btw must NOT call db.append_message or anything that would
    write to the session DB. The fake DB tracks reads only — if /btw
    tried to write, AttributeError would surface.
    """
    transport = _make_transport()
    _patch_btw_anthropic_client(monkeypatch, transport)
    db = _FakeDB()
    db.messages["s1"] = [SimpleNamespace(role="user", content="prior")]
    initial_msg_count = len(db.messages["s1"])
    cmd = BtwCommand(api_key="test")
    await cmd.execute("question", _runtime("s1", db))
    # Parent's message list unchanged
    assert len(db.messages["s1"]) == initial_msg_count


@pytest.mark.asyncio
async def test_api_failure_surfaces_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "rate limited"})

    transport = httpx.MockTransport(handler)
    _patch_btw_anthropic_client(monkeypatch, transport)
    cmd = BtwCommand(api_key="test")
    result = await cmd.execute("anything", _runtime())
    assert "failed" in result.output.lower() or "429" in result.output


@pytest.mark.asyncio
async def test_empty_response_is_surfaced_as_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        # Anthropic Message with empty content array — SDK accepts this
        # shape; the BtwCommand surfaces it as "no text content".
        return httpx.Response(200, json={
            "id": "msg_x", "type": "message", "role": "assistant",
            "model": "claude-haiku-4-5", "content": [],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 0},
        })

    transport = httpx.MockTransport(handler)
    _patch_btw_anthropic_client(monkeypatch, transport)
    cmd = BtwCommand(api_key="test")
    result = await cmd.execute("anything", _runtime())
    assert "no text content" in result.output.lower()


def test_metadata():
    cmd = BtwCommand(api_key="x")
    assert cmd.name == "btw"
    assert "ephemeral" in cmd.description.lower() or "side" in cmd.description.lower()
