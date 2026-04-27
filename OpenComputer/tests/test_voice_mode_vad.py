"""tests/test_voice_mode_vad.py"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from extensions.voice_mode.audio_capture import AudioBuffer
from extensions.voice_mode.vad import VadError, VadResult, detect_speech


def _webrtcvad_importable() -> bool:
    """Check if webrtcvad's C extension actually loads (some Python versions fail at runtime)."""
    try:
        import webrtcvad  # noqa: F401
        return True
    except (ImportError, OSError):
        return False


_skip_no_webrtcvad = pytest.mark.skipif(
    not _webrtcvad_importable(),
    reason="webrtcvad's C extension didn't load — integration tests skipped (voice-mode is opt-in)",
)


def _silence_buffer(duration_seconds=0.6, sample_rate=16000):
    """Pure silence (zeros)."""
    n_samples = int(sample_rate * duration_seconds)
    return AudioBuffer(
        pcm_bytes=b"\x00\x00" * n_samples,  # int16 zeros
        sample_rate=sample_rate,
        channels=1,
        dtype="int16",
    )


def _noise_buffer(duration_seconds=0.6, sample_rate=16000):
    """Noise (high-amplitude random-looking values)."""
    import random
    n_samples = int(sample_rate * duration_seconds)
    random.seed(42)
    samples = []
    for _ in range(n_samples):
        v = random.randint(-20000, 20000)
        samples.append(v.to_bytes(2, "little", signed=True))
    return AudioBuffer(
        pcm_bytes=b"".join(samples),
        sample_rate=sample_rate,
        channels=1,
        dtype="int16",
    )


@_skip_no_webrtcvad
def test_silence_returns_no_speech():
    """Pure silence should NOT be classified as speech."""
    buf = _silence_buffer(duration_seconds=0.6)
    result = detect_speech(buf, aggressiveness=2)
    assert result.is_speech is False
    assert result.speech_ratio < 0.3


def test_unsupported_sample_rate_raises():
    """webrtcvad only supports 8000/16000/32000/48000 Hz."""
    buf = AudioBuffer(pcm_bytes=b"\x00" * 10000, sample_rate=22050, channels=1, dtype="int16")
    with pytest.raises(VadError, match="sample rate"):
        detect_speech(buf)


def test_stereo_raises():
    """webrtcvad requires mono."""
    buf = AudioBuffer(pcm_bytes=b"\x00" * 10000, sample_rate=16000, channels=2, dtype="int16")
    with pytest.raises(VadError, match="mono|channels"):
        detect_speech(buf)


def test_non_int16_raises():
    buf = AudioBuffer(pcm_bytes=b"\x00" * 10000, sample_rate=16000, channels=1, dtype="float32")
    with pytest.raises(VadError, match="int16|format"):
        detect_speech(buf)


def test_too_short_buffer_returns_no_speech():
    """Buffer shorter than one VAD frame should return no_speech without crashing."""
    buf = AudioBuffer(pcm_bytes=b"\x00" * 10, sample_rate=16000, channels=1, dtype="int16")
    result = detect_speech(buf)
    assert result.is_speech is False


def test_aggressiveness_validated():
    buf = _silence_buffer()
    with pytest.raises(ValueError, match="aggressiveness"):
        detect_speech(buf, aggressiveness=5)
    with pytest.raises(ValueError, match="aggressiveness"):
        detect_speech(buf, aggressiveness=-1)


def test_speech_threshold_validated():
    buf = _silence_buffer()
    with pytest.raises(ValueError, match="threshold"):
        detect_speech(buf, speech_threshold=1.5)


def test_missing_webrtcvad_raises_vaderror():
    """If webrtcvad isn't installed, VadError with install hint."""
    buf = _silence_buffer()
    with patch("builtins.__import__", side_effect=ImportError("no webrtcvad")):
        with pytest.raises(VadError, match="webrtcvad"):
            detect_speech(buf)


def test_vad_result_shape():
    """VadResult is frozen dataclass with is_speech, speech_ratio, total_frames."""
    import dataclasses
    r = VadResult(is_speech=True, speech_ratio=0.5, total_frames=20)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.is_speech = False
