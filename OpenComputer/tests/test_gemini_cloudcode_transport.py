"""Tests for the Gemini Cloud Code Assist transport.

Lives in extensions/gemini-oauth-provider/cloudcode_transport.py — handles
message translation (OC ↔ Gemini), generateContent + streamGenerateContent,
SSE parsing, and project-context-aware request envelope.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).parent.parent
_TRANSPORT_PY = (
    _REPO / "extensions" / "gemini-oauth-provider" / "cloudcode_transport.py"
)


def _load_transport():
    sys.modules.pop("cloudcode_transport_test", None)
    spec = importlib.util.spec_from_file_location(
        "cloudcode_transport_test", _TRANSPORT_PY
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["cloudcode_transport_test"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Message translation
# --------------------------------------------------------------------------- #

def test_build_contents_translates_user_message():
    from plugin_sdk.core import Message
    mod = _load_transport()
    contents, system = mod.build_gemini_contents(
        [Message(role="user", content="hello")]
    )
    assert contents == [{"role": "user", "parts": [{"text": "hello"}]}]
    assert system is None


def test_build_contents_translates_assistant_role_to_model():
    from plugin_sdk.core import Message
    mod = _load_transport()
    contents, _ = mod.build_gemini_contents(
        [Message(role="assistant", content="hi back")]
    )
    assert contents == [{"role": "model", "parts": [{"text": "hi back"}]}]


def test_build_contents_extracts_system_message_to_system_instruction():  # noqa: N802
    from plugin_sdk.core import Message
    mod = _load_transport()
    contents, system = mod.build_gemini_contents([
        Message(role="system", content="be helpful"),
        Message(role="user", content="hi"),
    ])
    assert system == {"role": "system", "parts": [{"text": "be helpful"}]}
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_build_contents_translates_tool_call_to_function_call():  # noqa: N802
    from plugin_sdk.core import Message, ToolCall
    mod = _load_transport()
    contents, _ = mod.build_gemini_contents([
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="call-1", name="read_file", arguments={"path": "/a"})
            ],
        ),
    ])
    assert len(contents) == 1
    parts = contents[0]["parts"]
    assert parts[0]["functionCall"]["name"] == "read_file"
    assert parts[0]["functionCall"]["args"] == {"path": "/a"}
    # Hermes-style sentinel signature so Code Assist accepts non-Code-Assist origins
    assert parts[0]["thoughtSignature"] == "skip_thought_signature_validator"


def test_build_contents_translates_tool_result_to_function_response():  # noqa: N802
    from plugin_sdk.core import Message
    mod = _load_transport()
    contents, _ = mod.build_gemini_contents([
        Message(
            role="tool",
            content="file contents here",
            tool_call_id="call-1",
            name="read_file",
        ),
    ])
    assert contents[0]["role"] == "user"
    fr = contents[0]["parts"][0]["functionResponse"]
    assert fr["name"] == "read_file"
    # Plain text wrapped as {"output": text}
    assert fr["response"] == {"output": "file contents here"}


def test_build_contents_skips_empty_assistant_message():
    """Gemini rejects parts:[] — empty assistant turns must be dropped."""
    from plugin_sdk.core import Message
    mod = _load_transport()
    contents, _ = mod.build_gemini_contents([
        Message(role="assistant", content=""),
        Message(role="user", content="hi"),
    ])
    # The empty assistant message is filtered out
    assert len(contents) == 1
    assert contents[0]["role"] == "user"


# --------------------------------------------------------------------------- #
# Tool schema translation
# --------------------------------------------------------------------------- #

def test_translate_tools_strips_schema_only_keys():
    from plugin_sdk.tool_contract import ToolSchema
    mod = _load_transport()
    tools = [
        ToolSchema(
            name="read_file",
            description="Read a file",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "$schema": "http://json-schema.org/draft-07/schema#",
                "additionalProperties": False,
            },
        )
    ]
    out = mod.translate_tools_to_gemini(tools)
    assert len(out) == 1
    decl = out[0]["functionDeclarations"][0]
    assert decl["name"] == "read_file"
    assert "$schema" not in decl["parameters"]
    assert "additionalProperties" not in decl["parameters"]


def test_translate_tools_returns_empty_for_none():
    mod = _load_transport()
    assert mod.translate_tools_to_gemini(None) == []
    assert mod.translate_tools_to_gemini([]) == []


# --------------------------------------------------------------------------- #
# Request envelope
# --------------------------------------------------------------------------- #

def test_wrap_request_envelope():
    mod = _load_transport()
    inner = {"contents": [{"role": "user", "parts": [{"text": "x"}]}]}
    body = mod.wrap_code_assist_request(
        project_id="proj-x",
        model="gemini-2.5-pro",
        inner_request=inner,
        user_prompt_id="prompt-id-fixed",
    )
    assert body["project"] == "proj-x"
    assert body["model"] == "gemini-2.5-pro"
    assert body["user_prompt_id"] == "prompt-id-fixed"
    assert body["request"] == inner


def test_wrap_request_generates_uuid_when_no_prompt_id():
    mod = _load_transport()
    body = mod.wrap_code_assist_request(
        project_id="p", model="m", inner_request={}
    )
    assert body["user_prompt_id"]  # auto-generated
    assert len(body["user_prompt_id"]) >= 8


# --------------------------------------------------------------------------- #
# Response translation
# --------------------------------------------------------------------------- #

def test_translate_response_extracts_text_and_usage():
    mod = _load_transport()
    response = {
        "response": {
            "candidates": [{
                "content": {"parts": [{"text": "the answer"}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 7,
                "totalTokenCount": 19,
            },
        }
    }
    pr = mod.translate_response(response, model="gemini-2.5-pro")
    assert pr.message.content == "the answer"
    assert pr.stop_reason == "end_turn"
    assert pr.usage.input_tokens == 12
    assert pr.usage.output_tokens == 7


def test_translate_response_extracts_tool_call():
    mod = _load_transport()
    response = {
        "response": {
            "candidates": [{
                "content": {"parts": [
                    {"functionCall": {
                        "name": "read_file",
                        "args": {"path": "/a"},
                    }},
                ]},
                "finishReason": "STOP",
            }],
        }
    }
    pr = mod.translate_response(response, model="gemini-2.5-pro")
    assert pr.message.tool_calls
    tc = pr.message.tool_calls[0]
    assert tc.name == "read_file"
    assert tc.arguments == {"path": "/a"}
    assert pr.stop_reason == "tool_use"


def test_translate_response_handles_empty_candidates():
    mod = _load_transport()
    pr = mod.translate_response({"response": {"candidates": []}}, model="m")
    assert pr.message.content == ""


def test_translate_finish_reason_max_tokens():
    mod = _load_transport()
    assert mod.map_finish_reason("MAX_TOKENS") == "max_tokens"
    assert mod.map_finish_reason("STOP") == "end_turn"
    assert mod.map_finish_reason("SAFETY") == "content_filter"
    assert mod.map_finish_reason("RECITATION") == "content_filter"


# --------------------------------------------------------------------------- #
# SSE parsing
# --------------------------------------------------------------------------- #

def test_parse_sse_lines_yields_data_payloads():
    mod = _load_transport()
    raw = (
        b'data: {"response":{"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}}\n'
        b"\n"
        b"data: [DONE]\n"
    )
    chunks = list(mod.iter_sse_payloads(raw))
    assert len(chunks) == 1
    assert chunks[0]["response"]["candidates"][0]["content"]["parts"][0]["text"] == "hi"


def test_parse_sse_lines_terminates_on_done():
    mod = _load_transport()
    raw = b"data: [DONE]\n\n"
    chunks = list(mod.iter_sse_payloads(raw))
    assert chunks == []


def test_parse_sse_skips_non_data_lines():
    """Comments / event names should be ignored."""
    mod = _load_transport()
    raw = (
        b": keepalive\n"
        b'event: message\n'
        b'data: {"response":{}}\n\n'
    )
    chunks = list(mod.iter_sse_payloads(raw))
    assert chunks == [{"response": {}}]


# --------------------------------------------------------------------------- #
# CloudCodeTransport — end-to-end with mocked httpx
# --------------------------------------------------------------------------- #

def test_transport_complete_calls_correct_endpoint():
    from plugin_sdk.core import Message
    mod = _load_transport()

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["body"] = kwargs.get("json", {})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "response": {
                "candidates": [{
                    "content": {"parts": [{"text": "hi"}]},
                    "finishReason": "STOP",
                }],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            }
        }
        return resp

    transport = mod.CloudCodeTransport(
        access_token_provider=lambda: "at-x",
        project_id_provider=lambda: "proj-x",
    )
    with patch("httpx.post", side_effect=fake_post):
        import asyncio
        pr = asyncio.run(transport.complete(
            model="gemini-2.5-pro",
            messages=[Message(role="user", content="hi")],
        ))

    assert captured["url"].endswith("/v1internal:generateContent")
    assert captured["headers"]["Authorization"] == "Bearer at-x"
    assert captured["body"]["project"] == "proj-x"
    assert captured["body"]["model"] == "gemini-2.5-pro"
    assert pr.message.content == "hi"


def test_transport_raises_on_401():
    from plugin_sdk.core import Message
    mod = _load_transport()

    bad = MagicMock()
    bad.status_code = 401
    bad.text = '{"error":{"code":401}}'

    transport = mod.CloudCodeTransport(
        access_token_provider=lambda: "bad",
        project_id_provider=lambda: "p",
    )
    with patch("httpx.post", return_value=bad):
        import asyncio
        with pytest.raises(RuntimeError, match="unauthorized"):
            asyncio.run(transport.complete(
                model="m",
                messages=[Message(role="user", content="x")],
            ))
