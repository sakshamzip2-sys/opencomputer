"""media-tools plugin entry — registers ImageInfo + TTSGenerate + AudioTranscribe.

C.3 MVP (2026-05-05). All-local, no paid APIs. Image generation
(needs paid API + model weights) is explicitly out of scope.

Layout note: sibling files are flat at the plugin root (audio_transcribe.py,
image_info.py, tts_generate.py). Avoid the ``tools/`` subdir convention —
CLAUDE.md gotcha #1 explains why (Python's sys.modules cache shadows
sibling-named packages across plugins).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# Dual-import: plugin-loader puts this dir on sys.path; tests that
# import ``extensions.media_tools.plugin`` go through the package
# fallback.
try:
    from audio_transcribe import (  # type: ignore[import-not-found]
        WhisperBackendUnavailableError,
        transcribe,
    )
    from image_info import (  # type: ignore[import-not-found]
        PILUnavailableError,
        inspect_image,
    )
    from tts_generate import (  # type: ignore[import-not-found]
        EdgeTTSUnavailableError,
        synthesize,
    )
except ImportError:  # pragma: no cover
    from extensions.media_tools.audio_transcribe import (
        WhisperBackendUnavailableError,
        transcribe,
    )
    from extensions.media_tools.image_info import (
        PILUnavailableError,
        inspect_image,
    )
    from extensions.media_tools.tts_generate import (
        EdgeTTSUnavailableError,
        synthesize,
    )

logger = logging.getLogger("opencomputer.ext.media_tools")


class _RunToExecute:
    """Bridge ``run(**kwargs) -> dict`` to ``execute(call) -> ToolResult``.

    Mirrors the helper in extensions/memory-vector/plugin.py.
    """

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            result = await self.run(**call.arguments)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — must not raise from execute
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {exc}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=json.dumps(result, default=str),
        )


class ImageInfo(_RunToExecute, BaseTool):
    """Inspect an image file (dimensions, format, EXIF)."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="ImageInfo",
            description=(
                "Inspect a local image file. Returns dimensions, format, "
                "color mode, and EXIF metadata."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )

    async def run(self, *, path: str) -> dict:
        try:
            meta = inspect_image(Path(path).expanduser().resolve())
        except FileNotFoundError:
            return {"error": "file not found", "path": path}
        except PILUnavailableError as e:
            return {"error": str(e)}
        return {
            "path": meta.path,
            "format": meta.format,
            "mode": meta.mode,
            "width": meta.width,
            "height": meta.height,
            "exif": meta.exif,
        }


class TTSGenerate(_RunToExecute, BaseTool):
    """Synthesize text to MP3 audio via edge-tts."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="TTSGenerate",
            description=(
                "Generate an MP3 from text using Microsoft Edge TTS. "
                "All-local synthesis; no paid API."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "voice": {
                        "type": "string",
                        "default": "en-US-AvaNeural",
                        "description": "edge-tts voice id; see "
                        "https://github.com/rany2/edge-tts for the list.",
                    },
                    "out_path": {
                        "type": "string",
                        "description": "Where to write the MP3.",
                    },
                },
                "required": ["text", "out_path"],
            },
        )

    async def run(
        self,
        *,
        text: str,
        out_path: str,
        voice: str = "en-US-AvaNeural",
    ) -> dict:
        try:
            written = await synthesize(
                text,
                voice=voice,
                out_path=Path(out_path).expanduser().resolve(),
            )
        except EdgeTTSUnavailableError as e:
            return {"error": str(e)}
        return {"path": str(written), "voice": voice}


class AudioTranscribe(_RunToExecute, BaseTool):
    """Transcribe local audio via mlx-whisper or pywhispercpp."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="AudioTranscribe",
            description=(
                "Transcribe a local audio file. Uses mlx-whisper on Apple "
                "Silicon when available; falls back to pywhispercpp on "
                "other platforms. Returns the transcribed text + backend."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "model": {
                        "type": "string",
                        "default": "base",
                        "description": "Whisper model size: tiny/base/small/medium/large.",
                    },
                },
                "required": ["path"],
            },
        )

    async def run(self, *, path: str, model: str = "base") -> dict:
        try:
            t = transcribe(Path(path).expanduser().resolve(), model=model)
        except FileNotFoundError:
            return {"error": "file not found", "path": path}
        except WhisperBackendUnavailableError as e:
            return {"error": str(e)}
        return {"text": t.text, "backend": t.backend}


def register(api) -> None:
    api.register_tool(ImageInfo())
    api.register_tool(TTSGenerate())
    api.register_tool(AudioTranscribe())
    logger.info("media-tools plugin: 3 tools registered")
