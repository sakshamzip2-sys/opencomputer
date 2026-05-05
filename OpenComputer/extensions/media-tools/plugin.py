"""media-tools plugin entry — registers ImageInfo + TTSGenerate + AudioTranscribe.

C.3 MVP (2026-05-05). All-local, no paid APIs. Image generation
(needs paid API + model weights) is explicitly out of scope.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Plugin-loader gives us the parent dir on sys.path; package import is
# the fallback for tests that do `from extensions.media_tools.plugin`.
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from tools.audio_transcribe import (  # type: ignore
        WhisperBackendUnavailableError,
        transcribe,
    )
    from tools.image_info import (  # type: ignore
        PILUnavailableError,
        inspect_image,
    )
    from tools.tts_generate import EdgeTTSUnavailableError, synthesize  # type: ignore
finally:
    pass

from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger("opencomputer.ext.media_tools")


class ImageInfo(BaseTool):
    """Inspect an image file (dimensions, format, EXIF)."""

    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="ImageInfo",
            description=(
                "Inspect a local image file. Returns dimensions, format, "
                "color mode, and EXIF metadata."
            ),
            input_schema={
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


class TTSGenerate(BaseTool):
    """Synthesize text to MP3 audio via edge-tts."""

    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="TTSGenerate",
            description=(
                "Generate an MP3 from text using Microsoft Edge TTS. "
                "All-local synthesis; no paid API."
            ),
            input_schema={
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


class AudioTranscribe(BaseTool):
    """Transcribe local audio via mlx-whisper or pywhispercpp."""

    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="AudioTranscribe",
            description=(
                "Transcribe a local audio file. Uses mlx-whisper on Apple "
                "Silicon when available; falls back to pywhispercpp on "
                "other platforms. Returns the transcribed text + backend."
            ),
            input_schema={
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
    api.register_tool(ImageInfo)
    api.register_tool(TTSGenerate)
    api.register_tool(AudioTranscribe)
    logger.info("media-tools plugin: 3 tools registered")
