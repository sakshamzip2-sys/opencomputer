"""VideoAnalyzeTool — first-class video-analysis tool.

Wave 5 T7 — Hermes-port (c9a3f36f5). Mirror of
:class:`opencomputer.tools.vision_analyze.VisionAnalyzeTool` for video
files: base64-encodes the file and sends as a ``video_url`` content
block (OpenRouter / Gemini standard). 50 MB cap, 180s minimum timeout.

Provider auth + model selection inherits from
:func:`opencomputer.agent.aux_llm.complete_video`. ``AUXILIARY_VIDEO_MODEL``
env override falls back to ``AUXILIARY_VISION_MODEL`` so users can keep
a single multimodal aux model configured for both.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger(__name__)

#: Supported file extensions. Anything outside this set is rejected
#: before any I/O — providers' video models tend to support the same
#: short list, and rejecting early gives a useful error message.
SUPPORTED_VIDEO_FORMATS: frozenset[str] = frozenset(
    {"mp4", "webm", "mov", "avi", "mkv", "mpeg"},
)

#: Hard cap on file size — 50 MB. Sized to fit comfortably in a single
#: HTTP request to OpenRouter / Gemini and to keep the base64 encoding
#: in memory without making the agent loop slow.
MAX_VIDEO_BYTES: int = 50 * 1024 * 1024

#: Soft warning threshold — 20 MB. Above this we log a heads-up that
#: the call will be slow but still try.
WARN_VIDEO_BYTES: int = 20 * 1024 * 1024

#: Minimum provider timeout for video calls. Short videos still take
#: seconds-to-tens-of-seconds for the model to ingest, so 180s is the
#: floor regardless of the user's standard request timeout.
MIN_TIMEOUT_S: float = 180.0


def _ext(path: str) -> str:
    """Return ``path``'s lowercase extension without the leading dot."""
    return Path(path).suffix.lower().lstrip(".")


def _mime_for(ext: str) -> str:
    """Best-effort MIME for a video extension. Falls back to ``video/<ext>``."""
    return mimetypes.types_map.get("." + ext, f"video/{ext}")


async def video_analyze(
    *,
    path: str,
    prompt: str,
    model: str | None = None,
) -> str:
    """Analyze a local video file; return a text description.

    Hermes-port shape: function-style API used directly by tests +
    callers; the :class:`VideoAnalyzeTool` registers the dispatcher.
    Errors raise ``ValueError`` for client-side validation failures
    (unsupported format, oversize) and ``RuntimeError`` for provider
    failures (so the tool wrapper can convert each into a clean
    is_error ToolResult).
    """
    ext = _ext(path)
    if ext not in SUPPORTED_VIDEO_FORMATS:
        raise ValueError(
            f"Unsupported video format: .{ext!r}. "
            f"Supported: {sorted(SUPPORTED_VIDEO_FORMATS)}",
        )
    size = os.path.getsize(path)
    if size > MAX_VIDEO_BYTES:
        raise ValueError(
            f"Video exceeds {MAX_VIDEO_BYTES // (1024 * 1024)} MB cap "
            f"(file is {size // (1024 * 1024)} MB)",
        )
    if size > WARN_VIDEO_BYTES:
        logger.warning(
            "video_analyze: %s is %d MB — large videos take longer",
            path,
            size // (1024 * 1024),
        )
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    mime = _mime_for(ext)

    aux_model = (
        model
        or os.environ.get("AUXILIARY_VIDEO_MODEL")
        or os.environ.get("AUXILIARY_VISION_MODEL")
    )

    from opencomputer.agent.aux_llm import complete_video

    return await complete_video(
        video_base64=b64,
        mime_type=mime,
        prompt=prompt or "Describe this video in detail.",
        max_tokens=1024,
        model=aux_model,
    )


class VideoAnalyzeTool(BaseTool):
    """Wrap :func:`video_analyze` as a first-class tool registration.

    parallel_safe = True (independent provider call). strict_mode True
    so the JSON schema is enforced.
    """

    parallel_safe = True
    strict_mode = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="VideoAnalyze",
            description=(
                "Analyze a local video file with a multimodal LLM. Returns a "
                f"text description. Hard cap {MAX_VIDEO_BYTES // (1024 * 1024)} MB. "
                f"Supported: {', '.join(sorted(SUPPORTED_VIDEO_FORMATS))}. "
                "AUXILIARY_VIDEO_MODEL env override falls back to "
                "AUXILIARY_VISION_MODEL."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path to a local video file.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Question / instruction for the model. "
                            "Default: 'Describe this video in detail.'"
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional provider-side model id. Falls back to "
                            "AUXILIARY_VIDEO_MODEL or AUXILIARY_VISION_MODEL env "
                            "vars if unset."
                        ),
                    },
                },
                "required": ["path", "prompt"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        path = args.get("path")
        prompt = args.get("prompt") or "Describe this video in detail."
        model = args.get("model")
        if not path or not isinstance(path, str):
            return ToolResult(
                tool_call_id=call.id,
                content="VideoAnalyze: missing required `path` argument",
                is_error=True,
            )
        try:
            text = await video_analyze(path=path, prompt=prompt, model=model)
        except (ValueError, OSError) as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"VideoAnalyze: {exc}",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 — provider failure surface
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"VideoAnalyze provider call failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                is_error=True,
            )
        return ToolResult(tool_call_id=call.id, content=text or "")


__all__ = [
    "MAX_VIDEO_BYTES",
    "MIN_TIMEOUT_S",
    "SUPPORTED_VIDEO_FORMATS",
    "WARN_VIDEO_BYTES",
    "VideoAnalyzeTool",
    "video_analyze",
]
