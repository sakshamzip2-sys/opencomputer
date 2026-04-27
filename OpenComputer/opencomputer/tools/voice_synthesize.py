"""VoiceSynthesize tool — text → audio file via OpenAI TTS.

Wraps the existing :func:`opencomputer.voice.synthesize_speech` (which is
already cost-guarded). Exposes voice synthesis to the agent so it can
generate speech without going through a channel-specific path.

Phase 1.1 of the catch-up plan (real-gui-velvet-lemur): the voice module
already shipped, but had no agent-invocable surface. Adding this tool
lights up "agent says X out loud" workflows without any new infra.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class VoiceSynthesizeTool(BaseTool):
    """Synthesize speech audio from text. Returns the file path."""

    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="voice.synthesize",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Synthesize speech audio from text via OpenAI TTS",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="VoiceSynthesize",
            description=(
                "Convert text into spoken audio via OpenAI TTS; returns the absolute "
                "path of the generated file. Use this when the user wants you to speak "
                "a reply out loud, generate a voice memo, or hand the answer to a chat "
                "platform that prefers audio. Default format is opus (good for Telegram/"
                "WhatsApp); pass `format` to override (mp3/wav/flac/aac/pcm). Default "
                "voice is `alloy`; override with echo/fable/onyx/nova/shimmer. Network "
                "call to OpenAI — incurs token cost; the underlying voice module is "
                "cost-guarded but loud usage will still bill the account."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to speak.",
                        "maxLength": 4000,
                    },
                    "voice": {
                        "type": "string",
                        "description": "OpenAI TTS voice (alloy, echo, fable, onyx, nova, shimmer).",
                        "default": "alloy",
                    },
                    "format": {
                        "type": "string",
                        "description": "Audio format (opus|mp3|aac|flac|wav|pcm).",
                        "default": "opus",
                    },
                    "model": {
                        "type": "string",
                        "description": "TTS model (tts-1 or tts-1-hd).",
                        "default": "tts-1",
                    },
                },
                "required": ["text"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        text = (args.get("text") or "").strip()
        if not text:
            return ToolResult(
                tool_call_id=call.id,
                content="Error: text is required and must be non-empty",
                is_error=True,
            )

        # Lazy import so adding this tool doesn't drag the openai SDK into every
        # plugin-loader test run; voice module owns its own dependency check.
        from opencomputer.voice import VoiceConfig, synthesize_speech

        cfg = VoiceConfig(
            voice=args.get("voice", "alloy"),
            format=args.get("format", "opus"),
            model=args.get("model", "tts-1"),
        )
        try:
            path = synthesize_speech(text, cfg=cfg)
        except Exception as exc:  # noqa: BLE001 — surface any synth failure as tool error
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error synthesizing speech: {exc}",
                is_error=True,
            )

        return ToolResult(
            tool_call_id=call.id,
            content=f"Audio written to: {Path(path).resolve()}",
        )
