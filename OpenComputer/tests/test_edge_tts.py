"""Tests for Edge TTS (free, no-API-key voice provider)."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opencomputer.voice.edge_tts import (
    EdgeTTSNotInstalled,
    EdgeVoiceConfig,
    synthesize_speech_edge,
)


class _FakeCommunicate:
    """Stand-in for edge_tts.Communicate."""

    captured: dict = {}

    def __init__(self, text, voice, *, rate="+0%", volume="+0%", pitch="+0Hz"):
        type(self).captured = {
            "text": text, "voice": voice,
            "rate": rate, "volume": volume, "pitch": pitch,
        }

    async def save(self, out_path: str) -> None:
        Path(out_path).write_bytes(b"FAKE_MP3_AUDIO_BYTES_FOR_TEST")


def _fake_module():
    return SimpleNamespace(Communicate=_FakeCommunicate)


def test_synthesize_writes_mp3(tmp_path):
    out = synthesize_speech_edge(
        "hello world", dest_dir=tmp_path, edge_tts_module=_fake_module(),
    )
    assert out.exists()
    assert out.suffix == ".mp3"
    assert out.read_bytes().startswith(b"FAKE_MP3")


def test_synthesize_uses_default_voice(tmp_path):
    synthesize_speech_edge(
        "hello", dest_dir=tmp_path, edge_tts_module=_fake_module(),
    )
    assert _FakeCommunicate.captured["voice"] == "en-US-AriaNeural"


def test_synthesize_passes_cfg(tmp_path):
    cfg = EdgeVoiceConfig(
        voice="en-GB-SoniaNeural", rate="+25%", volume="-10%", pitch="+50Hz"
    )
    synthesize_speech_edge(
        "test", cfg=cfg, dest_dir=tmp_path, edge_tts_module=_fake_module(),
    )
    captured = _FakeCommunicate.captured
    assert captured["voice"] == "en-GB-SoniaNeural"
    assert captured["rate"] == "+25%"
    assert captured["volume"] == "-10%"
    assert captured["pitch"] == "+50Hz"


def test_empty_text_raises():
    with pytest.raises(ValueError, match="must be non-empty"):
        synthesize_speech_edge("", edge_tts_module=_fake_module())


def test_whitespace_text_raises():
    with pytest.raises(ValueError, match="must be non-empty"):
        synthesize_speech_edge("   ", edge_tts_module=_fake_module())


def test_oversized_text_raises():
    huge = "x" * 9000
    with pytest.raises(ValueError, match="exceeds Edge TTS limit"):
        synthesize_speech_edge(huge, edge_tts_module=_fake_module())


def test_synthesis_failure_wrapped(tmp_path):
    class _Boom:
        def __init__(self, *args, **kwargs):
            pass

        async def save(self, out_path):
            raise RuntimeError("network down")

    boom = SimpleNamespace(Communicate=_Boom)
    with pytest.raises(RuntimeError, match="Edge TTS synthesis failed"):
        synthesize_speech_edge("hi", dest_dir=tmp_path, edge_tts_module=boom)


def test_empty_output_raises(tmp_path):
    class _Silent:
        def __init__(self, *args, **kwargs):
            pass

        async def save(self, out_path):
            Path(out_path).write_bytes(b"")

    silent = SimpleNamespace(Communicate=_Silent)
    with pytest.raises(RuntimeError, match="produced no output"):
        synthesize_speech_edge("hi", dest_dir=tmp_path, edge_tts_module=silent)


def test_creates_dest_dir(tmp_path):
    out_dir = tmp_path / "deep" / "nested" / "dir"
    assert not out_dir.exists()
    synthesize_speech_edge("hi", dest_dir=out_dir, edge_tts_module=_fake_module())
    assert out_dir.exists()


def test_unique_filename_per_call(tmp_path):
    out1 = synthesize_speech_edge("a", dest_dir=tmp_path, edge_tts_module=_fake_module())
    out2 = synthesize_speech_edge("b", dest_dir=tmp_path, edge_tts_module=_fake_module())
    assert out1 != out2


def test_missing_library_raises_helpful_error():
    with patch("opencomputer.voice.edge_tts._import_edge_tts") as mock_imp:
        mock_imp.side_effect = EdgeTTSNotInstalled(
            "edge-tts not installed. Install with `pip install edge-tts`."
        )
        with pytest.raises(EdgeTTSNotInstalled, match="not installed"):
            synthesize_speech_edge("hi")


def test_cfg_defaults():
    cfg = EdgeVoiceConfig()
    assert cfg.voice == "en-US-AriaNeural"
    assert cfg.rate == "+0%"
    assert cfg.volume == "+0%"
    assert cfg.pitch == "+0Hz"


def test_cfg_is_frozen():
    cfg = EdgeVoiceConfig()
    with pytest.raises((AttributeError, TypeError)):
        cfg.voice = "different"  # type: ignore[misc]
