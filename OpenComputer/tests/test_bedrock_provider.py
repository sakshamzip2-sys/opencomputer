"""PR-C: BedrockTransport + BedrockProvider tests with mocked boto3."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_boto3(monkeypatch):
    """Patch boto3 client so tests don't require real AWS creds."""
    mock_client = MagicMock()
    mock_boto3_mod = MagicMock()
    mock_boto3_mod.client.return_value = mock_client
    # Inject before anyone imports boto3
    sys.modules["boto3"] = mock_boto3_mod
    yield mock_client
    # Cleanup: don't leave the mock in sys.modules between tests
    sys.modules.pop("boto3", None)
    # Also evict the transport module so it re-imports boto3 cleanly next time
    sys.modules.pop("extensions.aws_bedrock_provider.transport", None)
    sys.modules.pop("extensions.aws_bedrock_provider.provider", None)


def test_bedrock_transport_instantiates_with_mock_boto3(mock_boto3):
    # Evict cached module so it picks up the mocked boto3
    sys.modules.pop("extensions.aws_bedrock_provider.transport", None)
    from extensions.aws_bedrock_provider.transport import BedrockTransport

    t = BedrockTransport(region_name="us-east-1")
    assert t.name == "bedrock"
    assert t._region == "us-east-1"


def test_bedrock_transport_format_request_basic(mock_boto3):
    sys.modules.pop("extensions.aws_bedrock_provider.transport", None)
    from extensions.aws_bedrock_provider.transport import BedrockTransport

    from plugin_sdk.core import Message
    from plugin_sdk.transports import NormalizedRequest

    t = BedrockTransport()
    req = NormalizedRequest(
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        messages=[Message(role="user", content="hello")],
        system="You are helpful.",
        max_tokens=1024,
        temperature=0.7,
    )
    native = t.format_request(req)
    assert native["modelId"] == req.model
    assert native["inferenceConfig"]["maxTokens"] == 1024
    assert native["inferenceConfig"]["temperature"] == 0.7
    assert "system" in native
    assert native["system"] == [{"text": "You are helpful."}]
    assert len(native["messages"]) == 1
    assert native["messages"][0]["role"] == "user"


def test_bedrock_transport_parse_response_basic(mock_boto3):
    sys.modules.pop("extensions.aws_bedrock_provider.transport", None)
    from extensions.aws_bedrock_provider.transport import BedrockTransport

    t = BedrockTransport()
    raw = {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": "Hello back!"}],
            },
        },
        "usage": {"inputTokens": 10, "outputTokens": 5},
        "stopReason": "end_turn",
    }
    normalized = t.parse_response(raw)
    pr = normalized.provider_response
    assert pr.message.content == "Hello back!"
    assert pr.usage.input_tokens == 10
    assert pr.usage.output_tokens == 5
    assert pr.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_bedrock_provider_complete_roundtrip(mock_boto3):
    sys.modules.pop("extensions.aws_bedrock_provider.transport", None)
    sys.modules.pop("extensions.aws_bedrock_provider.provider", None)
    from extensions.aws_bedrock_provider.provider import BedrockProvider

    from plugin_sdk.core import Message  # noqa: PLC0415

    mock_boto3.converse.return_value = {
        "output": {"message": {"role": "assistant", "content": [{"text": "OK"}]}},
        "usage": {"inputTokens": 3, "outputTokens": 1},
        "stopReason": "end_turn",
    }
    provider = BedrockProvider(region_name="us-east-1")
    result = await provider.complete(
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        messages=[Message(role="user", content="hi")],
    )
    assert result.message.content == "OK"
    assert mock_boto3.converse.called


def test_bedrock_provider_class_metadata(mock_boto3):
    sys.modules.pop("extensions.aws_bedrock_provider.transport", None)
    sys.modules.pop("extensions.aws_bedrock_provider.provider", None)
    from extensions.aws_bedrock_provider.provider import BedrockProvider

    assert BedrockProvider.name == "aws-bedrock"
    assert "claude" in BedrockProvider.default_model.lower()


def test_bedrock_transport_raises_clear_error_when_boto3_missing():
    """If boto3 isn't installed, instantiating BedrockTransport raises a clear ImportError."""
    # Evict cached module so the import inside __init__ runs fresh
    sys.modules.pop("extensions.aws_bedrock_provider.transport", None)

    saved = sys.modules.pop("boto3", None)
    try:
        with patch.dict("sys.modules", {"boto3": None}):
            # Force fresh import with boto3 unavailable
            sys.modules.pop("extensions.aws_bedrock_provider.transport", None)
            from extensions.aws_bedrock_provider.transport import BedrockTransport

            with pytest.raises(ImportError, match="boto3 is required"):
                BedrockTransport()
    finally:
        if saved is not None:
            sys.modules["boto3"] = saved
        sys.modules.pop("extensions.aws_bedrock_provider.transport", None)
