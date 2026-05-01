"""Public realtime-voice contract.

Direct Python port of openclaw/src/realtime-voice/provider-types.ts (commit 2026-04-23).
Plugins implementing realtime voice (e.g. OpenAI Realtime, future Anthropic
voice) inherit ``BaseRealtimeVoiceBridge`` and implement the seven abstract
methods. Audio is PCM16 raw bytes — μ-law (telephony) is intentionally
out of scope for OC's local-mic use case.

The SDK boundary test enforces this module imports nothing from
``opencomputer.*``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

RealtimeVoiceRole = Literal["user", "assistant"]
RealtimeVoiceCloseReason = Literal["completed", "error"]


@dataclass(frozen=True, slots=True)
class RealtimeVoiceTool:
    """Function-tool schema sent to the realtime model on session.update.

    Mirror of the TS ``RealtimeVoiceTool`` shape. ``parameters`` is a
    JSON-Schema object dict (matches ``ToolSchema.parameters`` from
    ``plugin_sdk.tool_contract`` so OC's existing tool registry plugs in
    without translation).
    """

    type: Literal["function"]
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RealtimeVoiceToolCallEvent:
    """Emitted by the bridge when the model invokes a tool mid-stream.

    The bridge buffers any provider-specific delta/buffering protocol and
    assembles this event when the call is complete. The session
    orchestrator dispatches via ``ToolRegistry`` and calls
    ``bridge.submit_tool_result(call_id, result)`` on completion.

    Cross-provider field set:

    * ``call_id`` — universal id. Used by ``submit_tool_result`` to
      route results back. Required.
    * ``name``, ``args`` — what to call and with what. Required.
    * ``item_id`` — OpenAI's separate conversation-item id. ``None`` for
      providers that don't have a separate item concept (Gemini, hypothetical
      Anthropic). Consumers SHOULD prefer ``call_id`` when they need an id;
      reach for ``item_id`` only when interoperating with OpenAI's
      conversation-item APIs.
    * ``extra`` — opaque per-provider dict for fields that don't fit the
      cross-provider shape (OpenAI's ``response_id``, Gemini's
      ``scheduling`` hint). Cross-provider consumers MUST treat it as
      optional metadata — never branch on its contents from shared code.
    """

    call_id: str
    name: str
    args: Any  # decoded JSON, typically dict
    item_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class BaseRealtimeVoiceBridge(ABC):
    """ABC mirroring OpenClaw's RealtimeVoiceBridge (TS) interface.

    Concrete implementations open a WebSocket (or whatever transport
    the provider needs) and translate provider events to the registered
    callbacks. The session orchestrator is unaware of the underlying
    transport — it only depends on this ABC.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection. Resolves once session is ready."""

    @abstractmethod
    def send_audio(self, audio: bytes) -> None:
        """Push a PCM16 audio chunk from the mic to the model."""

    @abstractmethod
    def send_user_message(self, text: str) -> None:
        """Inject a typed-in user message (no audio)."""

    @abstractmethod
    def submit_tool_result(self, call_id: str, result: Any) -> None:
        """After the agent ran a tool, push the result back to the model."""

    @abstractmethod
    def trigger_greeting(self, instructions: str | None = None) -> None:
        """Ask the model to speak first (used at session start)."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the connection. Idempotent."""

    @abstractmethod
    def is_connected(self) -> bool:
        """True only when the session is configured AND the WS is open."""


__all__ = [
    "BaseRealtimeVoiceBridge",
    "RealtimeVoiceCloseReason",
    "RealtimeVoiceRole",
    "RealtimeVoiceTool",
    "RealtimeVoiceToolCallEvent",
]
