"""AudioTranscribe — local STT via mlx-whisper or pywhispercpp."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path


class WhisperBackendUnavailableError(RuntimeError):
    """No local whisper backend installed (neither mlx-whisper nor pywhispercpp)."""


@dataclass(frozen=True)
class Transcription:
    text: str
    backend: str
    duration_seconds: float = 0.0


def _try_import_mlx_whisper():
    try:
        import mlx_whisper  # type: ignore

        return mlx_whisper
    except ImportError:
        return None


def _try_import_pywhispercpp():
    try:
        from pywhispercpp.model import Model  # type: ignore

        return Model
    except ImportError:
        return None


def transcribe(path: Path, *, model: str = "base") -> Transcription:
    """Transcribe an audio file. Picks the best available backend.

    Order:
      1. mlx-whisper (Apple Silicon)
      2. pywhispercpp (cross-platform)
      3. WhisperBackendUnavailableError

    Returns a :class:`Transcription`. Backend choice is reported.
    """
    if not path.exists():
        raise FileNotFoundError(str(path))

    mlx = _try_import_mlx_whisper() if platform.system() == "Darwin" else None
    if mlx is not None:
        result = mlx.transcribe(str(path))
        # mlx-whisper returns a dict with 'text'
        return Transcription(
            text=str(result.get("text", "")),
            backend="mlx-whisper",
        )

    Model = _try_import_pywhispercpp()
    if Model is not None:
        m = Model(model)
        segments = m.transcribe(str(path))
        text = " ".join(s.text for s in segments)
        return Transcription(text=text, backend="pywhispercpp")

    raise WhisperBackendUnavailableError(
        "No whisper backend available. Install 'mlx-whisper' (Apple Silicon) "
        "or 'pywhispercpp' (cross-platform) to enable AudioTranscribe."
    )


__all__ = ["Transcription", "WhisperBackendUnavailableError", "transcribe"]
