"""ACP tool event schema builders.

Provides factory functions for the JSON payloads sent as ACP notifications
when the agent loop starts or completes a tool call.

These builders are consumed by ACPSession._run_conversation via the
tool_callback mechanism wired through AgentLoop.run_conversation.
"""

from __future__ import annotations

import uuid
from typing import Any


def make_tool_call_id() -> str:
    """Generate a unique tool call ID for ACP notifications."""
    return f"acp-tool-{uuid.uuid4().hex[:12]}"


def build_tool_start(
    tool_name: str,
    tool_call_id: str,
    args: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build payload for session/toolStart notification."""
    return {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "args": args or {},
    }


def build_tool_complete(
    tool_call_id: str,
    result: Any,
) -> dict[str, Any]:
    """Build payload for session/toolComplete notification."""
    return {
        "tool_call_id": tool_call_id,
        "result": str(result) if not isinstance(result, (str, dict, list, type(None))) else result,
    }


def build_tool_error(
    tool_call_id: str,
    error: str,
) -> dict[str, Any]:
    """Build payload for session/toolError notification."""
    return {
        "tool_call_id": tool_call_id,
        "error": error,
    }


__all__ = [
    "make_tool_call_id",
    "build_tool_start",
    "build_tool_complete",
    "build_tool_error",
]
