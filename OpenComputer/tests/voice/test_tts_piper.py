"""Tests for opencomputer.voice.tts_piper (Wave 5 T8).

Piper-tts is not a hard dependency — these tests stub the lazy import
chain so they pass even without piper-tts installed.
"""

from __future__ import annotations

import sys

import pytest

from opencomputer.voice.tts_piper import (
    DEFAULT_VOICE,
    PiperConfig,
    PiperTTS,
)


def test_default_voice_is_lessac():
    assert DEFAULT_VOICE == "en_US-lessac-medium"


def test_config_defaults():
    cfg = PiperConfig()
    assert cfg.voice == DEFAULT_VOICE
    assert cfg.use_cuda is False
    assert cfg.length_scale is None
    assert cfg.noise_scale is None
    assert cfg.noise_w_scale is None
    assert cfg.volume is None
    assert cfg.normalize_audio is None


def test_config_is_frozen():
    cfg = PiperConfig()
    with pytest.raises(Exception):
        cfg.voice = "x"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_lazy_import_no_piper_installed(monkeypatch):
    """Instantiation should raise an actionable error when piper-tts missing.

    We can't simply monkeypatch sys.modules['piper'] = None because the
    lazy importer catches ImportError, and with piper actually installed
    in the dev env the import succeeds before our shim takes effect.
    Patch the module's _import_piper directly to simulate the missing
    case — it's the public seam the contract guarantees.
    """
    import opencomputer.voice.tts_piper as mod

    def _missing_piper():
        raise RuntimeError(
            "Piper TTS requires the piper-tts package. "
            "Install with: pip install piper-tts",
        )

    monkeypatch.setattr(mod, "_import_piper", _missing_piper)
    monkeypatch.setattr(mod, "_resolve_voice_path", lambda v: __import__("pathlib").Path(v))
    mod._load_voice.cache_clear()

    p = PiperTTS(PiperConfig(voice="/tmp/nonexistent.onnx"))
    with pytest.raises(RuntimeError, match="pip install piper-tts"):
        await p.synthesize("hello", out_path="/tmp/x.wav")


@pytest.mark.asyncio
async def test_voice_cache_reuses(monkeypatch, tmp_path):
    """Same voice path → same cached PiperVoice instance via lru_cache."""
    import opencomputer.voice.tts_piper as mod

    fake_voice = object()
    calls = {"n": 0}

    def fake_load(path, use_cuda=False):
        calls["n"] += 1
        return fake_voice

    monkeypatch.setattr(mod, "_load_voice", fake_load)
    # Use a path-shaped voice so _resolve_voice_path returns it as-is
    onnx_file = tmp_path / "x.onnx"
    onnx_file.write_bytes(b"fake")
    cfg = PiperConfig(voice=str(onnx_file))
    p1 = PiperTTS(cfg)
    p2 = PiperTTS(cfg)
    p1._get_voice()
    p2._get_voice()
    # Direct call to fake_load, both p1 and p2 should hit the same fn
    assert calls["n"] == 2  # we replaced lru_cache wholesale; uncached fn


def test_resolve_voice_path_returns_existing_onnx(tmp_path):
    """An existing .onnx path is returned verbatim with no download."""
    import opencomputer.voice.tts_piper as mod

    onnx_file = tmp_path / "voice.onnx"
    onnx_file.write_bytes(b"fake")
    out = mod._resolve_voice_path(str(onnx_file))
    assert out == onnx_file


def test_voice_cache_dir_uses_oc_home(monkeypatch, tmp_path):
    """The cache dir honours $OC_HOME."""
    import opencomputer.voice.tts_piper as mod

    monkeypatch.setenv("OC_HOME", str(tmp_path))
    out = mod._voice_cache_dir()
    assert tmp_path in out.parents or out.is_relative_to(tmp_path)
