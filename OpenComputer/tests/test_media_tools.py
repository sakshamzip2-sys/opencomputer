"""Tests for extensions/media-tools/ (C.3 MVP).

ImageInfo runs against a real generated PNG. TTS and AudioTranscribe
tests exercise the missing-backend error paths to keep the suite cheap
+ deterministic — no network, no model download.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest


def _load_module(name: str, rel_path: str):
    """Load a media-tools module by name + path under extensions/media-tools/."""
    full = (
        Path(__file__).resolve().parent.parent
        / "extensions"
        / "media-tools"
        / rel_path
    )
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, full)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ─── ImageInfo ────────────────────────────────────────────────────────


def _write_sample_png(path: Path) -> Path:
    from PIL import Image

    img = Image.new("RGB", (32, 16), color=(255, 0, 0))
    img.save(path, format="PNG")
    return path


def test_image_info_returns_metadata(tmp_path: Path):
    mod = _load_module("media_tools_image_info_test", "image_info.py")
    p = _write_sample_png(tmp_path / "red.png")

    meta = mod.inspect_image(p)
    assert meta.format == "PNG"
    assert meta.width == 32
    assert meta.height == 16
    assert meta.mode == "RGB"


def test_image_info_raises_on_missing_file(tmp_path: Path):
    mod = _load_module("media_tools_image_info_test", "image_info.py")
    with pytest.raises(FileNotFoundError):
        mod.inspect_image(tmp_path / "ghost.png")


# ─── TTSGenerate ──────────────────────────────────────────────────────


def test_tts_rejects_empty_text(tmp_path: Path):
    """TTSGenerate.synthesize raises ValueError for empty input."""
    import asyncio

    mod = _load_module("media_tools_tts_test", "tts_generate.py")

    async def runner():
        with pytest.raises(ValueError):
            await mod.synthesize("", out_path=tmp_path / "out.mp3")

    asyncio.run(runner())


def test_tts_unavailable_error_when_module_missing(tmp_path: Path, monkeypatch):
    """When edge-tts can't be imported, raises EdgeTTSUnavailableError."""
    import asyncio

    mod = _load_module("media_tools_tts_test", "tts_generate.py")

    saved = sys.modules.get("edge_tts", "__missing__")
    sys.modules["edge_tts"] = None  # type: ignore[assignment]
    try:

        async def runner():
            with pytest.raises(mod.EdgeTTSUnavailableError):
                await mod.synthesize("hello", out_path=tmp_path / "x.mp3")

        asyncio.run(runner())
    finally:
        if saved == "__missing__":
            sys.modules.pop("edge_tts", None)
        else:
            sys.modules["edge_tts"] = saved


# ─── AudioTranscribe ──────────────────────────────────────────────────


def test_audio_transcribe_raises_when_no_backend(tmp_path: Path, monkeypatch):
    """When neither mlx-whisper nor pywhispercpp is installed, clear error."""
    mod = _load_module("media_tools_stt_test", "audio_transcribe.py")

    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")  # not a real wav

    monkeypatch.setattr(mod, "_try_import_mlx_whisper", lambda: None)
    monkeypatch.setattr(mod, "_try_import_pywhispercpp", lambda: None)

    with pytest.raises(mod.WhisperBackendUnavailableError):
        mod.transcribe(audio)


def test_audio_transcribe_uses_mlx_when_available(tmp_path: Path, monkeypatch):
    """If mlx-whisper is importable, transcribe routes through it."""
    mod = _load_module("media_tools_stt_test", "audio_transcribe.py")
    audio = tmp_path / "stub.wav"
    audio.write_bytes(b"stub")

    class _FakeMlx:
        @staticmethod
        def transcribe(p):
            return {"text": "hello world"}

    import platform as _platform
    monkeypatch.setattr(mod, "_try_import_mlx_whisper", lambda: _FakeMlx)
    monkeypatch.setattr(_platform, "system", lambda: "Darwin")

    out = mod.transcribe(audio)
    assert out.text == "hello world"
    assert out.backend == "mlx-whisper"


def test_audio_transcribe_raises_on_missing_file(tmp_path: Path):
    mod = _load_module("media_tools_stt_test", "audio_transcribe.py")
    with pytest.raises(FileNotFoundError):
        mod.transcribe(tmp_path / "ghost.wav")
