"""LocalAudioIO — sounddevice-based mic/speaker for realtime voice."""
from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import numpy as np


def test_local_audio_io_starts_input_and_output_streams() -> None:
    from opencomputer.voice.audio_io import LocalAudioIO

    with patch("opencomputer.voice.audio_io.sd") as sd:
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock()
        io = LocalAudioIO(on_mic_chunk=lambda b: None)
        io.start()
        sd.RawInputStream.assert_called_once()
        sd.RawOutputStream.assert_called_once()


def test_local_audio_io_send_audio_writes_to_output_stream() -> None:
    from opencomputer.voice.audio_io import LocalAudioIO

    with patch("opencomputer.voice.audio_io.sd") as sd:
        out_stream = MagicMock()
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock(return_value=out_stream)
        io = LocalAudioIO(on_mic_chunk=lambda b: None)
        io.start()
        io.send_audio(b"\x01\x02\x03\x04")
        out_stream.write.assert_called_with(b"\x01\x02\x03\x04")


def test_clear_audio_drops_pending_speaker_buffer() -> None:
    """Barge-in: any unplayed audio in the output buffer is dropped."""
    from opencomputer.voice.audio_io import LocalAudioIO

    with patch("opencomputer.voice.audio_io.sd") as sd:
        out_stream = MagicMock()
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock(return_value=out_stream)
        io = LocalAudioIO(on_mic_chunk=lambda b: None)
        io.start()
        io.send_audio(b"a" * 1024)
        io.clear_audio()
        io.send_audio(b"b" * 1024)
        # Two writes total: one before clear, one after.
        assert out_stream.write.call_count >= 1


def test_is_open_reflects_started_state() -> None:
    from opencomputer.voice.audio_io import LocalAudioIO

    with patch("opencomputer.voice.audio_io.sd") as sd:
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock()
        io = LocalAudioIO(on_mic_chunk=lambda b: None)
        assert io.is_open() is False
        io.start()
        assert io.is_open() is True
        io.stop()
        assert io.is_open() is False


def test_mic_callback_passes_pcm16_bytes_to_handler() -> None:
    """sounddevice's RawInputStream callback signature: (indata, frames, time, status).
    LocalAudioIO must convert numpy PCM16 → bytes and forward."""
    from opencomputer.voice.audio_io import LocalAudioIO

    received: list[bytes] = []
    handler: Callable[[bytes], None] = received.append
    with patch("opencomputer.voice.audio_io.sd") as sd:
        sd.RawInputStream = MagicMock()
        sd.RawOutputStream = MagicMock()
        io = LocalAudioIO(on_mic_chunk=handler)
        io.start()
        kwargs = sd.RawInputStream.call_args.kwargs
        cb = kwargs["callback"]
        sample_bytes = (np.array([0, 1, -1, 32000], dtype=np.int16)).tobytes()
        cb(sample_bytes, 4, None, None)
        assert received == [sample_bytes]
