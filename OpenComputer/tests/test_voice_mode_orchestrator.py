"""tests/test_voice_mode_orchestrator.py — main voice-mode loop (T5)."""
from __future__ import annotations

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.voice_mode.audio_capture import AudioBuffer
from extensions.voice_mode.voice_mode import (
    TurnResult,
    VoiceModeError,  # noqa: F401 — public API import smoke-test
    run_single_turn,
    run_voice_loop,
)


def _audio(duration: float = 1.0) -> AudioBuffer:
    return AudioBuffer(
        pcm_bytes=b"\x00" * int(16000 * duration * 2),
        sample_rate=16000,
        channels=1,
        dtype="int16",
    )


def _capture_with_audio(buffer: AudioBuffer) -> MagicMock:
    cap = MagicMock()
    cap.stop = MagicMock(return_value=buffer)
    return cap


@pytest.mark.asyncio
async def test_single_turn_happy_path():
    capture = _capture_with_audio(_audio(1.0))

    async def agent(text):
        return f"echo: {text}"

    with patch(
        "extensions.voice_mode.voice_mode.detect_speech",
        return_value=MagicMock(is_speech=True, speech_ratio=0.6),
    ), patch(
        "extensions.voice_mode.voice_mode.transcribe",
        new_callable=AsyncMock,
        return_value=MagicMock(text="hello agent", backend="openai-api", duration_seconds=0.5),
    ), patch(
        "extensions.voice_mode.voice_mode.synthesize_and_play",
        new_callable=AsyncMock,
        return_value=MagicMock(duration_seconds=1.5, interrupted=False, bytes_played=24000),
    ):
        result = await run_single_turn(
            agent_runner=agent, cost_guard=MagicMock(), capture=capture,
        )

    assert result is not None
    assert result.user_text == "hello agent"
    assert result.agent_text == "echo: hello agent"
    assert result.interrupted is False


@pytest.mark.asyncio
async def test_single_turn_too_short_returns_none():
    capture = _capture_with_audio(_audio(0.1))  # 100ms — too short

    async def agent(text):
        return "x"

    result = await run_single_turn(
        agent_runner=agent, cost_guard=MagicMock(), capture=capture,
    )
    assert result is None


@pytest.mark.asyncio
async def test_single_turn_no_speech_returns_none():
    capture = _capture_with_audio(_audio(1.0))

    async def agent(text):
        return "x"

    with patch(
        "extensions.voice_mode.voice_mode.detect_speech",
        return_value=MagicMock(is_speech=False, speech_ratio=0.05),
    ):
        result = await run_single_turn(
            agent_runner=agent, cost_guard=MagicMock(), capture=capture,
        )
    assert result is None


@pytest.mark.asyncio
async def test_single_turn_empty_transcript_returns_none():
    capture = _capture_with_audio(_audio(1.0))

    async def agent(text):
        return "x"

    with patch(
        "extensions.voice_mode.voice_mode.detect_speech",
        return_value=MagicMock(is_speech=True, speech_ratio=0.6),
    ), patch(
        "extensions.voice_mode.voice_mode.transcribe",
        new_callable=AsyncMock,
        return_value=MagicMock(text="   ", backend="openai-api", duration_seconds=0.5),
    ):
        result = await run_single_turn(
            agent_runner=agent, cost_guard=MagicMock(), capture=capture,
        )
    assert result is None


@pytest.mark.asyncio
async def test_single_turn_passes_prefer_local_through():
    """``prefer_local=True`` should be forwarded to ``transcribe`` so the
    user's --local CLI flag actually changes backend selection."""
    capture = _capture_with_audio(_audio(1.0))

    async def agent(text):
        return "ack"

    with patch(
        "extensions.voice_mode.voice_mode.detect_speech",
        return_value=MagicMock(is_speech=True, speech_ratio=0.6),
    ), patch(
        "extensions.voice_mode.voice_mode.transcribe",
        new_callable=AsyncMock,
        return_value=MagicMock(text="hi", backend="mlx-whisper", duration_seconds=0.4),
    ) as mock_transcribe, patch(
        "extensions.voice_mode.voice_mode.synthesize_and_play",
        new_callable=AsyncMock,
        return_value=MagicMock(duration_seconds=0.5, interrupted=False, bytes_played=8000),
    ):
        result = await run_single_turn(
            agent_runner=agent,
            cost_guard=MagicMock(),
            capture=capture,
            prefer_local_stt=True,
        )

    assert result is not None
    assert mock_transcribe.await_args.kwargs["prefer_local"] is True


@pytest.mark.asyncio
async def test_voice_loop_stops_on_stop_trigger(tmp_path):
    # stop_trigger returns True immediately → loop exits without invoking record_trigger.
    record_calls = [0]

    def record_trigger():
        record_calls[0] += 1
        return True

    async def agent(text):
        return "x"

    await run_voice_loop(
        agent_runner=agent,
        cost_guard=MagicMock(),
        profile_home=tmp_path,
        record_trigger=record_trigger,
        stop_trigger=lambda: True,
    )
    assert record_calls[0] == 0  # loop exited before recording


@pytest.mark.asyncio
async def test_voice_loop_swallows_turn_errors(tmp_path):
    """A single failed turn (e.g. STT error) shouldn't break the loop."""
    iterations = [0]

    def stop_after_3():
        iterations[0] += 1
        return iterations[0] > 3

    async def agent(text):
        return "x"

    with patch(
        "extensions.voice_mode.voice_mode.run_single_turn",
        new_callable=AsyncMock,
        side_effect=[
            RuntimeError("STT down"),  # first turn fails
            None,                      # second: VAD rejected
            TurnResult(
                user_text="x",
                agent_text="y",
                interrupted=False,
                user_audio_seconds=1,
                agent_speak_seconds=1,
            ),  # third: success
        ],
    ):
        # Should not raise — failures swallowed
        await run_voice_loop(
            agent_runner=agent,
            cost_guard=MagicMock(),
            profile_home=tmp_path,
            record_trigger=lambda: True,
            stop_trigger=stop_after_3,
        )


def test_turn_result_is_frozen():
    r = TurnResult(
        user_text="x",
        agent_text="y",
        interrupted=False,
        user_audio_seconds=1,
        agent_speak_seconds=1,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.user_text = "z"  # type: ignore[misc]


def test_voice_mode_error_is_runtime_error():
    assert issubclass(VoiceModeError, RuntimeError)
