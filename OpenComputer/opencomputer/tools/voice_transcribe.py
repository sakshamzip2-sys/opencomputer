"""VoiceTranscribe tool — audio file → text via OpenAI Whisper.

Wraps :func:`opencomputer.voice.transcribe_audio`. Exposes transcription
as an explicit agent tool so the model can summarize voice memos, decode
attachments, etc. Telegram already calls transcribe_audio for inbound
voice messages; this tool is for everything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class VoiceTranscribeTool(BaseTool):
    """Transcribe an audio file to text. Caller supplies an absolute path."""

    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="voice.transcribe",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Transcribe an audio file to text via OpenAI Whisper",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="VoiceTranscribe",
            description=(
                "Transcribe an audio file (m4a/mp3/wav/opus/etc.) to text using OpenAI "
                "Whisper. Use when the user supplies a voice memo, meeting recording, "
                "or attached audio you need to read into text. Pass an optional "
                "`language` ISO 639-1 hint ('en', 'es', 'ja') to bias the decoder; "
                "Whisper auto-detects otherwise. Caller must provide an absolute file "
                "path that already exists — relative paths are rejected. Network call "
                "to OpenAI; do not pass sensitive recordings without consent. Telegram "
                "voice messages already auto-transcribe; use this tool for everything else."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "audio_path": {
                        "type": "string",
                        "description": "Absolute path to an audio file.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Optional ISO 639-1 language hint (e.g. 'en', 'es').",
                    },
                },
                "required": ["audio_path"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        raw = (args.get("audio_path") or "").strip()
        if not raw:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: audio_path is required",
                is_error=True,
            )

        path = Path(raw)
        if not path.is_absolute():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: audio_path must be absolute, got: {raw}",
                is_error=True,
            )
        if not path.exists():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: file not found: {path}",
                is_error=True,
            )
        if not path.is_file():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: not a regular file: {path}",
                is_error=True,
            )

        from opencomputer.voice import transcribe_audio

        try:
            text = transcribe_audio(str(path), language=args.get("language"))
        except Exception as exc:  # noqa: BLE001 — surface as tool error
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error transcribing audio: {exc}",
                is_error=True,
            )

        return ToolResult(tool_call_id=call.id, content=text or "")
