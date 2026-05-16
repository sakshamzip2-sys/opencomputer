"""VoiceSynthesizeLocal tool — text → audio via local NeuTTS (no API call).

Milestone 4. The local counterpart of
:class:`opencomputer.tools.voice_synthesize.VoiceSynthesizeTool` (which uses
OpenAI TTS): this tool synthesizes speech entirely on-device via NeuTTS — no
network call at synthesis time, no per-call cost.

NeuTTS is a *voice-cloning* model — it speaks in the voice of a reference
clip — so the tool requires a ``reference_audio`` path plus that clip's
``reference_text`` transcript.

Registered only when the ``neutts`` package is importable (gated in
``cli._register_builtin_tools`` via
:func:`opencomputer.voice.tts_neutts.neutts_available`); without the
``[neutts]`` extra the tool is not registered and the agent never sees it —
zero behavior change for users who do not install it.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

#: Upper bound on input text length, mirroring ``VoiceSynthesizeTool``.
_MAX_TEXT_CHARS = 4000


class VoiceSynthesizeLocalTool(BaseTool):
    """Synthesize speech locally via NeuTTS, cloning a reference voice."""

    # Item 3 parity: schema enumerates its parameters.
    strict_mode = True

    #: A read-style synthesis with no external side effects — safe to run
    #: alongside other parallel tools.
    parallel_safe: bool = True

    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="voice.synthesize.local",
            tier_required=ConsentTier.IMPLICIT,
            human_description=(
                "Synthesize speech audio from text on-device via NeuTTS "
                "(local — no network call, no API cost)"
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="VoiceSynthesizeLocal",
            description=(
                "Convert text into spoken audio entirely on-device via NeuTTS "
                "— no network call and no API cost, unlike VoiceSynthesize "
                "(which uses OpenAI). Returns the absolute path of a generated "
                ".wav file. NeuTTS is a voice-cloning model: it speaks in the "
                "voice of a reference clip, so `reference_audio` (a path to a "
                "3-15s clean mono .wav) and `reference_text` (that clip's exact "
                "transcript) are both required. Requires `pip install "
                "opencomputer[neutts]`; run `oc voice install-neutts` once to "
                "pre-download the model."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to speak.",
                        "maxLength": _MAX_TEXT_CHARS,
                    },
                    "reference_audio": {
                        "type": "string",
                        "description": (
                            "Path to a reference .wav (3-15s of clean, "
                            "continuous mono speech) — the voice to clone."
                        ),
                    },
                    "reference_text": {
                        "type": "string",
                        "description": (
                            "The exact transcript of the reference_audio clip."
                        ),
                    },
                },
                "required": ["text", "reference_audio", "reference_text"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments if isinstance(call.arguments, dict) else {}

        text = (args.get("text") or "").strip()
        if not text:
            return self._error(call, "text is required and must be non-empty")
        if len(text) > _MAX_TEXT_CHARS:
            return self._error(
                call,
                f"text length {len(text)} exceeds the {_MAX_TEXT_CHARS}-char limit",
            )

        reference_audio = (args.get("reference_audio") or "").strip()
        if not reference_audio:
            return self._error(
                call,
                "reference_audio is required — NeuTTS clones a reference "
                "voice; pass a path to a 3-15s clean mono .wav clip.",
            )
        reference_text = (args.get("reference_text") or "").strip()
        if not reference_text:
            return self._error(
                call,
                "reference_text is required — pass the exact transcript of "
                "the reference_audio clip.",
            )

        # Lazy import so registering this tool never drags `torch` into a
        # plugin-loader test run; the voice module owns its own dep check.
        from opencomputer.voice.tts_neutts import (  # noqa: PLC0415
            NeuTTSConfig,
            NeuTTSSynthesizer,
        )

        out_dir = Path(os.environ.get("TMPDIR", "/tmp"))
        out_path = out_dir / f"neutts_{uuid.uuid4().hex[:12]}.wav"
        cfg = NeuTTSConfig(
            reference_audio=reference_audio,
            reference_text=reference_text,
        )
        try:
            await NeuTTSSynthesizer(cfg).synthesize(text, out_path=str(out_path))
        except Exception as exc:  # noqa: BLE001 — any synth failure → tool error
            return self._error(
                call, f"local speech synthesis failed: {type(exc).__name__}: {exc}"
            )

        return ToolResult(
            tool_call_id=call.id,
            content=f"Audio written to: {out_path.resolve()}",
        )

    @staticmethod
    def _error(call: ToolCall, message: str) -> ToolResult:
        """Build an error :class:`ToolResult` with a uniform ``Error:`` prefix."""
        return ToolResult(
            tool_call_id=call.id,
            content=f"Error: {message}",
            is_error=True,
        )


__all__ = ["VoiceSynthesizeLocalTool"]
