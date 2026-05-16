"""Tests for ``opencomputer.voice.tts_neutts`` — the NeuTTS local-TTS provider.

The ``neutts`` package is a heavy optional dependency and is **not** installed
in the dev / CI venv, so these tests never touch the real model: ``_load_model``
is monkeypatched with a fake that returns a small audio array. The real
``soundfile`` write path *is* exercised (``soundfile`` is a dev dependency).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import soundfile as sf

from opencomputer.voice import tts_neutts
from opencomputer.voice.tts_neutts import (
    NEUTTS_SAMPLE_RATE,
    NeuTTSConfig,
    NeuTTSSynthesizer,
    download_neutts_model,
    neutts_available,
)

# pytest-asyncio runs in `asyncio_mode = "auto"`.


class _FakeNeuTTSModel:
    """Stand-in for ``neutts.NeuTTS`` — records calls, returns silent audio."""

    def __init__(self) -> None:
        self.encoded: list[str] = []
        self.inferred: list[tuple[str, str]] = []

    def encode_reference(self, ref_audio_path: str) -> list[str]:
        self.encoded.append(ref_audio_path)
        return ["fake-ref-code"]

    def infer(self, text: str, ref_codes: Any, ref_text: str) -> np.ndarray:
        self.inferred.append((text, ref_text))
        # 0.1 s of silence at the NeuTTS sample rate.
        return np.zeros(NEUTTS_SAMPLE_RATE // 10, dtype="float32")


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> _FakeNeuTTSModel:
    """Replace ``_load_model`` with a fake — no real ``neutts`` / weights needed."""
    model = _FakeNeuTTSModel()
    monkeypatch.setattr(tts_neutts, "_load_model", lambda *a, **k: model)
    return model


# --- neutts_available ----------------------------------------------------


def test_neutts_available_is_false_when_package_absent() -> None:
    """``neutts`` is not installed in the dev venv → availability is False."""
    assert neutts_available() is False


# --- NeuTTSConfig --------------------------------------------------------


def test_config_defaults() -> None:
    """The config carries the reference voice + sensible model defaults."""
    cfg = NeuTTSConfig(reference_audio="ref.wav", reference_text="hello there")
    assert cfg.reference_audio == "ref.wav"
    assert cfg.reference_text == "hello there"
    assert cfg.backbone_repo == tts_neutts.DEFAULT_BACKBONE_REPO
    assert cfg.codec_repo == tts_neutts.DEFAULT_CODEC_REPO
    assert cfg.device == "cpu"


# --- NeuTTSSynthesizer.synthesize ----------------------------------------


async def test_synthesize_writes_a_24khz_wav(
    fake_model: _FakeNeuTTSModel, tmp_path: Any
) -> None:
    """A synth call encodes the reference, infers, and writes a 24 kHz wav."""
    ref = tmp_path / "ref.wav"
    sf.write(str(ref), np.zeros(2400, dtype="float32"), NEUTTS_SAMPLE_RATE)
    out = tmp_path / "out.wav"

    cfg = NeuTTSConfig(reference_audio=str(ref), reference_text="ref transcript")
    result = await NeuTTSSynthesizer(cfg).synthesize(
        "hello world", out_path=str(out)
    )

    assert result == str(out)
    assert out.is_file()
    # The reference was encoded once, then inference ran with the text.
    assert fake_model.encoded == [str(ref)]
    assert fake_model.inferred == [("hello world", "ref transcript")]
    # The written file is a readable wav at the NeuTTS sample rate.
    _data, sample_rate = sf.read(str(out))
    assert sample_rate == NEUTTS_SAMPLE_RATE


async def test_synthesize_rejects_empty_text(
    fake_model: _FakeNeuTTSModel, tmp_path: Any
) -> None:
    """Empty / whitespace text is rejected before any model work."""
    cfg = NeuTTSConfig(
        reference_audio=str(tmp_path / "ref.wav"), reference_text="t"
    )
    with pytest.raises(ValueError, match="non-empty"):
        await NeuTTSSynthesizer(cfg).synthesize(
            "   ", out_path=str(tmp_path / "o.wav")
        )


async def test_synthesize_rejects_missing_reference_audio(
    fake_model: _FakeNeuTTSModel, tmp_path: Any
) -> None:
    """A non-existent reference clip raises FileNotFoundError, not a crash."""
    cfg = NeuTTSConfig(
        reference_audio=str(tmp_path / "nope.wav"), reference_text="t"
    )
    with pytest.raises(FileNotFoundError, match="reference audio"):
        await NeuTTSSynthesizer(cfg).synthesize(
            "hi", out_path=str(tmp_path / "o.wav")
        )


# --- download_neutts_model + lazy import ---------------------------------


def test_download_neutts_model_warms_the_loader(
    fake_model: _FakeNeuTTSModel,
) -> None:
    """``download_neutts_model`` just triggers ``_load_model`` — no raise."""
    download_neutts_model()  # the fake loader stands in for the real download


def test_import_neutts_raises_an_actionable_error_when_absent() -> None:
    """With ``neutts`` genuinely not installed, the error names the fix."""
    with pytest.raises(RuntimeError, match=r"pip install opencomputer\[neutts\]"):
        tts_neutts._import_neutts()
