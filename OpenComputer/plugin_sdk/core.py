"""
Core public types — the canonical vocabulary plugins use.

These types are the STABLE contract. Changes here are breaking changes
and require a major version bump of plugin_sdk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

# ─── Message / conversation primitives ─────────────────────────────────

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class Message:
    """A single conversation message — canonical form used everywhere internally."""

    role: Role
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None
    name: str | None = None  # for tool messages, the tool name
    reasoning: str | None = None  # extended thinking, if supported


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A request from the model to invoke a tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The result of executing a tool call."""

    tool_call_id: str
    content: str
    is_error: bool = False


# ─── Platform / channel primitives ─────────────────────────────────────


class Platform(str, Enum):
    """Supported messaging platforms. Plugins can register new ones."""

    CLI = "cli"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    WHATSAPP = "whatsapp"
    SIGNAL = "signal"
    IMESSAGE = "imessage"
    WEB = "web"


@dataclass(frozen=True, slots=True)
class MessageEvent:
    """Platform-agnostic inbound message — the common format produced by every adapter."""

    platform: Platform
    chat_id: str
    user_id: str
    text: str
    timestamp: float  # unix timestamp
    attachments: list[str] = field(default_factory=list)  # file paths or URLs
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SendResult:
    """Outbound delivery result from a channel adapter."""

    success: bool
    message_id: str | None = None
    error: str | None = None


# ─── Plugin manifest ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PluginManifest:
    """Metadata for a plugin — parsed from plugin.json at discovery time."""

    id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    homepage: str = ""
    license: str = "MIT"
    kind: Literal["channel", "provider", "tool", "skill", "mixed"] = "mixed"
    entry: str = ""  # path to the entry module, relative to plugin root
    # Phase 14.C: profile scoping. None or ["*"] means "any profile can
    # opt in" (most permissive, backward-compatible default). A list of
    # profile names restricts the plugin to those profiles only.
    profiles: tuple[str, ...] | None = None
    # Phase 14.C: single-instance plugins (e.g. plugins that own a bot
    # token) can only belong to ONE profile at a time. Core tracks a
    # lock in ~/.opencomputer/.locks/<plugin-id> when this is True.
    single_instance: bool = False
    # Phase 12b1 (Sub-project A): plugins that should be active on a fresh
    # install without the user opting in. Currently only `memory-honcho`
    # uses this (so Honcho becomes the default memory provider when
    # Docker is available). Wizard + config consumers honor this flag
    # rather than the legacy "empty provider = baseline only" fallback.
    enabled_by_default: bool = False


# ─── Stop reasons ──────────────────────────────────────────────────────


class StopReason(str, Enum):
    """Why a conversation step ended."""

    END_TURN = "end_turn"  # model produced final response, no more tool calls
    TOOL_USE = "tool_use"  # model wants to call tools — loop continues
    MAX_TOKENS = "max_tokens"  # hit output limit
    INTERRUPTED = "interrupted"  # user cancelled
    BUDGET_EXHAUSTED = "budget_exhausted"  # iteration budget spent
    ERROR = "error"  # unrecoverable error


__all__ = [
    "Role",
    "Message",
    "ToolCall",
    "ToolResult",
    "Platform",
    "MessageEvent",
    "SendResult",
    "PluginManifest",
    "StopReason",
]
