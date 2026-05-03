"""OpenAI Responses API format conversion for OpenComputer.

Adapted from hermes-agent-2026.4.23/agent/codex_responses_adapter.py.
Hermes-specific imports stripped; uses OC plugin_sdk types throughout.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from plugin_sdk import Message, ToolCall, ToolSchema, Usage
from plugin_sdk.provider_contract import ProviderResponse

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_IDENTITY = "You are a helpful AI assistant."


def messages_to_responses_input(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert OC Message list to Responses API input items."""
    items: list[dict[str, Any]] = []
    for msg in messages:
        if msg.tool_call_id is not None:
            # Tool result → function_call_output
            items.append({
                "type": "function_call_output",
                "call_id": msg.tool_call_id,
                "output": msg.content or "",
            })
            continue
        if msg.tool_calls:
            for tc in msg.tool_calls:
                items.append({
                    "type": "function_call",
                    "call_id": tc.id or str(uuid.uuid4()),
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments or {}),
                })
            continue
        content_parts: list[dict[str, Any]] = []
        if msg.content:
            content_parts.append({"type": "input_text", "text": msg.content})
        items.append({
            "type": "message",
            "role": msg.role,
            "content": content_parts,
        })
    return items


def tools_to_responses_tools(schemas: list[ToolSchema]) -> list[dict[str, Any]]:
    """Convert OC ToolSchema list to Responses API tools array."""
    return [
        {
            "type": "function",
            "name": s.name,
            "description": s.description or "",
            "parameters": s.parameters or {},
            "strict": True,
        }
        for s in schemas
    ]


def responses_output_to_provider(raw: dict[str, Any]) -> ProviderResponse:
    """Convert Responses API output to OC ProviderResponse."""
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for item in raw.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") in ("output_text", "text"):
                    text_parts.append(part.get("text", ""))
        elif item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(
                id=item.get("call_id", ""),
                name=item.get("name", ""),
                arguments=args,
            ))

    usage_raw = raw.get("usage", {})
    usage = Usage(
        input_tokens=usage_raw.get("input_tokens", 0),
        output_tokens=usage_raw.get("output_tokens", 0),
    )
    msg = Message(
        role="assistant",
        content=" ".join(text_parts) or None,
        tool_calls=tool_calls or None,
    )
    return ProviderResponse(message=msg, usage=usage, stop_reason="end_turn")


__all__ = [
    "messages_to_responses_input",
    "tools_to_responses_tools",
    "responses_output_to_provider",
]
