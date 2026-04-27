"""tests/test_voice_mode_tts_playback.py"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.voice_mode.tts_playback import (
    PlaybackError,
    PlaybackResult,
    play_audio_file,
    synthesize_and_play,
)


@pytest.mark.asyncio
async def test_synthesize_and_play_happy_path(tmp_path):
    """Synthesize → play → return result with duration."""
    fake_audio_file = tmp_path / "out.opus"
    fake_audio_file.write_bytes(b"fake audio data")

    with patch(
        "extensions.voice_mode.tts_playback.synthesize_speech",
        new_callable=AsyncMock,
        return_value=fake_audio_file,
    ), patch(
        "extensions.voice_mode.tts_playback._play_file_with_barge_in",
        new_callable=AsyncMock,
        return_value=PlaybackResult(duration_seconds=1.5, interrupted=False, bytes_played=24000),
    ):
        result = await synthesize_and_play("hello world", cost_guard=MagicMock())

    assert result.interrupted is False
    assert result.duration_seconds == 1.5


@pytest.mark.asyncio
async def test_synthesize_and_play_cleans_up_temp_file(tmp_path):
    fake_audio = tmp_path / "out.opus"
    fake_audio.write_bytes(b"x")

    with patch(
        "extensions.voice_mode.tts_playback.synthesize_speech",
        new_callable=AsyncMock,
        return_value=fake_audio,
    ), patch(
        "extensions.voice_mode.tts_playback._play_file_with_barge_in",
        new_callable=AsyncMock,
        return_value=PlaybackResult(duration_seconds=1, interrupted=False, bytes_played=0),
    ):
        await synthesize_and_play("x", cost_guard=MagicMock())

    assert not fake_audio.exists(), "temp file not cleaned up"


@pytest.mark.asyncio
async def test_synthesize_and_play_handles_synthesis_failure():
    with patch(
        "extensions.voice_mode.tts_playback.synthesize_speech",
        new_callable=AsyncMock,
        side_effect=RuntimeError("TTS API down"),
    ):
        with pytest.raises(PlaybackError, match="synth"):
            await synthesize_and_play("x", cost_guard=MagicMock())


@pytest.mark.asyncio
async def test_play_audio_file_handles_missing_soundfile():
    """If soundfile not installed, PlaybackError with install hint."""
    fake_path = Path("/tmp/nonexistent.opus")
    with patch("builtins.__import__", side_effect=ImportError("no soundfile")):
        with pytest.raises(PlaybackError, match="soundfile|install"):
            await play_audio_file(fake_path)


@pytest.mark.asyncio
async def test_play_audio_file_missing_file_raises():
    bogus = Path("/tmp/definitely-does-not-exist.opus")
    with pytest.raises(PlaybackError, match="not found|does not exist"):
        await play_audio_file(bogus)


@pytest.mark.asyncio
async def test_barge_in_interrupts_playback(tmp_path):
    """barge_in_check returning True stops playback early."""
    fake_path = tmp_path / "long.wav"
    fake_path.write_bytes(b"x")

    fake_sd = MagicMock()
    fake_stream = MagicMock()
    fake_stream.active = True
    fake_sd.play = MagicMock()
    fake_sd.stop = MagicMock()
    fake_sd.get_stream.return_value = fake_stream

    fake_sf = MagicMock()
    import numpy as np
    fake_sf.read.return_value = (np.zeros(48000), 48000)  # 1 sec audio

    call_count = [0]
    def barge_check():
        call_count[0] += 1
        # Return True after 3 polls (~0.3s)
        return call_count[0] >= 3

    with patch.dict("sys.modules", {"sounddevice": fake_sd, "soundfile": fake_sf}):
        # Make stream.active become False after stop is called
        def stop_side_effect():
            fake_stream.active = False
        fake_sd.stop.side_effect = stop_side_effect

        result = await play_audio_file(
            fake_path,
            barge_in_check=barge_check,
            barge_in_poll_seconds=0.01,  # fast polling for test
        )

    assert result.interrupted is True
    fake_sd.stop.assert_called_once()


@pytest.mark.asyncio
async def test_no_barge_in_check_plays_to_completion(tmp_path):
    fake_path = tmp_path / "short.wav"
    fake_path.write_bytes(b"x")

    fake_sd = MagicMock()
    fake_sd.wait = MagicMock()  # blocks then returns
    fake_stream = MagicMock()
    fake_stream.active = False
    fake_sd.get_stream.return_value = fake_stream
    import numpy as np
    fake_sf = MagicMock()
    fake_sf.read.return_value = (np.zeros(8000), 16000)

    with patch.dict("sys.modules", {"sounddevice": fake_sd, "soundfile": fake_sf}):
        result = await play_audio_file(fake_path)

    assert result.interrupted is False


def test_playback_result_is_frozen():
    import dataclasses
    r = PlaybackResult(duration_seconds=1, interrupted=False, bytes_played=100)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.interrupted = True
