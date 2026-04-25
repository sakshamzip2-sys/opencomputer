"""BedrockProvider — composes BedrockTransport with BaseProvider.

PR-C of ~/.claude/plans/replicated-purring-dewdrop.md.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, StreamEvent
from plugin_sdk.tool_contract import ToolSchema
from plugin_sdk.transports import NormalizedRequest, TransportBase

logger = logging.getLogger(__name__)


class BedrockProvider(BaseProvider):
    """AWS Bedrock provider via the Converse API.

    Construction:
        provider = BedrockProvider()  # uses AWS env / IAM
        provider = BedrockProvider(region_name="us-west-2")

    Default model: anthropic.claude-3-5-sonnet-20241022-v2:0
    """

    name = "aws-bedrock"
    default_model = "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def __init__(
        self,
        *,
        region_name: str | None = None,
        transport: TransportBase | None = None,
    ) -> None:
        if transport is None:
            # Lazy import to defer boto3 dependency
            from extensions.aws_bedrock_provider.transport import BedrockTransport
            transport = BedrockTransport(region_name=region_name)
        self._transport = transport

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
        req = NormalizedRequest(
            model=model,
            messages=messages,
            system=system,
            tools=tuple(tools or ()),
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
        )
        native = self._transport.format_request(req)
        raw = await self._transport.send(native)
        normalized = self._transport.parse_response(raw)
        return normalized.provider_response

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
        req = NormalizedRequest(
            model=model,
            messages=messages,
            system=system,
            tools=tuple(tools or ()),
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )
        native = self._transport.format_request(req)
        async for event in self._transport.send_stream(native):
            yield event


__all__ = ["BedrockProvider"]
