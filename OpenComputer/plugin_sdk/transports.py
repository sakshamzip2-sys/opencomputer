"""Transport ABC — pluggable provider format conversion + HTTP transport.

Mirrors hermes-agent v0.11's agent/transports/ pattern. Each provider
that uses the Transport ABC owns its own format-conversion + API-shape
implementation. Plugins that don't want to use this layer continue to
inherit from BaseProvider directly (the existing pattern is unchanged).

PR-C of ~/.claude/plans/replicated-purring-dewdrop.md.

Reference:
- sources/hermes-agent-2026.4.23/agent/transports/base.py
- sources/hermes-agent-2026.4.23/agent/transports/types.py
- sources/hermes-agent-2026.4.23/agent/transports/{anthropic,bedrock,chat_completions,codex}.py
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import ProviderResponse, StreamEvent
from plugin_sdk.tool_contract import ToolSchema


@dataclass(frozen=True, slots=True)
class NormalizedRequest:
    """Provider-agnostic request shape. Each TransportBase subclass converts
    this into the provider's native API format (Messages API, Chat Completions,
    Bedrock Converse, Responses API, etc.).

    Mirrors hermes' transports/types.py NormalizedRequest.
    """

    model: str
    messages: list[Message]
    system: str = ""
    tools: tuple[ToolSchema, ...] = ()
    max_tokens: int = 4096
    temperature: float = 1.0
    stream: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedResponse:
    """Provider-agnostic response shape. Each TransportBase subclass converts
    its native response into this. Wraps the existing ProviderResponse for
    backwards compat — providers that use TransportBase still satisfy
    BaseProvider's `complete() -> ProviderResponse` contract.
    """

    provider_response: ProviderResponse
    raw_native: Any = None  # provider-specific raw payload, for debugging


class TransportBase(ABC):
    """Pluggable transport layer.

    Implementations:
    - Format conversion: NormalizedRequest -> provider-native dict
    - HTTP transport: native dict -> raw response (via httpx, boto3, etc.)
    - Response parsing: raw response -> NormalizedResponse

    A Provider plugin that uses Transport composes one TransportBase
    subclass + the BaseProvider ABC. The provider's BaseProvider.complete
    delegates to TransportBase.send + .parse_response.
    """

    name: str = ""
    """Stable identifier for the transport (e.g. 'anthropic', 'bedrock')."""

    @abstractmethod
    def format_request(self, req: NormalizedRequest) -> dict[str, Any]:
        """Convert a NormalizedRequest into the provider's native request dict."""
        ...

    @abstractmethod
    async def send(self, native_request: dict[str, Any]) -> Any:
        """Send the native request via HTTP/SDK. Return the raw response.

        For non-streaming: return the full response payload.
        For streaming: caller uses send_stream() instead — this method handles non-stream only.
        """
        ...

    @abstractmethod
    async def send_stream(self, native_request: dict[str, Any]) -> AsyncIterator[StreamEvent]:
        """Send the native request as a stream. Yield StreamEvent objects.
        Final yielded event has kind='done' and carries the complete ProviderResponse."""
        ...

    @abstractmethod
    def parse_response(self, raw: Any) -> NormalizedResponse:
        """Convert a native non-stream response into a NormalizedResponse.

        Streaming responses go through send_stream + the events carry parsed
        text/tool_calls; the final 'done' event carries the assembled NormalizedResponse.
        """
        ...


__all__ = [
    "NormalizedRequest",
    "NormalizedResponse",
    "TransportBase",
]
