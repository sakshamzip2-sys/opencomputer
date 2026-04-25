"""Speech-to-text via OpenAI's Whisper (``audio.transcriptions.create``).

Transcribes audio files (mp3 / mp4 / mpeg / m4a / wav / webm / ogg / flac).
Cost-guarded: checks ``CostGuard.check_budget`` before the call.

Usage::

    from opencomputer.voice import transcribe_audio

    text = transcribe_audio(Path("/tmp/voice.ogg"))
    # → "good morning, what's the rsi of guj alkali"

Errors:

- :class:`opencomputer.cost_guard.BudgetExceeded` — daily / monthly cap hit.
- :class:`ValueError` — file missing or > 25 MB (Whisper API limit).
- :class:`RuntimeError` — API failure.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

from opencomputer.cost_guard import BudgetExceeded, get_default_guard
from opencomputer.voice.costs import stt_cost_usd

logger = logging.getLogger("opencomputer.voice.stt")


# Whisper hard limit
_WHISPER_MAX_BYTES = 25 * 1024 * 1024
_DEFAULT_MODEL = "whisper-1"

# We don't have ffmpeg on every host, so we fall back to a 30 s assumption
# for cost projection when we can't read the duration directly. This is a
# safe over-estimate since Whisper rounds up to the nearest second anyway.
_FALLBACK_DURATION_S = 30.0


def transcribe_audio(
    audio_path: Path | str,
    *,
    model: str = _DEFAULT_MODEL,
    language: str | None = None,
    cost_guard: object | None = None,
    openai_client: object | None = None,
) -> str:
    """Transcribe an audio file to text. Returns the transcript as a string.

    Args:
        audio_path: Path to the audio file (mp3 / m4a / ogg / wav / etc.).
        model: Whisper model id (currently only ``whisper-1`` exists).
        language: Optional ISO-639-1 hint (e.g. ``"en"``, ``"hi"``). Skips
            language detection — slightly faster, more accurate when known.
        cost_guard: Override the default cost-guard (used in tests).
        openai_client: Override the OpenAI client (used in tests).

    Raises:
        ValueError: file missing or > 25 MB.
        BudgetExceeded: budget cap blocked the call.
        RuntimeError: API failure.
    """
    p = Path(audio_path)
    if not p.exists() or not p.is_file():
        raise ValueError(f"audio file not found: {p}")

    size = p.stat().st_size
    if size > _WHISPER_MAX_BYTES:
        raise ValueError(
            f"audio file is {size // 1024 // 1024} MB; Whisper limit is "
            f"{_WHISPER_MAX_BYTES // 1024 // 1024} MB. Split the file first."
        )

    # Project cost. Try to read duration from the file (works for WAV);
    # fall back to a flat assumption otherwise.
    duration_s = _estimate_duration_seconds(p)
    projected = stt_cost_usd(duration_s, model=model)

    guard = cost_guard or get_default_guard()
    decision = guard.check_budget("openai", projected_cost_usd=projected)
    if not decision.allowed:
        logger.warning("STT blocked by cost-guard: %s", decision.reason)
        raise BudgetExceeded(decision.reason)

    # Resolve client (lazy so tests can patch).
    if openai_client is None:
        from openai import OpenAI

        openai_client = OpenAI()

    try:
        with p.open("rb") as fh:
            kwargs: dict[str, object] = {
                "model": model,
                "file": fh,
            }
            if language:
                kwargs["language"] = language
            response = openai_client.audio.transcriptions.create(**kwargs)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"OpenAI STT failed: {type(exc).__name__}: {exc}") from exc

    # OpenAI's response object exposes .text on TranscriptionVerbose; text str
    # for the simple variant. Handle both.
    text = getattr(response, "text", None) or str(response)
    text = text.strip()

    guard.record_usage("openai", cost_usd=projected, operation=f"stt:{model}")
    logger.info(
        "STT transcribed %s (~%.1fs) → %d chars ($%.4f)",
        p.name, duration_s, len(text), projected,
    )
    return text


def _estimate_duration_seconds(path: Path) -> float:
    """Return audio duration in seconds. Falls back to ``_FALLBACK_DURATION_S``
    when the format isn't readable without ffmpeg.

    WAV files have a parseable header in stdlib (``wave`` module). For OGG /
    MP3 / M4A we'd need ffmpeg or a heavier parser; project flat instead.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".wav":
            with wave.open(str(path), "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate() or 1
                return frames / float(rate)
    except wave.Error:
        pass
    return _FALLBACK_DURATION_S


__all__ = ["transcribe_audio"]
