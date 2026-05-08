"""Wave 3 (2026-05-08) — mlx-whisper STT backend for Apple Silicon.

mlx-whisper runs Whisper directly on Apple's MLX framework (Metal
Performance Shaders). On M-series Macs it benchmarks 5-10× faster
than ``openai-whisper`` (the standard CPU/CUDA backend). This module
exposes a backend with the same minimal interface as
:func:`opencomputer.voice.stt.transcribe_audio` so the voice layer
can swap backends via ``voice.stt: "mlx-whisper"`` in config.yaml.

Hard requirements:
* ``platform.system() == "Darwin"`` AND ``platform.machine() == "arm64"``
* ``pip install mlx-whisper`` (optional extra ``voice-mlx``)

Soft fail: when the platform doesn't match OR the package isn't
installed, :func:`is_available` returns False so callers can fall
through to other backends without crashing. The transcribe call
raises a helpful error message in the same case.

Local-only: runs entirely on-device. No network call, no API key,
no upload. Audio data never leaves the user's machine. This is the
right backend for sensitive content (e.g. financial discussions,
voice notes you don't want shipped to a SaaS endpoint).
"""

from __future__ import annotations

import logging
import platform
from pathlib import Path

logger = logging.getLogger("opencomputer.voice.stt_mlx_whisper")


_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"


def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def is_available() -> bool:
    """True when the backend can actually run on this host.

    Conservative: requires both Apple Silicon + the mlx_whisper package
    importable. Returns False on any failure path so callers chain to
    the next backend without exception noise.
    """
    if not _is_apple_silicon():
        return False
    try:
        import mlx_whisper  # noqa: F401
        return True
    except ImportError:
        return False


def transcribe_audio(
    audio_path: Path | str,
    *,
    model: str = _DEFAULT_MODEL,
    language: str | None = None,
) -> str:
    """Transcribe an audio file to text using mlx-whisper.

    Args:
        audio_path: path to a wav / mp3 / m4a / etc. — anything ffmpeg
            can decode.
        model: HuggingFace repo id of an MLX-converted Whisper model.
            Default is whisper-large-v3-turbo (fast + accurate).
        language: ISO-639-1 hint to bypass language auto-detection
            (slight speedup). ``None`` = auto-detect.

    Returns:
        The transcript text.

    Raises:
        RuntimeError: when the backend isn't available on this host.
        FileNotFoundError: when the input file doesn't exist.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"audio file not found: {audio_path}")

    if not _is_apple_silicon():
        raise RuntimeError(
            "mlx-whisper requires Apple Silicon (arm64 macOS); "
            f"this host is {platform.system()}/{platform.machine()}"
        )
    try:
        import mlx_whisper
    except ImportError as e:
        raise RuntimeError(
            "mlx-whisper is not installed; run "
            "`pip install mlx-whisper` (or use the voice-mlx extra)"
        ) from e

    kwargs: dict = {"path_or_hf_repo": model}
    if language:
        kwargs["language"] = language

    logger.info("mlx-whisper transcribe %s using %s", audio_path, model)
    result = mlx_whisper.transcribe(str(audio_path), **kwargs)
    return result["text"].strip()


__all__ = ["is_available", "transcribe_audio"]
