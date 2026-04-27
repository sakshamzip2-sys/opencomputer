"""TTS synthesis + sounddevice playback with barge-in.

Companion to ``opencomputer/voice/tts.py``: the latter handles cost-guarded
synthesis to disk; this module wires that synth call into a playback loop
that watches an optional ``barge_in_check`` callable so the user can
interrupt the agent mid-utterance (e.g. by tapping the spacebar).

Both ``sounddevice`` and ``soundfile`` are imported lazily inside the
playback path so this module loads cleanly on hosts without audio
hardware (CI, SSH, Docker, ...).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Re-export under our own name so tests can monkeypatch
# ``extensions.voice_mode.tts_playback.synthesize_speech`` directly.
from opencomputer.voice.tts import synthesize_speech  # noqa: F401

_log = logging.getLogger("opencomputer.voice_mode.tts_playback")


class PlaybackError(RuntimeError):
    """Raised when synthesis or playback cannot proceed."""


@dataclass(frozen=True, slots=True)
class PlaybackResult:
    duration_seconds: float
    interrupted: bool  # True if barge-in fired
    bytes_played: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def synthesize_and_play(
    text: str,
    *,
    cost_guard,
    voice_config=None,
    barge_in_check: Callable[[], bool] | None = None,
    barge_in_poll_seconds: float = 0.1,
) -> PlaybackResult:
    """Synthesize ``text`` via voice/tts.py and play it back over sounddevice.

    The synthesized temp file is always removed in the ``finally`` block —
    voice mode never persists generated audio.

    Args:
        text: Text to speak.
        cost_guard: ``opencomputer.cost_guard.CostGuard`` (forwarded to
            ``synthesize_speech``).
        voice_config: Optional ``opencomputer.voice.tts.VoiceConfig``.
        barge_in_check: Optional callable polled during playback. Returning
            ``True`` aborts playback cleanly.
        barge_in_poll_seconds: Poll interval for ``barge_in_check``.

    Raises:
        PlaybackError: synthesis failure or playback failure.
    """
    # Run the synth. In production ``synthesize_speech`` is a sync function;
    # tests patch it with AsyncMock. Handle both transparently.
    try:
        kwargs: dict[str, Any] = {"cost_guard": cost_guard}
        if voice_config is not None:
            kwargs["cfg"] = voice_config
        result = synthesize_speech(text, **kwargs)
        if asyncio.iscoroutine(result):
            audio_path = await result
        else:
            audio_path = result
    except Exception as exc:  # noqa: BLE001 — wrap for caller
        raise PlaybackError(f"TTS synth failed: {exc}") from exc

    audio_path = Path(audio_path)
    try:
        return await _play_file_with_barge_in(
            audio_path,
            barge_in_check=barge_in_check,
            barge_in_poll_seconds=barge_in_poll_seconds,
        )
    finally:
        # Honour voice-mode's "audio never persists" rule.
        try:
            audio_path.unlink(missing_ok=True)
        except OSError as exc:
            _log.debug("could not unlink temp audio %s: %s", audio_path, exc)


async def play_audio_file(
    path: Path,
    *,
    barge_in_check: Callable[[], bool] | None = None,
    barge_in_poll_seconds: float = 0.1,
) -> PlaybackResult:
    """Play an existing audio file (mp3/wav/ogg/opus) over sounddevice.

    Decoding goes through ``soundfile`` (libsndfile) — handles wav, flac,
    ogg/opus directly. mp3 needs libsndfile >= 1.1 (Python wheel ships it).

    Args:
        path: Audio file to play.
        barge_in_check: Optional callable polled during playback.
        barge_in_poll_seconds: Poll interval for ``barge_in_check``.

    Raises:
        PlaybackError: missing file, missing dependency, decode/playback
            failure.
    """
    return await _play_file_with_barge_in(
        Path(path),
        barge_in_check=barge_in_check,
        barge_in_poll_seconds=barge_in_poll_seconds,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _lazy_import_audio():
    """Import sounddevice + soundfile, raising PlaybackError with install hint."""
    try:
        import sounddevice as sd  # noqa: F401
        import soundfile as sf  # noqa: F401
    except ImportError as exc:
        raise PlaybackError(
            f"audio playback dependencies missing ({exc}). "
            f"install: pip install opencomputer[voice]  "
            f"(provides sounddevice + soundfile)"
        ) from exc
    return sd, sf


async def _play_file_with_barge_in(
    path: Path,
    *,
    barge_in_check: Callable[[], bool] | None,
    barge_in_poll_seconds: float,
) -> PlaybackResult:
    """Decode + play ``path``, optionally polling ``barge_in_check``.

    Loop:

    * Lazy-import sounddevice + soundfile.
    * Verify file exists.
    * Decode with soundfile.read → numpy array + samplerate.
    * Hand off to ``sd.play`` (non-blocking).
    * If ``barge_in_check`` provided: poll every ``barge_in_poll_seconds``
      while the stream is active. Return interrupted=True on True.
    * Else: ``sd.wait()`` to block until playback finishes.
    """
    sd, sf = _lazy_import_audio()

    if not path.exists():
        raise PlaybackError(f"audio file not found: {path}")

    try:
        data, samplerate = sf.read(str(path))
    except Exception as exc:  # noqa: BLE001
        raise PlaybackError(f"failed to decode {path}: {exc}") from exc

    bytes_played = _estimate_bytes(data)
    start = time.monotonic()
    interrupted = False

    try:
        sd.play(data, samplerate)
    except Exception as exc:  # noqa: BLE001
        raise PlaybackError(f"sounddevice.play failed: {exc}") from exc

    try:
        if barge_in_check is None:
            # Block until playback ends — wrap so we don't pin the event loop.
            await asyncio.to_thread(sd.wait)
        else:
            # Poll loop. ``sd.get_stream().active`` flips False once
            # playback ends naturally; ``sd.stop()`` flips it on barge-in.
            while True:
                stream = sd.get_stream()
                if not getattr(stream, "active", False):
                    break
                if barge_in_check():
                    sd.stop()
                    interrupted = True
                    break
                await asyncio.sleep(barge_in_poll_seconds)
    except PlaybackError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PlaybackError(f"playback loop failed: {exc}") from exc

    return PlaybackResult(
        duration_seconds=time.monotonic() - start,
        interrupted=interrupted,
        bytes_played=bytes_played,
    )


def _estimate_bytes(data: Any) -> int:
    """Best-effort byte count for the decoded array (informational only)."""
    nbytes = getattr(data, "nbytes", None)
    if isinstance(nbytes, int):
        return nbytes
    try:
        return len(data)
    except TypeError:
        return 0


__all__ = [
    "PlaybackError",
    "PlaybackResult",
    "play_audio_file",
    "synthesize_and_play",
]
