"""WeatherExampleProvider — hardcoded-response demo provider.

This is a reference example demonstrating the scaffolder output shape,
NOT a real weather API. Every call returns the same stub text so the
plugin works offline with no API keys or network dependencies. Use it
as a starting point when building a real provider plugin.

See ``extensions/anthropic-provider/provider.py`` or
``extensions/openai-provider/provider.py`` for a production-style
implementation that actually calls an LLM backend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import (
    BaseProvider,
    ProviderResponse,
    StreamEvent,
    Usage,
)
from plugin_sdk.tool_contract import ToolSchema

#: Hardcoded reply returned for every prompt. Kept as a module-level
#: constant so tests can assert against it without duplicating the string.
DEMO_REPLY: str = "It's sunny and 72°F everywhere."


class WeatherExampleProvider(BaseProvider):
    """Demo provider — ignores inputs, always returns a sunny forecast."""

    name = "weather_example"
    default_model = "demo-weather-v1"

    def __init__(self, api_key: str | None = None) -> None:
        # api_key intentionally unused — this is a hardcoded demo.
        self.api_key = api_key

    async def complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
        stream: bool = False,
    ) -> ProviderResponse:
        """Return a canned ProviderResponse, regardless of the prompt."""
        reply = Message(role="assistant", content=DEMO_REPLY)
        return ProviderResponse(
            message=reply,
            stop_reason="end_turn",
            usage=Usage(),
        )

    async def stream_complete(
        self,
        *,
        model: str,
        messages: list[Message],
        system: str = "",
        tools: list[ToolSchema] | None = None,
        max_tokens: int = 4096,
        temperature: float = 1.0,
    ) -> AsyncIterator[StreamEvent]:
        """Stream the same canned response as a single text delta + done."""
        yield StreamEvent(kind="text_delta", text=DEMO_REPLY)
        final = await self.complete(
            model=model,
            messages=messages,
            system=system,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        yield StreamEvent(kind="done", response=final)


__all__ = ["WeatherExampleProvider", "DEMO_REPLY"]
