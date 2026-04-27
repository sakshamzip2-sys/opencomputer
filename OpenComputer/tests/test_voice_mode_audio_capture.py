"""tests/test_voice_mode_audio_capture.py"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from extensions.voice_mode.audio_capture import (
    AudioBuffer,
    AudioCapture,
    AudioCaptureError,
)


def test_audio_buffer_duration():
    # 16000 samples/sec, mono, int16 (2 bytes/sample) → 16000 bytes = 0.5 sec
    buf = AudioBuffer(
        pcm_bytes=b"\x00" * 16000,
        sample_rate=16000,
        channels=1,
        dtype="int16",
    )
    assert buf.duration_seconds == pytest.approx(0.5)


def test_audio_buffer_to_wav_bytes():
    buf = AudioBuffer(pcm_bytes=b"\x00\x01\x00\x02", sample_rate=16000, channels=1, dtype="int16")
    wav = buf.to_wav_bytes()
    assert wav.startswith(b"RIFF")
    assert b"WAVEfmt" in wav


def test_capture_raises_when_sounddevice_missing():
    cap = AudioCapture()
    with patch.object(cap, "_import_sd", side_effect=AudioCaptureError("install sounddevice")):
        with pytest.raises(AudioCaptureError, match="install"):
            cap.start()


def test_capture_start_stop_lifecycle():
    cap = AudioCapture(sample_rate=16000, channels=1)
    fake_sd = MagicMock()
    fake_stream = MagicMock()
    fake_sd.InputStream.return_value = fake_stream

    with patch.object(cap, "_import_sd", return_value=fake_sd):
        cap.start()
        assert cap.is_recording() is True
        # Simulate the callback receiving 100 bytes of audio
        cap._chunks.append(b"\x00" * 100)
        buffer = cap.stop()

    assert cap.is_recording() is False
    assert buffer.pcm_bytes == b"\x00" * 100
    assert buffer.sample_rate == 16000
    fake_stream.start.assert_called_once()
    fake_stream.stop.assert_called_once()
    fake_stream.close.assert_called_once()


def test_capture_double_start_raises():
    cap = AudioCapture()
    fake_sd = MagicMock()
    fake_sd.InputStream.return_value = MagicMock()
    with patch.object(cap, "_import_sd", return_value=fake_sd):
        cap.start()
        with pytest.raises(AudioCaptureError, match="already in progress"):
            cap.start()


def test_capture_stop_without_start_raises():
    cap = AudioCapture()
    with pytest.raises(AudioCaptureError, match="no recording"):
        cap.stop()


def test_list_devices_filters_input_only():
    cap = AudioCapture()
    fake_sd = MagicMock()
    fake_sd.query_devices.return_value = [
        {"name": "Mic", "max_input_channels": 1, "max_output_channels": 0},
        {"name": "Speaker", "max_input_channels": 0, "max_output_channels": 2},
    ]
    with patch.object(cap, "_import_sd", return_value=fake_sd):
        devs = cap.list_devices()
    assert len(devs) == 1
    assert devs[0]["name"] == "Mic"


def test_capability_namespaces_registered():
    from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES
    from plugin_sdk.consent import ConsentTier

    assert F1_CAPABILITIES.get("voice.capture") == ConsentTier.EXPLICIT
    assert F1_CAPABILITIES.get("voice.playback") == ConsentTier.IMPLICIT
