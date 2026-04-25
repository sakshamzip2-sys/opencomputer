"""Tests for opencomputer.voice — TTS, STT, cost projection, cost-guard integration.

OpenAI calls are mocked so tests don't make network requests. Each test
verifies the request shape (model / voice / format / file) and that the
cost-guard is consulted before the call + recorded after.
"""

from __future__ import annotations

import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.cost_guard import BudgetExceeded, CostGuard
from opencomputer.cost_guard.guard import _reset_default_guard_for_tests
from opencomputer.voice import (
    OPENAI_STT_USD_PER_MINUTE,
    OPENAI_TTS_USD_PER_1K_CHARS,
    VoiceConfig,
    stt_cost_usd,
    synthesize_speech,
    transcribe_audio,
    tts_cost_usd,
)


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _reset_default_guard_for_tests()
    yield tmp_path
    _reset_default_guard_for_tests()


# ---------------------------------------------------------------------------
# Pricing helpers
# ---------------------------------------------------------------------------


class TestPricing:
    def test_tts_cost_zero_for_empty(self) -> None:
        assert tts_cost_usd("") == 0.0

    def test_tts_cost_for_1k_chars_is_rate(self) -> None:
        text = "x" * 1000
        assert tts_cost_usd(text) == pytest.approx(OPENAI_TTS_USD_PER_1K_CHARS["tts-1"])

    def test_tts_hd_costs_2x(self) -> None:
        text = "x" * 1000
        assert tts_cost_usd(text, model="tts-1-hd") == pytest.approx(0.030)

    def test_stt_cost_for_60s_is_rate(self) -> None:
        assert stt_cost_usd(60.0) == pytest.approx(OPENAI_STT_USD_PER_MINUTE["whisper-1"])

    def test_stt_cost_zero_for_zero_duration(self) -> None:
        assert stt_cost_usd(0) == 0.0
        assert stt_cost_usd(-5) == 0.0


# ---------------------------------------------------------------------------
# TTS — synthesize_speech
# ---------------------------------------------------------------------------


def _mock_openai_tts() -> MagicMock:
    """Construct a mock OpenAI client whose audio.speech path writes a fake file."""
    mock_client = MagicMock()

    class _Stream:
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False
        def stream_to_file(self, path):
            Path(path).write_bytes(b"OggS_fake_audio_payload_")

    mock_client.audio.speech.with_streaming_response.create.return_value = _Stream()
    return mock_client


class TestSynthesize:
    def test_basic_synthesis_writes_file(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        client = _mock_openai_tts()

        out = synthesize_speech(
            "good morning",
            cfg=VoiceConfig(),
            dest_dir=tmp_path,
            cost_guard=guard,
            openai_client=client,
        )
        assert out.exists()
        assert out.suffix == ".ogg"
        assert out.read_bytes().startswith(b"OggS")
        # Cost was recorded
        usage = guard.current_usage("openai")
        assert usage[0].daily_used > 0

    def test_passes_correct_kwargs_to_openai(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        client = _mock_openai_tts()
        synthesize_speech(
            "hi",
            cfg=VoiceConfig(model="tts-1-hd", voice="echo", format="mp3", speed=1.5),
            dest_dir=tmp_path,
            cost_guard=guard,
            openai_client=client,
        )
        kwargs = client.audio.speech.with_streaming_response.create.call_args.kwargs
        assert kwargs["model"] == "tts-1-hd"
        assert kwargs["voice"] == "echo"
        assert kwargs["response_format"] == "mp3"
        assert kwargs["speed"] == 1.5
        assert kwargs["input"] == "hi"

    def test_empty_text_rejected(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        with pytest.raises(ValueError, match="non-empty"):
            synthesize_speech("", cost_guard=guard, openai_client=_mock_openai_tts())

    def test_oversized_text_rejected(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        with pytest.raises(ValueError, match="4096"):
            synthesize_speech("x" * 5000, cost_guard=guard, openai_client=_mock_openai_tts())

    def test_invalid_voice_rejected(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        with pytest.raises(ValueError, match="voice"):
            synthesize_speech(
                "hi", cfg=VoiceConfig(voice="nope"), cost_guard=guard, openai_client=_mock_openai_tts()
            )

    def test_invalid_format_rejected(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        with pytest.raises(ValueError, match="format"):
            synthesize_speech(
                "hi", cfg=VoiceConfig(format="raw"), cost_guard=guard, openai_client=_mock_openai_tts()
            )

    def test_budget_exceeded_blocks_call(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        guard.set_limit("openai", daily=0.0001)  # tiny cap
        guard.record_usage("openai", cost_usd=0.005)  # already over
        client = _mock_openai_tts()

        with pytest.raises(BudgetExceeded):
            synthesize_speech(
                "good morning saksham",
                cost_guard=guard,
                openai_client=client,
            )
        # API was NOT called
        client.audio.speech.with_streaming_response.create.assert_not_called()

    def test_api_failure_wrapped_as_runtime_error(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        client = MagicMock()
        client.audio.speech.with_streaming_response.create.side_effect = ConnectionError("boom")
        with pytest.raises(RuntimeError, match="OpenAI TTS failed"):
            synthesize_speech(
                "hi",
                dest_dir=tmp_path,
                cost_guard=guard,
                openai_client=client,
            )


# ---------------------------------------------------------------------------
# STT — transcribe_audio
# ---------------------------------------------------------------------------


def _make_test_wav(path: Path, duration_s: float = 1.0) -> Path:
    """Write a tiny silent WAV so transcribe_audio has a real file to read."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        n_frames = int(duration_s * 16000)
        w.writeframes(b"\x00\x00" * n_frames)
    return path


def _mock_openai_stt(transcript: str = "hello world") -> MagicMock:
    mock = MagicMock()
    mock.audio.transcriptions.create.return_value = MagicMock(text=transcript)
    return mock


class TestTranscribe:
    def test_basic_transcription(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        wav = _make_test_wav(tmp_path / "hi.wav", duration_s=2.0)
        client = _mock_openai_stt(transcript="hello stock briefing")

        text = transcribe_audio(wav, cost_guard=guard, openai_client=client)
        assert text == "hello stock briefing"
        # Cost recorded
        usage = guard.current_usage("openai")
        assert usage[0].daily_used > 0

    def test_passes_language_when_provided(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        wav = _make_test_wav(tmp_path / "x.wav")
        client = _mock_openai_stt()
        transcribe_audio(wav, language="en", cost_guard=guard, openai_client=client)
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        assert kwargs["language"] == "en"

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        with pytest.raises(ValueError, match="not found"):
            transcribe_audio(tmp_path / "nope.wav", cost_guard=guard, openai_client=_mock_openai_stt())

    def test_oversized_file_rejected(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        big = tmp_path / "huge.wav"
        # Write 26 MB > 25 MB limit
        with big.open("wb") as f:
            f.seek(26 * 1024 * 1024)
            f.write(b"x")
        with pytest.raises(ValueError, match="25 MB"):
            transcribe_audio(big, cost_guard=guard, openai_client=_mock_openai_stt())

    def test_budget_exceeded_blocks_call(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        guard.set_limit("openai", daily=0.0001)
        guard.record_usage("openai", cost_usd=0.005)  # already over
        wav = _make_test_wav(tmp_path / "x.wav")
        client = _mock_openai_stt()

        with pytest.raises(BudgetExceeded):
            transcribe_audio(wav, cost_guard=guard, openai_client=client)
        client.audio.transcriptions.create.assert_not_called()

    def test_wav_duration_estimated_from_header(self, tmp_path: Path) -> None:
        guard = CostGuard(storage_path=tmp_path / "cg.json")
        wav = _make_test_wav(tmp_path / "y.wav", duration_s=3.0)
        client = _mock_openai_stt()
        transcribe_audio(wav, cost_guard=guard, openai_client=client)
        # 3s @ $0.006/min ≈ $0.0003
        usage = guard.current_usage("openai")
        assert usage[0].daily_used == pytest.approx(0.0003, abs=0.0001)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cost_estimate_text_only(self) -> None:
        from typer.testing import CliRunner

        from opencomputer.cli_voice import voice_app

        runner = CliRunner()
        result = runner.invoke(voice_app, ["cost-estimate", "--text", "hello world"])
        assert result.exit_code == 0
        assert "TTS" in result.stdout
        assert "$0.000" in result.stdout

    def test_cost_estimate_duration_only(self) -> None:
        from typer.testing import CliRunner

        from opencomputer.cli_voice import voice_app

        runner = CliRunner()
        result = runner.invoke(voice_app, ["cost-estimate", "--duration", "60"])
        assert result.exit_code == 0
        assert "STT" in result.stdout
        assert "$0.0060" in result.stdout

    def test_cost_estimate_requires_input(self) -> None:
        from typer.testing import CliRunner

        from opencomputer.cli_voice import voice_app

        runner = CliRunner()
        result = runner.invoke(voice_app, ["cost-estimate"])
        assert result.exit_code == 2
