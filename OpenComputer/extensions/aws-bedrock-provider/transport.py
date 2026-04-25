"""BedrockTransport — AWS Bedrock Converse API via boto3.

Mirrors hermes-agent v0.11's agent/transports/bedrock.py.

PR-C of ~/.claude/plans/replicated-purring-dewdrop.md.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import ProviderResponse, StreamEvent, Usage
from plugin_sdk.transports import NormalizedRequest, NormalizedResponse, TransportBase

logger = logging.getLogger(__name__)


class BedrockTransport(TransportBase):
    """AWS Bedrock Converse API transport.

    Auth: boto3 default credential chain (env / ~/.aws/credentials / IAM role).
    Region: AWS_REGION env or 'us-east-1' default.

    Limitations of v1:
    - Stream support: minimal (yields the full assembled response as one
      'done' event). Native chunk streaming is a future enhancement.
    - Tool use: passes through Bedrock's tool spec format; agents that
      use tools should pin to a Bedrock model that supports tool_use
      (Claude family on Bedrock, Llama 3.x).
    """

    name = "bedrock"

    def __init__(self, *, region_name: str | None = None) -> None:
        # Lazy-import boto3 — provider is opt-in via the [bedrock] extra
        try:
            import boto3
        except ImportError as exc:
            raise ImportError(
                "boto3 is required for BedrockTransport. "
                "Install with `pip install opencomputer[bedrock]` or "
                "`pip install boto3`."
            ) from exc
        self._region = region_name or os.environ.get("AWS_REGION", "us-east-1")
        self._client = boto3.client("bedrock-runtime", region_name=self._region)

    def format_request(self, req: NormalizedRequest) -> dict[str, Any]:
        """Convert NormalizedRequest -> Bedrock Converse API dict."""
        # Bedrock Converse API uses "messages" with role+content[]
        # System prompt is a separate top-level "system" field.
        messages = []
        for msg in req.messages:
            if msg.role == "system":
                # system messages are hoisted out
                continue
            role = "user" if msg.role == "user" else "assistant"
            messages.append({
                "role": role,
                "content": [{"text": msg.content}],
            })

        native: dict[str, Any] = {
            "modelId": req.model,
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": req.max_tokens,
                "temperature": req.temperature,
            },
        }
        # Combine req.system + any leading system messages
        sys_chunks = [m.content for m in req.messages if m.role == "system"]
        if req.system:
            sys_chunks.insert(0, req.system)
        if sys_chunks:
            native["system"] = [{"text": "\n\n".join(sys_chunks)}]

        # Tool config (Bedrock Converse format)
        if req.tools:
            tool_config = {
                "tools": [
                    {
                        "toolSpec": {
                            "name": t.name,
                            "description": getattr(t, "description", ""),
                            "inputSchema": {"json": getattr(t, "parameters", {})},
                        }
                    }
                    for t in req.tools
                ],
            }
            native["toolConfig"] = tool_config

        return native

    async def send(self, native_request: dict[str, Any]) -> Any:
        """Call Bedrock Converse API non-streaming."""
        # boto3 is sync — wrap in to_thread for async compatibility
        import asyncio
        return await asyncio.to_thread(self._client.converse, **native_request)

    async def send_stream(
        self, native_request: dict[str, Any]
    ) -> AsyncIterator[StreamEvent]:
        """Minimal stream impl: assemble full response, yield a single 'done' event.
        Future: yield text_delta events as Bedrock streaming chunks arrive."""
        raw = await self.send(native_request)
        normalized = self.parse_response(raw)
        # Yield a single done event with the full response
        yield StreamEvent(kind="done", response=normalized.provider_response)

    def parse_response(self, raw: Any) -> NormalizedResponse:
        """Bedrock Converse response -> NormalizedResponse."""
        # Bedrock response shape:
        # {"output": {"message": {"role": "...", "content": [...]}},
        #  "usage": {...}, "stopReason": "..."}
        output = raw.get("output", {})
        msg = output.get("message", {})
        content_blocks = msg.get("content", [])
        # Simple v1: extract first text block
        text = ""
        for block in content_blocks:
            if "text" in block:
                text = block["text"]
                break

        usage_raw = raw.get("usage", {})
        usage = Usage(
            input_tokens=int(usage_raw.get("inputTokens", 0)),
            output_tokens=int(usage_raw.get("outputTokens", 0)),
        )
        stop_reason = raw.get("stopReason", "end_turn")

        # Map Bedrock stop reasons to our naming
        if stop_reason == "tool_use":
            stop_reason = "tool_use"
        elif stop_reason == "max_tokens":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        provider_response = ProviderResponse(
            message=Message(role="assistant", content=text),
            stop_reason=stop_reason,
            usage=usage,
        )
        return NormalizedResponse(
            provider_response=provider_response,
            raw_native=raw,
        )
