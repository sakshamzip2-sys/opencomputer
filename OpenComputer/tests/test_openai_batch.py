"""Tests for OpenAI provider batch implementation (Subsystem E follow-up)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BatchRequest


def _load_openai_provider():
    repo_root = Path(__file__).resolve().parent.parent
    provider_path = repo_root / "extensions" / "openai-provider" / "provider.py"
    module_name = f"_openai_batch_test_{id(provider_path)}"
    spec = importlib.util.spec_from_file_location(module_name, provider_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_openai_submit_batch_uploads_jsonl_then_creates_batch(monkeypatch) -> None:
    """submit_batch must upload JSONL via files.create then call batches.create."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()

    uploaded_files: list = []
    created_batches: list = []

    async def _fake_files_create(**kwargs):
        uploaded_files.append(kwargs)
        f = MagicMock()
        f.id = "file-abc123"
        return f

    async def _fake_batches_create(**kwargs):
        created_batches.append(kwargs)
        b = MagicMock()
        b.id = "batch_xyz789"
        return b

    with patch.object(provider.client.files, "create", side_effect=_fake_files_create), \
         patch.object(provider.client.batches, "create", side_effect=_fake_batches_create):
        batch_id = await provider.submit_batch(
            [
                BatchRequest(
                    custom_id="r1",
                    messages=[Message(role="user", content="hi")],
                    model="gpt-4o",
                    max_tokens=100,
                ),
            ]
        )

    assert batch_id == "batch_xyz789"
    # File upload happened
    assert len(uploaded_files) == 1
    assert uploaded_files[0]["purpose"] == "batch"
    # Batch creation referenced the uploaded file
    assert len(created_batches) == 1
    assert created_batches[0]["input_file_id"] == "file-abc123"
    assert created_batches[0]["endpoint"] == "/v1/chat/completions"


@pytest.mark.asyncio
async def test_openai_submit_batch_carries_effort_and_schema(monkeypatch) -> None:
    """Per-request runtime_extras + response_schema must reach JSONL body."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()

    captured_jsonl: list[bytes] = []

    async def _fake_files_create(**kwargs):
        # Read the uploaded file body.
        f_arg = kwargs["file"]
        # f_arg is a tuple (filename, BytesIO)
        _, body = f_arg
        captured_jsonl.append(body.read())
        f = MagicMock()
        f.id = "file-x"
        return f

    async def _fake_batches_create(**kwargs):
        b = MagicMock()
        b.id = "batch_x"
        return b

    schema = {
        "type": "object",
        "properties": {"foo": {"type": "string"}},
        "required": ["foo"],
    }
    with patch.object(provider.client.files, "create", side_effect=_fake_files_create), \
         patch.object(provider.client.batches, "create", side_effect=_fake_batches_create):
        await provider.submit_batch(
            [
                BatchRequest(
                    custom_id="r2",
                    messages=[Message(role="user", content="x")],
                    model="gpt-4o",
                    runtime_extras={"reasoning_effort": "low"},
                    response_schema={"name": "test", "schema": schema},
                ),
            ]
        )

    body_text = captured_jsonl[0].decode("utf-8").strip()
    entry = json.loads(body_text)
    body = entry["body"]
    assert entry["custom_id"] == "r2"
    assert body["reasoning_effort"] == "low"
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["response_format"]["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_openai_get_batch_results_processing(monkeypatch) -> None:
    """In-progress batch returns processing placeholder."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()

    fake_batch = MagicMock()
    fake_batch.status = "in_progress"

    async def _fake_retrieve(batch_id):
        return fake_batch

    with patch.object(
        provider.client.batches, "retrieve", side_effect=_fake_retrieve
    ):
        results = await provider.get_batch_results("batch_x")

    assert len(results) == 1
    assert results[0].status == "processing"


@pytest.mark.asyncio
async def test_openai_get_batch_results_completed(monkeypatch) -> None:
    """Completed batch downloads output JSONL and translates to BatchResult."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    mod = _load_openai_provider()
    provider = mod.OpenAIProvider()

    fake_batch = MagicMock()
    fake_batch.status = "completed"
    fake_batch.output_file_id = "file-output-1"

    output_jsonl = (
        json.dumps(
            {
                "custom_id": "r1",
                "response": {
                    "body": {
                        "choices": [
                            {
                                "message": {"content": "Hello back"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                    }
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "custom_id": "r2",
                "error": {"message": "rate limit", "code": "rate_limit"},
            }
        )
    )

    fake_content = MagicMock()
    fake_content.text = output_jsonl

    async def _fake_retrieve(batch_id):
        return fake_batch

    async def _fake_content(file_id):
        return fake_content

    with patch.object(
        provider.client.batches, "retrieve", side_effect=_fake_retrieve
    ), patch.object(
        provider.client.files, "content", side_effect=_fake_content
    ):
        results = await provider.get_batch_results("batch_x")

    assert len(results) == 2
    by_id = {r.custom_id: r for r in results}
    assert by_id["r1"].status == "succeeded"
    assert "Hello back" in by_id["r1"].response.message.content
    assert by_id["r1"].response.usage.input_tokens == 5
    assert by_id["r2"].status == "errored"
    assert "rate limit" in by_id["r2"].error
