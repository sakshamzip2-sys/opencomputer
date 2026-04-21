"""
Tool contract — what plugin authors implement to add a new tool.

A tool is any callable the agent can invoke: Read, Write, Bash, etc.
Plugins can add new ones by subclassing `BaseTool` and registering
via `register_plugin(..., tools=[MyTool])`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from plugin_sdk.core import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """OpenAI-compatible JSON schema for a tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object

    def to_openai_format(self) -> dict[str, Any]:
        """Convert to the dict format the OpenAI API expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_format(self) -> dict[str, Any]:
        """Convert to the dict format the Anthropic API expects."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


class BaseTool(ABC):
    """Base class for a tool. Subclass and implement `schema` + `execute`."""

    #: Whether this tool is safe to run in parallel with other parallel-safe tools.
    parallel_safe: bool = False

    #: Maximum size of the result string (longer is truncated with a notice).
    max_result_size: int = 100_000

    @property
    @abstractmethod
    def schema(self) -> ToolSchema:
        """Return the JSON schema describing this tool's input."""
        ...

    @abstractmethod
    async def execute(self, call: ToolCall) -> ToolResult:
        """Actually run the tool. Must handle its own errors — never raise."""
        ...


__all__ = ["ToolSchema", "BaseTool"]
