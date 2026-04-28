"""Groq STT — fast/cheap Whisper-large-v3 transcription via Groq's API.

Tier 3.E from docs/refs/hermes-agent/2026-04-28-major-gaps.md.
Groq's Whisper-large-v3 is ~10x faster and ~5x cheaper than OpenAI's
``whisper-1`` for the same accuracy class. For voice memo
transcription on every Telegram audio message, costs add up — Groq
makes that workload economically reasonable.

The library:
    https://github.com/groq/groq-python
    pip install groq  (or: pip install opencomputer[stt-groq])

Why this matters: voice-mode (PR #199) defaults to OpenAI Whisper for
cloud STT. Adding Groq as a peer backend lets users opt into the
cheap+fast path without giving up the cost-guard / format-tolerance
infra.

Module is **lazy-imported** — production calls ``import groq`` only
when transcribe_audio_groq() is invoked, so installs without the extra
don't pay the import cost.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.voice.groq_stt")


_DEFAULT_MODEL = "whisper-large-v3"
# Groq's Whisper accepts up to 25 MB per request, same as OpenAI's.
_MAX_BYTES = 25 * 1024 * 1024

_KNOWN_MODELS: frozenset[str] = frozenset({
    "whisper-large-v3",       # Default — most accurate
    "whisper-large-v3-turbo", # Faster, slightly less accurate
    "distil-whisper-large-v3-en",  # English-only, ~2x speed of v3
})


class GroqNotInstalledError(RuntimeError):
    """Raised when groq isn't installed but transcribe_audio_groq is called."""


# Backwards-compat alias — the original name was used in tests / external callers
# before the N818 lint convention required the *Error suffix.
GroqNotInstalled = GroqNotInstalledError


def _import_groq() -> Any:
    """Lazy-import groq; raise a helpful error if missing."""
    try:
        import groq  # type: ignore[import-not-found]
    except ImportError as e:
        raise GroqNotInstalledError(
            "groq not installed. Install with `pip install groq` "
            "or `pip install opencomputer[stt-groq]`. Free tier "
            "available at https://console.groq.com — set GROQ_API_KEY."
        ) from e
    return groq


def transcribe_audio_groq(
    audio_path: Path | str,
    *,
    model: str = _DEFAULT_MODEL,
    language: str | None = None,
    api_key: str | None = None,
    groq_module: Any | None = None,
) -> str:
    """Transcribe an audio file via Groq's Whisper. Returns the transcript.

    Args:
        audio_path: Path to mp3/m4a/ogg/wav/webm/flac.
        model: ``whisper-large-v3`` (default), ``whisper-large-v3-turbo``,
            or ``distil-whisper-large-v3-en``.
        language: ISO-639-1 hint (``"en"``, ``"hi"``, etc.). Skips
            language detection — slightly faster + more accurate when known.
        api_key: GROQ_API_KEY override. Default reads from env.
        groq_module: Test seam — inject a fake module exposing
            ``Groq`` class with a ``audio.transcriptions.create`` method.

    Raises:
        ValueError: file missing or > 25 MB, or unknown model.
        GroqNotInstalled: package not installed AND no test module.
        RuntimeError: API failure / no API key.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        raise ValueError(f"audio file not found: {audio_path}")
    size = audio_path.stat().st_size
    if size > _MAX_BYTES:
        raise ValueError(
            f"audio file size {size} bytes exceeds Groq STT limit of "
            f"{_MAX_BYTES} ({_MAX_BYTES // 1024 // 1024} MB)"
        )
    if size == 0:
        raise ValueError(f"audio file is empty: {audio_path}")
    if model not in _KNOWN_MODELS:
        # Don't hard-block — Groq adds models periodically. Warn + try.
        logger.warning(
            "model %r not in known set %r — passing through to Groq anyway",
            model, sorted(_KNOWN_MODELS),
        )

    import os
    key = api_key or os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError(
            "no API key — set GROQ_API_KEY env var or pass api_key= "
            "to transcribe_audio_groq. Get one free at "
            "https://console.groq.com"
        )

    mod = groq_module or _import_groq()
    client = mod.Groq(api_key=key)

    try:
        with audio_path.open("rb") as fh:
            kwargs: dict[str, Any] = {
                "model": model,
                "file": (audio_path.name, fh.read()),
            }
            if language:
                kwargs["language"] = language
            response = client.audio.transcriptions.create(**kwargs)
    except Exception as e:
        raise RuntimeError(
            f"Groq STT failed: {type(e).__name__}: {e}"
        ) from e

    text = getattr(response, "text", None) or ""
    if isinstance(response, dict):
        text = response.get("text", "")
    text = str(text).strip()
    if not text:
        raise RuntimeError("Groq STT returned empty transcript")
    logger.info(
        "Groq STT transcribed %s (model=%s, %d bytes) → %d chars",
        audio_path.name, model, size, len(text),
    )
    return text


__all__ = [
    "transcribe_audio_groq",
    "GroqNotInstalled",
]
