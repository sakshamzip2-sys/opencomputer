"""Tests for the Edge-TTS adapter that plugs into ``VoiceConfig``.

Verifies the integration between :func:`opencomputer.voice.synthesize_speech`
(provider="edge") and the existing :mod:`opencomputer.voice.edge_tts`
backend, plus the ffmpeg re-mux path for non-MP3 formats.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.voice import VoiceConfig, synthesize_speech
from opencomputer.voice.tts_edge import (
    DEFAULT_EDGE_VOICE,
    EdgeTTSError,
    _speed_to_rate,
    synthesize_edge_speech,
)


class _FakeCommunicate:
    """Stand-in for edge_tts.Communicate that writes a deterministic file."""

    captured: dict = {}

    def __init__(self, text, voice, *, rate="+0%", volume="+0%", pitch="+0Hz"):
        type(self).captured = {
            "text": text, "voice": voice,
            "rate": rate, "volume": volume, "pitch": pitch,
        }

    async def save(self, out_path: str) -> None:
        Path(out_path).write_bytes(b"FAKE_MP3_AUDIO")


def _fake_edge_tts_module():
    return SimpleNamespace(Communicate=_FakeCommunicate)


# ─── _speed_to_rate ─────────────────────────────────────────────


def test_speed_to_rate_baseline_returns_zero_pct():
    assert _speed_to_rate(1.0) == "+0%"


def test_speed_to_rate_faster():
    assert _speed_to_rate(1.2) == "+20%"


def test_speed_to_rate_slower():
    assert _speed_to_rate(0.85) == "-15%"


# ─── VoiceConfig provider field ────────────────────────────────


def test_voice_config_default_provider_is_openai():
    cfg = VoiceConfig()
    assert cfg.provider == "openai"


def test_voice_config_accepts_edge_provider():
    cfg = VoiceConfig(provider="edge", voice="en-US-AriaNeural", format="mp3")
    assert cfg.provider == "edge"
    assert cfg.voice == "en-US-AriaNeural"


def test_voice_config_rejects_unknown_provider():
    cfg = VoiceConfig(provider="totally-fake", voice="en-US-AriaNeural", format="mp3")
    with pytest.raises(ValueError, match="provider must be one of"):
        synthesize_speech("hi", cfg=cfg)


# ─── synthesize_edge_speech direct ─────────────────────────────


def test_synthesize_edge_speech_writes_mp3(tmp_path, monkeypatch):
    cfg = VoiceConfig(provider="edge", voice=DEFAULT_EDGE_VOICE, format="mp3")
    fake = _fake_edge_tts_module()

    with patch("opencomputer.voice.edge_tts._import_edge_tts", return_value=fake):
        out = synthesize_edge_speech("hello world", cfg=cfg, dest_dir=tmp_path)

    assert out.exists()
    assert out.suffix == ".mp3"
    assert out.read_bytes() == b"FAKE_MP3_AUDIO"


def test_synthesize_edge_passes_voice_and_rate(tmp_path):
    cfg = VoiceConfig(provider="edge", voice="en-GB-RyanNeural",
                      format="mp3", speed=1.5)
    fake = _fake_edge_tts_module()

    with patch("opencomputer.voice.edge_tts._import_edge_tts", return_value=fake):
        synthesize_edge_speech("hi", cfg=cfg, dest_dir=tmp_path)

    assert _FakeCommunicate.captured["voice"] == "en-GB-RyanNeural"
    assert _FakeCommunicate.captured["rate"] == "+50%"


def test_synthesize_edge_default_voice_when_empty(tmp_path):
    cfg = VoiceConfig(provider="edge", voice="", format="mp3")
    fake = _fake_edge_tts_module()

    with patch("opencomputer.voice.edge_tts._import_edge_tts", return_value=fake):
        synthesize_edge_speech("hi", cfg=cfg, dest_dir=tmp_path)

    assert _FakeCommunicate.captured["voice"] == DEFAULT_EDGE_VOICE


def test_synthesize_edge_empty_text_raises():
    cfg = VoiceConfig(provider="edge", format="mp3")
    with pytest.raises(ValueError, match="text must be non-empty"):
        synthesize_edge_speech("   ", cfg=cfg)


def test_synthesize_edge_invalid_format_raises():
    cfg = VoiceConfig(provider="edge", format="m4a")  # not in _FORMAT_EXTENSIONS
    with pytest.raises(ValueError, match="format must be one of"):
        synthesize_edge_speech("hi", cfg=cfg)


# ─── ffmpeg re-mux ──────────────────────────────────────────────


def test_synthesize_edge_opus_requires_ffmpeg(tmp_path):
    cfg = VoiceConfig(provider="edge", format="opus")
    fake = _fake_edge_tts_module()

    with patch("opencomputer.voice.edge_tts._import_edge_tts", return_value=fake):
        with patch("opencomputer.voice.tts_edge.shutil.which", return_value=None):
            with pytest.raises(EdgeTTSError, match="ffmpeg"):
                synthesize_edge_speech("hi", cfg=cfg, dest_dir=tmp_path)


def test_synthesize_edge_opus_succeeds_with_ffmpeg(tmp_path):
    cfg = VoiceConfig(provider="edge", format="opus")
    fake = _fake_edge_tts_module()

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
        # The output path is the last argument to ffmpeg.
        Path(cmd[-1]).write_bytes(b"FAKE_OPUS")
        return MagicMock(returncode=0)

    with patch("opencomputer.voice.edge_tts._import_edge_tts", return_value=fake):
        with patch("opencomputer.voice.tts_edge.shutil.which",
                   return_value="/usr/bin/ffmpeg"):
            with patch("opencomputer.voice.tts_edge.subprocess.run",
                       side_effect=fake_run) as run_mock:
                out = synthesize_edge_speech("hi", cfg=cfg, dest_dir=tmp_path)

    assert out.exists()
    assert out.suffix == ".ogg"
    # ffmpeg called with libopus
    cmd = run_mock.call_args.args[0]
    assert "libopus" in cmd
    assert cmd[-1] == str(out)


# ─── Dispatch via synthesize_speech ────────────────────────────


def test_synthesize_speech_dispatches_to_edge_when_provider_edge(tmp_path):
    cfg = VoiceConfig(provider="edge", voice=DEFAULT_EDGE_VOICE, format="mp3")
    fake = _fake_edge_tts_module()

    with patch("opencomputer.voice.edge_tts._import_edge_tts", return_value=fake):
        out = synthesize_speech("hi", cfg=cfg, dest_dir=tmp_path)

    assert out.exists()
    assert out.suffix == ".mp3"


def test_synthesize_speech_skips_openai_voice_validation_for_edge(tmp_path):
    """``en-US-AriaNeural`` is invalid for OpenAI but legal for Edge."""
    cfg = VoiceConfig(provider="edge", voice="en-US-AriaNeural", format="mp3")
    fake = _fake_edge_tts_module()
    with patch("opencomputer.voice.edge_tts._import_edge_tts", return_value=fake):
        # Should NOT raise ValueError despite voice not being in OpenAI's set.
        synthesize_speech("hi", cfg=cfg, dest_dir=tmp_path)


def test_synthesize_speech_openai_voice_validation_still_enforced():
    """When provider="openai", invalid voice still raises."""
    cfg = VoiceConfig(provider="openai", voice="en-US-AriaNeural", format="mp3")
    with pytest.raises(ValueError, match="voice must be one of"):
        synthesize_speech("hi", cfg=cfg)


def test_synthesize_speech_format_validation_runs_for_both_providers():
    """Bad format should error before provider dispatch."""
    cfg = VoiceConfig(provider="edge", format="m4a")
    with pytest.raises(ValueError, match="format must be one of"):
        synthesize_speech("hi", cfg=cfg)


# ─── EdgeTTSError surface ──────────────────────────────────────


def test_synthesize_edge_wraps_synth_failure(tmp_path):
    cfg = VoiceConfig(provider="edge", format="mp3")

    def boom_init(*args, **kwargs):  # noqa: ANN001
        raise RuntimeError("network down")

    fake_module = SimpleNamespace(Communicate=boom_init)

    with patch("opencomputer.voice.edge_tts._import_edge_tts",
               return_value=fake_module):
        with pytest.raises(EdgeTTSError, match="Edge TTS synth failed"):
            synthesize_edge_speech("hi", cfg=cfg, dest_dir=tmp_path)
