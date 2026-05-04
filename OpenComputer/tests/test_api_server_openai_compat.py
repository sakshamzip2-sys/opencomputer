"""Tests for the OpenAI-compatible /v1/chat/completions route on api-server."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_openai_format():
    spec_path = (
        Path(__file__).parent.parent
        / "extensions"
        / "api-server"
        / "openai_format.py"
    )
    spec = importlib.util.spec_from_file_location(
        "api_server_openai_format_test", spec_path
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_server_openai_format_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_openai_to_oc_messages_basic():
    fmt = _load_openai_format()
    out = fmt.openai_to_oc_messages([
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
    ])
    assert len(out) == 2
    assert out[0]["role"] == "system"
    assert out[1]["content"] == "Hello"


def test_openai_to_oc_messages_multimodal_extracts_text():
    fmt = _load_openai_format()
    out = fmt.openai_to_oc_messages([
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this:"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
                {"type": "text", "text": "in one word."},
            ],
        }
    ])
    assert out[0]["content"] == "Describe this:\nin one word."


def test_oc_response_to_openai_format():
    fmt = _load_openai_format()
    response = fmt.oc_response_to_openai(
        "Hi there!",
        model="opencomputer",
        input_tokens=5,
        output_tokens=2,
    )
    assert response["object"] == "chat.completion"
    assert response["id"].startswith("chatcmpl-")
    assert response["model"] == "opencomputer"
    assert response["choices"][0]["message"]["role"] == "assistant"
    assert response["choices"][0]["message"]["content"] == "Hi there!"
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["usage"]["prompt_tokens"] == 5
    assert response["usage"]["completion_tokens"] == 2
    assert response["usage"]["total_tokens"] == 7


def test_streaming_delta_chunk_shape():
    fmt = _load_openai_format()
    chunk = fmt.streaming_delta_chunk("chatcmpl-abc", "opencomputer", "Hello")
    parsed = json.loads(chunk)
    assert parsed["object"] == "chat.completion.chunk"
    assert parsed["id"] == "chatcmpl-abc"
    assert parsed["choices"][0]["delta"]["content"] == "Hello"
    assert parsed["choices"][0]["finish_reason"] is None


def test_streaming_final_chunk_shape():
    fmt = _load_openai_format()
    chunk = fmt.streaming_final_chunk("chatcmpl-abc", "opencomputer")
    parsed = json.loads(chunk)
    assert parsed["choices"][0]["finish_reason"] == "stop"
    assert parsed["choices"][0]["delta"] == {}


def test_oc_response_id_uniqueness():
    """Two calls produce different ids."""
    fmt = _load_openai_format()
    a = fmt.oc_response_to_openai("x")
    b = fmt.oc_response_to_openai("x")
    assert a["id"] != b["id"]


def test_empty_messages_list_passes_through():
    fmt = _load_openai_format()
    assert fmt.openai_to_oc_messages([]) == []
    assert fmt.openai_to_oc_messages(None) == []
