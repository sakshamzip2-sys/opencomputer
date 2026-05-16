"""Tests for ``VoiceSynthesizeLocalTool`` — the local NeuTTS synthesis tool.

The ``neutts`` package is not installed in the dev / CI venv; these tests
monkeypatch ``NeuTTSSynthesizer`` so the tool's own logic (validation, config
assembly, error mapping) is exercised without the real model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from opencomputer.tools.voice_synthesize_local import VoiceSynthesizeLocalTool
from opencomputer.voice import tts_neutts
from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall

# pytest-asyncio runs in `asyncio_mode = "auto"`.


def _call(**arguments: Any) -> ToolCall:
    return ToolCall(id="vsl-1", name="VoiceSynthesizeLocal", arguments=arguments)


# --- schema + capability claim -------------------------------------------


def test_schema_required_fields() -> None:
    """The schema requires text + the reference clip + its transcript."""
    schema = VoiceSynthesizeLocalTool().schema
    assert schema.name == "VoiceSynthesizeLocal"
    assert set(schema.parameters["required"]) == {
        "text",
        "reference_audio",
        "reference_text",
    }


def test_capability_claim_is_implicit() -> None:
    """Local synthesis is a low-stakes, no-network action — IMPLICIT tier."""
    claims = VoiceSynthesizeLocalTool.capability_claims
    assert len(claims) == 1
    assert claims[0].tier_required is ConsentTier.IMPLICIT
    assert claims[0].capability_id == "voice.synthesize.local"
    assert isinstance(claims, tuple)


# --- validation: reject before any synthesis -----------------------------


async def test_missing_text_is_rejected() -> None:
    result = await VoiceSynthesizeLocalTool().execute(
        _call(reference_audio="ref.wav", reference_text="t")
    )
    assert result.is_error is True
    assert "text" in result.content.lower()


async def test_missing_reference_audio_is_rejected() -> None:
    result = await VoiceSynthesizeLocalTool().execute(
        _call(text="hello", reference_text="t")
    )
    assert result.is_error is True
    assert "reference_audio" in result.content.lower()


async def test_missing_reference_text_is_rejected() -> None:
    result = await VoiceSynthesizeLocalTool().execute(
        _call(text="hello", reference_audio="ref.wav")
    )
    assert result.is_error is True
    assert "reference_text" in result.content.lower()


# --- happy path -----------------------------------------------------------


async def test_synthesizes_via_neutts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A valid call builds a NeuTTSConfig and routes through the synthesizer."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    calls: list[tuple[str, str]] = []
    configs: list[Any] = []

    class _FakeSynth:
        def __init__(self, config: Any) -> None:
            configs.append(config)

        async def synthesize(self, text: str, *, out_path: str) -> str:
            calls.append((text, out_path))
            Path(out_path).write_bytes(b"RIFFfake-wav-bytes")
            return out_path

    monkeypatch.setattr(tts_neutts, "NeuTTSSynthesizer", _FakeSynth)

    result = await VoiceSynthesizeLocalTool().execute(
        _call(
            text="hello world",
            reference_audio="/tmp/ref.wav",
            reference_text="the reference transcript",
        )
    )

    assert result.is_error is False
    assert "Audio written to:" in result.content
    assert len(calls) == 1
    assert calls[0][0] == "hello world"
    # The tool threaded the reference voice into the NeuTTSConfig.
    assert configs[0].reference_audio == "/tmp/ref.wav"
    assert configs[0].reference_text == "the reference transcript"


async def test_synth_failure_becomes_a_clean_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthesizer exception is mapped to an error ToolResult, never raised."""

    class _BoomSynth:
        def __init__(self, config: Any) -> None:
            pass

        async def synthesize(self, text: str, *, out_path: str) -> str:
            raise RuntimeError("model exploded")

    monkeypatch.setattr(tts_neutts, "NeuTTSSynthesizer", _BoomSynth)

    result = await VoiceSynthesizeLocalTool().execute(
        _call(text="hi", reference_audio="/tmp/ref.wav", reference_text="r")
    )
    assert result.is_error is True
    assert "synthesis failed" in result.content.lower()
