"""TTSGenerate — text-to-speech via edge-tts (already a core dep)."""

from __future__ import annotations

from pathlib import Path


class EdgeTTSUnavailableError(RuntimeError):
    """edge-tts not installed — should never happen (it's a core dep)."""


async def synthesize(
    text: str, *, voice: str = "en-US-AvaNeural", out_path: Path
) -> Path:
    """Synthesize text to MP3 at ``out_path``. Returns the written path.

    Edge TTS is async-native; this function is awaitable. The caller
    handles event-loop lifetime.
    """
    if not text or not text.strip():
        raise ValueError("text must be non-empty")
    try:
        import edge_tts  # type: ignore
    except ImportError as e:
        raise EdgeTTSUnavailableError(
            "edge-tts is not installed (this is unexpected; it's a core "
            "OC dependency — try 'pip install -e .'."
        ) from e

    out_path.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(text=text, voice=voice)
    await communicate.save(str(out_path))
    return out_path


__all__ = ["EdgeTTSUnavailableError", "synthesize"]
