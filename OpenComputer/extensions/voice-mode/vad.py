"""Voice Activity Detection (VAD) gate via webrtcvad.

Filters non-speech audio out of an :class:`AudioBuffer` before it reaches
the (relatively expensive) Whisper STT path. Lazy-imports ``webrtcvad`` so
``from extensions.voice_mode.vad import detect_speech`` works even on hosts
that haven't installed the optional ``voice`` extra.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .audio_capture import AudioBuffer

_log = logging.getLogger("opencomputer.voice_mode.vad")

# webrtcvad supports only these sample rates and frame durations.
_SUPPORTED_SAMPLE_RATES = (8000, 16000, 32000, 48000)
_FRAME_MS = 30  # 10/20/30 ms allowed; 30 ms balances latency vs. accuracy
_BYTES_PER_SAMPLE_INT16 = 2


@dataclass(frozen=True, slots=True)
class VadResult:
    is_speech: bool
    speech_ratio: float  # 0..1 — fraction of frames containing speech
    total_frames: int


class VadError(RuntimeError):
    """Raised when VAD cannot run (missing webrtcvad, bad audio format, etc.)."""


def _import_webrtcvad():
    """Lazy import; convert ImportError into a VadError with install hint."""
    try:
        import webrtcvad
        return webrtcvad
    except ImportError as exc:
        raise VadError(
            "webrtcvad is not installed. Install the voice extras: "
            "`pip install opencomputer[voice]` (or `pip install webrtcvad`)."
        ) from exc


def detect_speech(
    buffer: AudioBuffer,
    *,
    aggressiveness: int = 2,
    speech_threshold: float = 0.3,
) -> VadResult:
    """Detect whether ``buffer`` contains speech.

    Splits the buffer into 30 ms frames, asks webrtcvad to classify each
    frame, and aggregates the speech ratio. ``is_speech`` is ``True`` when
    ``speech_ratio >= speech_threshold``.

    webrtcvad requires:
      * 16-bit mono PCM (``dtype="int16"``, ``channels=1``)
      * Sample rate 8000, 16000, 32000, or 48000 Hz
      * Frame size of 10/20/30 ms (we use 30 ms)

    Raises:
        ValueError: ``aggressiveness`` outside 0..3 or ``speech_threshold``
            outside 0..1.
        VadError: ``webrtcvad`` not installed, or ``buffer`` does not match
            the format webrtcvad supports.
    """
    if not 0 <= aggressiveness <= 3:
        raise ValueError(
            f"aggressiveness must be in 0..3 (got {aggressiveness})"
        )
    if not 0.0 <= speech_threshold <= 1.0:
        raise ValueError(
            f"speech_threshold must be in 0.0..1.0 (got {speech_threshold})"
        )

    # Format validation BEFORE importing webrtcvad so callers learn about
    # config bugs even on machines without the wheel.
    if buffer.dtype != "int16":
        raise VadError(
            f"webrtcvad requires int16 PCM format (got dtype={buffer.dtype!r})"
        )
    if buffer.channels != 1:
        raise VadError(
            f"webrtcvad requires mono audio (got channels={buffer.channels})"
        )
    if buffer.sample_rate not in _SUPPORTED_SAMPLE_RATES:
        raise VadError(
            f"unsupported sample rate {buffer.sample_rate}; "
            f"webrtcvad only accepts {_SUPPORTED_SAMPLE_RATES}"
        )

    # 30 ms frame at 16 kHz int16 mono = 480 samples = 960 bytes.
    samples_per_frame = int(buffer.sample_rate * _FRAME_MS / 1000)
    bytes_per_frame = samples_per_frame * _BYTES_PER_SAMPLE_INT16

    pcm = buffer.pcm_bytes
    if len(pcm) < bytes_per_frame:
        # Too short to evaluate even one frame — treat as silence. This is
        # the right answer for the push-to-talk gate (don't ship sub-frame
        # blips to STT).
        return VadResult(is_speech=False, speech_ratio=0.0, total_frames=0)

    vad_mod = _import_webrtcvad()
    vad = vad_mod.Vad(aggressiveness)

    total = 0
    speech = 0
    # Iterate full frames only; trailing partial frame is dropped (webrtcvad
    # rejects non-conforming sizes).
    for start in range(0, len(pcm) - bytes_per_frame + 1, bytes_per_frame):
        frame = pcm[start : start + bytes_per_frame]
        try:
            if vad.is_speech(frame, buffer.sample_rate):
                speech += 1
        except Exception as exc:  # noqa: BLE001
            # webrtcvad raises on malformed input — surface as VadError so
            # callers don't have to know the underlying exception type.
            raise VadError(f"webrtcvad rejected frame: {exc}") from exc
        total += 1

    if total == 0:
        return VadResult(is_speech=False, speech_ratio=0.0, total_frames=0)

    ratio = speech / total
    return VadResult(
        is_speech=ratio >= speech_threshold,
        speech_ratio=ratio,
        total_frames=total,
    )
