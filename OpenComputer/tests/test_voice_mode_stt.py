"""tests/test_voice_mode_stt.py — backend auto-detection + fallback chain."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.voice_mode.audio_capture import AudioBuffer
from extensions.voice_mode.stt import SttError, TranscribeResult, transcribe


def _audio_500ms():
    return AudioBuffer(
        pcm_bytes=b"\x00\x00" * 8000, sample_rate=16000, channels=1, dtype="int16"
    )


def _empty_audio():
    return AudioBuffer(pcm_bytes=b"", sample_rate=16000, channels=1, dtype="int16")


@pytest.mark.asyncio
async def test_empty_audio_raises():
    with pytest.raises(SttError, match="empty"):
        await transcribe(_empty_audio())


@pytest.mark.asyncio
async def test_no_backend_raises_with_install_hint(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    # Simulate ALL local backends unavailable by making the backend
    # functions themselves raise ImportError — same effect as missing wheels.
    def fake_mlx(audio):
        raise ImportError("no mlx_whisper")

    def fake_cpp(audio):
        raise ImportError("no pywhispercpp")

    with patch("extensions.voice_mode.stt._transcribe_mlx_whisper", side_effect=fake_mlx), \
         patch("extensions.voice_mode.stt._transcribe_whisper_cpp", side_effect=fake_cpp):
        with pytest.raises(SttError, match="install"):
            await transcribe(_audio_500ms())


@pytest.mark.asyncio
async def test_openai_api_when_key_set(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake_cost_guard = MagicMock()

    with patch(
        "extensions.voice_mode.stt._transcribe_openai_api",
        new_callable=AsyncMock,
        return_value=TranscribeResult(
            text="hello world", backend="openai-api", duration_seconds=0.5
        ),
    ) as mock:
        result = await transcribe(_audio_500ms(), cost_guard=fake_cost_guard)
    assert result.text == "hello world"
    assert result.backend == "openai-api"
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_prefer_local_skips_api(monkeypatch):
    """Even with OPENAI_API_KEY set, prefer_local=True tries local first."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    api_called = [False]

    async def fake_api(*a, **kw):
        api_called[0] = True
        return TranscribeResult(text="api", backend="openai-api", duration_seconds=0)

    def fake_local(audio):
        return TranscribeResult(text="local", backend="mlx-whisper", duration_seconds=0)

    with patch("extensions.voice_mode.stt._transcribe_openai_api", side_effect=fake_api), \
         patch("extensions.voice_mode.stt._transcribe_mlx_whisper", side_effect=fake_local):
        result = await transcribe(_audio_500ms(), prefer_local=True)
    assert result.text == "local"
    assert api_called[0] is False


@pytest.mark.asyncio
async def test_explicit_backend_forces_path():
    """backend='openai-api' forces API even without prefer_local."""
    fake_cost_guard = MagicMock()
    with patch(
        "extensions.voice_mode.stt._transcribe_openai_api",
        new_callable=AsyncMock,
        return_value=TranscribeResult(
            text="forced api", backend="openai-api", duration_seconds=0
        ),
    ) as mock:
        result = await transcribe(
            _audio_500ms(), backend="openai-api", cost_guard=fake_cost_guard
        )
    assert result.text == "forced api"
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_local_fallback_chain(monkeypatch):
    """No API key + mlx fails + whisper-cpp succeeds → use whisper-cpp."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with patch(
        "extensions.voice_mode.stt._transcribe_mlx_whisper",
        side_effect=ImportError("not on linux"),
    ), patch(
        "extensions.voice_mode.stt._transcribe_whisper_cpp",
        return_value=TranscribeResult(
            text="from whisper-cpp", backend="whisper-cpp", duration_seconds=0
        ),
    ):
        result = await transcribe(_audio_500ms())
    assert result.text == "from whisper-cpp"
    assert result.backend == "whisper-cpp"


def test_transcribe_result_is_frozen():
    import dataclasses
    r = TranscribeResult(text="x", backend="x", duration_seconds=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.text = "y"


@pytest.mark.asyncio
async def test_invalid_backend_name_raises():
    with pytest.raises(SttError, match="unknown backend"):
        await transcribe(_audio_500ms(), backend="bogus-backend")


@pytest.mark.asyncio
async def test_explicit_mlx_backend(monkeypatch):
    """backend='mlx-whisper' forces mlx path even when API key is set."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch(
        "extensions.voice_mode.stt._transcribe_mlx_whisper",
        return_value=TranscribeResult(
            text="mlx out", backend="mlx-whisper", duration_seconds=0
        ),
    ) as mock:
        result = await transcribe(_audio_500ms(), backend="mlx-whisper")
    assert result.text == "mlx out"
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_explicit_whisper_cpp_backend(monkeypatch):
    """backend='whisper-cpp' forces whisper-cpp path."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch(
        "extensions.voice_mode.stt._transcribe_whisper_cpp",
        return_value=TranscribeResult(
            text="cpp out", backend="whisper-cpp", duration_seconds=0
        ),
    ) as mock:
        result = await transcribe(_audio_500ms(), backend="whisper-cpp")
    assert result.text == "cpp out"
    mock.assert_called_once()


@pytest.mark.asyncio
async def test_prefer_local_falls_back_to_api(monkeypatch):
    """prefer_local=True: if both local backends fail, fall back to API
    when OPENAI_API_KEY is set (ensures no silent total failure)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def fake_api(*a, **kw):
        return TranscribeResult(text="api fallback", backend="openai-api", duration_seconds=0)

    with patch(
        "extensions.voice_mode.stt._transcribe_mlx_whisper",
        side_effect=ImportError("no mlx"),
    ), patch(
        "extensions.voice_mode.stt._transcribe_whisper_cpp",
        side_effect=ImportError("no cpp"),
    ), patch(
        "extensions.voice_mode.stt._transcribe_openai_api", side_effect=fake_api
    ):
        result = await transcribe(_audio_500ms(), prefer_local=True)
    assert result.backend == "openai-api"
    assert result.text == "api fallback"
