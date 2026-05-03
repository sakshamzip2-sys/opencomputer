"""Tests for the Codex (OpenAI Responses API) provider plugin."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from plugin_sdk import Message, ToolCall, ToolSchema

_CODEX_DIR = Path(__file__).parent.parent / "extensions" / "codex-provider"
# Add codex-provider to sys.path so flat imports inside it work
sys.path.insert(0, str(_CODEX_DIR))


def _load_codex_plugin():
    """Load codex plugin.py via importlib to avoid sys.modules name collision."""
    spec = importlib.util.spec_from_file_location(
        "codex_provider_plugin", _CODEX_DIR / "plugin.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_messages_to_responses_input_basic():
    from codex_responses_adapter import messages_to_responses_input

    msgs = [Message(role="user", content="Hello")]
    items = messages_to_responses_input(msgs)
    assert items[0]["type"] == "message"
    assert items[0]["role"] == "user"
    assert any(p["type"] == "input_text" for p in items[0]["content"])


def test_messages_to_responses_input_assistant():
    from codex_responses_adapter import messages_to_responses_input

    msgs = [Message(role="assistant", content="Hi there")]
    items = messages_to_responses_input(msgs)
    assert items[0]["role"] == "assistant"
    assert items[0]["type"] == "message"


def test_tool_calls_in_messages():
    from codex_responses_adapter import messages_to_responses_input

    tc = ToolCall(id="call_1", name="read_file", arguments={"path": "/tmp/x"})
    msgs = [Message(role="assistant", content=None, tool_calls=[tc])]
    items = messages_to_responses_input(msgs)
    types = [i.get("type") for i in items]
    assert "function_call" in types


def test_tool_result_message():
    from codex_responses_adapter import messages_to_responses_input

    msgs = [Message(role="tool", content="result text", tool_call_id="call_1")]
    items = messages_to_responses_input(msgs)
    assert items[0]["type"] == "function_call_output"
    assert items[0]["call_id"] == "call_1"
    assert items[0]["output"] == "result text"


def test_tools_to_responses_tools():
    from codex_responses_adapter import tools_to_responses_tools

    schema = ToolSchema(
        name="read_file",
        description="Read a file",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    tools = tools_to_responses_tools([schema])
    assert tools[0]["type"] == "function"
    assert tools[0]["name"] == "read_file"
    assert tools[0]["strict"] is True


def test_responses_output_to_provider_response():
    from codex_responses_adapter import responses_output_to_provider

    raw = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done"}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    resp = responses_output_to_provider(raw)
    assert resp.message.content == "Done"
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 5


def test_responses_output_with_tool_call():
    import json

    from codex_responses_adapter import responses_output_to_provider

    raw = {
        "output": [
            {
                "type": "function_call",
                "call_id": "call_abc",
                "name": "bash",
                "arguments": json.dumps({"command": "echo hi"}),
            }
        ],
        "usage": {"input_tokens": 5, "output_tokens": 2},
    }
    resp = responses_output_to_provider(raw)
    assert resp.message.tool_calls is not None
    assert resp.message.tool_calls[0].name == "bash"
    assert resp.message.tool_calls[0].id == "call_abc"


@pytest.mark.asyncio
async def test_codex_provider_complete_mocked():
    from unittest.mock import AsyncMock, MagicMock, patch

    CodexProvider = _load_codex_plugin().CodexProvider

    mock_response = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello from codex"}],
            }
        ],
        "usage": {"input_tokens": 3, "output_tokens": 5},
    }

    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = lambda: mock_response
            mock_client.post = AsyncMock(return_value=mock_resp)

            provider = CodexProvider()
            result = await provider.complete(
                model="codex-mini-latest",
                messages=[Message(role="user", content="Hello")],
            )
    assert result.message.content == "Hello from codex"
