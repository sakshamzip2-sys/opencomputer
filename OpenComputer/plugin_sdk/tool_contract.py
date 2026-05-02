"""
Tool contract — what plugin authors implement to add a new tool.

A tool is any callable the agent can invoke: Read, Write, Bash, etc.
Plugins can add new ones by subclassing `BaseTool` and registering
via `register_plugin(..., tools=[MyTool])`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from plugin_sdk.consent import CapabilityClaim
from plugin_sdk.core import ToolCall, ToolResult


@dataclass(frozen=True, slots=True)
class ToolSchema:
    """OpenAI-compatible JSON schema for a tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema object

    #: Item 3 (2026-05-02): when True, ``to_anthropic_format`` emits
    #: ``"strict": True`` so Anthropic enforces schema validation on
    #: tool inputs (no missing required fields, no extra fields, no
    #: type mismatches). Defaults False for backwards-compat — every
    #: existing ``ToolSchema(...)`` constructor call continues to work.
    #: The agent loop sets this from the tool's ``BaseTool.strict_mode``
    #: when building the request.
    strict: bool = False

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
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
        if self.strict:
            out["strict"] = True
        return out


class BaseTool(ABC):
    """Base class for a tool. Subclass and implement `schema` + `execute`."""

    #: Whether this tool is safe to run in parallel with other parallel-safe tools.
    parallel_safe: bool = False

    #: Maximum size of the result string (longer is truncated with a notice).
    max_result_size: int = 100_000

    #: F1 (Sub-project F): capabilities this tool needs the user to have
    #: granted. Empty list (default) means unprivileged — no gate check.
    #: Subclasses SHOULD override with a tuple (not list) to avoid the
    #: mutable-default-class-attribute footgun.
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = ()

    #: Item 3 (2026-05-02): when True, the tool's schema is sent to
    #: Anthropic with ``strict: true`` so the API enforces input
    #: validation (no missing required fields, no extra fields, no type
    #: mismatches). Defaults False because most existing tool schemas
    #: lack ``additionalProperties: false`` and would reject calls that
    #: previously worked. Tools opt-in by setting ``strict_mode = True``
    #: AFTER auditing their parameters dict for strict-compatibility.
    strict_mode: ClassVar[bool] = False

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
