"""Tests for Groq STT (fast/cheap Whisper transcription)."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opencomputer.voice.groq_stt import (
    GroqNotInstalled,
    transcribe_audio_groq,
)


class _FakeTranscriptions:
    """Stand-in for groq.Groq().audio.transcriptions."""
    captured: dict = {}

    @classmethod
    def create(cls, **kwargs):
        cls.captured = dict(kwargs)
        return SimpleNamespace(text="this is a fake transcript")


class _FakeAudio:
    transcriptions = _FakeTranscriptions


class _FakeClient:
    def __init__(self, api_key=None, **kwargs):
        self.api_key = api_key
        self.audio = _FakeAudio


def _fake_module():
    return SimpleNamespace(Groq=_FakeClient)


@pytest.fixture
def audio_file(tmp_path):
    """Create a fake audio file for tests."""
    p = tmp_path / "test.mp3"
    p.write_bytes(b"FAKE_AUDIO_BYTES" * 100)  # ~1.6KB
    return p


def test_basic_transcribe(audio_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    result = transcribe_audio_groq(audio_file, groq_module=_fake_module())
    assert result == "this is a fake transcript"


def test_passes_default_model(audio_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    transcribe_audio_groq(audio_file, groq_module=_fake_module())
    assert _FakeTranscriptions.captured["model"] == "whisper-large-v3"


def test_passes_explicit_model(audio_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    transcribe_audio_groq(
        audio_file,
        model="whisper-large-v3-turbo",
        groq_module=_fake_module(),
    )
    assert _FakeTranscriptions.captured["model"] == "whisper-large-v3-turbo"


def test_passes_language_hint(audio_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    transcribe_audio_groq(
        audio_file, language="en", groq_module=_fake_module()
    )
    assert _FakeTranscriptions.captured["language"] == "en"


def test_omits_language_when_none(audio_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    transcribe_audio_groq(audio_file, groq_module=_fake_module())
    assert "language" not in _FakeTranscriptions.captured


def test_missing_file_raises():
    with pytest.raises(ValueError, match="audio file not found"):
        transcribe_audio_groq(
            Path("/nonexistent/audio.mp3"),
            api_key="x",
            groq_module=_fake_module(),
        )


def test_empty_file_raises(tmp_path):
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="audio file is empty"):
        transcribe_audio_groq(empty, api_key="x", groq_module=_fake_module())


def test_oversized_file_raises(tmp_path):
    huge = tmp_path / "huge.mp3"
    # 26MB file > 25MB cap
    huge.write_bytes(b"x" * (26 * 1024 * 1024))
    with pytest.raises(ValueError, match="exceeds Groq STT limit"):
        transcribe_audio_groq(huge, api_key="x", groq_module=_fake_module())


def test_unknown_model_warns_but_proceeds(audio_file, monkeypatch, caplog):
    """Unknown models pass through (Groq adds models periodically) but log a warning."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    import logging
    with caplog.at_level(logging.WARNING):
        result = transcribe_audio_groq(
            audio_file,
            model="future-model-v9",
            groq_module=_fake_module(),
        )
    assert result == "this is a fake transcript"
    assert any("not in known set" in r.message for r in caplog.records)


def test_no_api_key_raises(audio_file, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="no API key"):
        transcribe_audio_groq(audio_file, groq_module=_fake_module())


def test_explicit_api_key_used(audio_file, monkeypatch):
    """Explicit api_key overrides env."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    captured_key = {}

    class _CapturingClient:
        def __init__(self, api_key=None, **kwargs):
            captured_key["key"] = api_key
            self.audio = _FakeAudio

    capturing_module = SimpleNamespace(Groq=_CapturingClient)
    transcribe_audio_groq(audio_file, api_key="explicit-key", groq_module=capturing_module)
    assert captured_key["key"] == "explicit-key"


def test_api_failure_wrapped(audio_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    class _BoomTranscriptions:
        @classmethod
        def create(cls, **kwargs):
            raise RuntimeError("network down")

    class _BoomAudio:
        transcriptions = _BoomTranscriptions

    class _BoomClient:
        def __init__(self, api_key=None, **kwargs):
            self.audio = _BoomAudio

    boom = SimpleNamespace(Groq=_BoomClient)
    with pytest.raises(RuntimeError, match="Groq STT failed"):
        transcribe_audio_groq(audio_file, groq_module=boom)


def test_empty_transcript_raises(audio_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    class _SilentTranscriptions:
        @classmethod
        def create(cls, **kwargs):
            return SimpleNamespace(text="")

    class _SilentAudio:
        transcriptions = _SilentTranscriptions

    class _SilentClient:
        def __init__(self, api_key=None, **kwargs):
            self.audio = _SilentAudio

    silent = SimpleNamespace(Groq=_SilentClient)
    with pytest.raises(RuntimeError, match="empty transcript"):
        transcribe_audio_groq(audio_file, groq_module=silent)


def test_dict_response_supported(audio_file, monkeypatch):
    """Some SDK versions return a dict-like response."""
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    class _DictTranscriptions:
        @classmethod
        def create(cls, **kwargs):
            return {"text": "dict-style response"}

    class _DictAudio:
        transcriptions = _DictTranscriptions

    class _DictClient:
        def __init__(self, api_key=None, **kwargs):
            self.audio = _DictAudio

    dict_module = SimpleNamespace(Groq=_DictClient)
    result = transcribe_audio_groq(audio_file, groq_module=dict_module)
    assert result == "dict-style response"


def test_strips_whitespace(audio_file, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    class _PaddedTranscriptions:
        @classmethod
        def create(cls, **kwargs):
            return SimpleNamespace(text="  spaced  \n")

    class _PaddedAudio:
        transcriptions = _PaddedTranscriptions

    class _PaddedClient:
        def __init__(self, api_key=None, **kwargs):
            self.audio = _PaddedAudio

    padded = SimpleNamespace(Groq=_PaddedClient)
    assert transcribe_audio_groq(audio_file, groq_module=padded) == "spaced"


def test_missing_library_raises_helpful_error(audio_file):
    with patch("opencomputer.voice.groq_stt._import_groq") as mock_imp:
        mock_imp.side_effect = GroqNotInstalled(
            "groq not installed. Install with `pip install groq`."
        )
        with pytest.raises(GroqNotInstalled, match="not installed"):
            transcribe_audio_groq(audio_file, api_key="x")
