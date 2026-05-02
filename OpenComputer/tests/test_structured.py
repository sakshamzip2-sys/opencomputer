"""Tests for opencomputer.agent.structured.parse_structured."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field

from opencomputer.agent.structured import StructuredOutputError, parse_structured
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)


class _Reply(BaseModel):
    """Tiny test schema."""

    name: str = Field(min_length=1)
    age: int = Field(ge=0, le=200)


class _StubProvider(BaseProvider):
    name = "stub"
    default_model = "stub-1"

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.captured_kwargs: dict[str, Any] = {}

    async def complete(self, **kwargs: Any) -> ProviderResponse:
        self.captured_kwargs = kwargs
        return ProviderResponse(
            message=Message(role="assistant", content=self.response_text),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(self, **kwargs: Any):
        resp = await self.complete(**kwargs)
        yield StreamEvent(kind="done", response=resp)


@pytest.mark.asyncio
async def test_parse_structured_returns_validated_instance() -> None:
    """Happy path — provider returns valid JSON, helper returns model instance."""
    provider = _StubProvider('{"name": "Alice", "age": 30}')

    result = await parse_structured(
        response_model=_Reply,
        messages=[Message(role="user", content="describe Alice")],
        provider=provider,
        model="stub-1",
    )

    assert isinstance(result, _Reply)
    assert result.name == "Alice"
    assert result.age == 30


@pytest.mark.asyncio
async def test_parse_structured_passes_schema_to_provider() -> None:
    """The Pydantic schema must reach the provider via response_schema kwarg."""
    provider = _StubProvider('{"name": "X", "age": 1}')

    await parse_structured(
        response_model=_Reply,
        messages=[Message(role="user", content="hi")],
        provider=provider,
        model="stub-1",
    )

    schema_spec = provider.captured_kwargs["response_schema"]
    assert schema_spec["name"] == "_reply"
    assert schema_spec["schema"]["type"] == "object"
    assert "name" in schema_spec["schema"]["properties"]
    assert "age" in schema_spec["schema"]["properties"]


@pytest.mark.asyncio
async def test_parse_structured_raises_on_invalid_json() -> None:
    """Provider returned non-JSON text → StructuredOutputError."""
    provider = _StubProvider("This is not JSON at all.")

    with pytest.raises(StructuredOutputError, match="not valid JSON"):
        await parse_structured(
            response_model=_Reply,
            messages=[Message(role="user", content="hi")],
            provider=provider,
            model="stub-1",
        )


@pytest.mark.asyncio
async def test_parse_structured_raises_on_schema_violation() -> None:
    """JSON parses but field validation fails → StructuredOutputError."""
    # age missing — required field
    provider = _StubProvider('{"name": "Alice"}')

    with pytest.raises(StructuredOutputError, match="schema validation"):
        await parse_structured(
            response_model=_Reply,
            messages=[Message(role="user", content="hi")],
            provider=provider,
            model="stub-1",
        )


@pytest.mark.asyncio
async def test_parse_structured_raises_on_empty_content() -> None:
    """Provider returned empty string → StructuredOutputError."""
    provider = _StubProvider("")

    with pytest.raises(StructuredOutputError, match="empty content"):
        await parse_structured(
            response_model=_Reply,
            messages=[Message(role="user", content="hi")],
            provider=provider,
            model="stub-1",
        )


@pytest.mark.asyncio
async def test_parse_structured_uses_custom_name() -> None:
    """Caller-supplied name flows into response_schema.name."""
    provider = _StubProvider('{"name": "X", "age": 1}')

    await parse_structured(
        response_model=_Reply,
        messages=[Message(role="user", content="hi")],
        provider=provider,
        model="stub-1",
        name="my_custom_name",
    )

    assert provider.captured_kwargs["response_schema"]["name"] == "my_custom_name"
