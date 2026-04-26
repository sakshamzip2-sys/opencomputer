"""Tests for VoiceSynthesizeTool + VoiceTranscribeTool (Phase 1.1).

The underlying opencomputer.voice module is exercised by tests/test_voice.py;
here we only verify the tool wrappers — schema, capability claims, error
handling, and the call-into-voice-module contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencomputer.tools.voice_synthesize import VoiceSynthesizeTool
from opencomputer.tools.voice_transcribe import VoiceTranscribeTool
from plugin_sdk.consent import ConsentTier
from plugin_sdk.core import ToolCall

# ---------- VoiceSynthesizeTool ----------


def test_synthesize_capability_claim_implicit():
    claims = VoiceSynthesizeTool.capability_claims
    assert len(claims) == 1
    assert claims[0].capability_id == "voice.synthesize"
    assert claims[0].tier_required == ConsentTier.IMPLICIT


def test_synthesize_schema_name_pascal_case():
    tool = VoiceSynthesizeTool()
    assert tool.schema.name == "VoiceSynthesize"
    assert "text" in tool.schema.parameters["required"]


def test_synthesize_parallel_safe_true():
    assert VoiceSynthesizeTool.parallel_safe is True


def test_synthesize_rejects_empty_text():
    tool = VoiceSynthesizeTool()
    call = ToolCall(id="t1", name="VoiceSynthesize", arguments={"text": "  "})
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "non-empty" in result.content


def test_synthesize_rejects_missing_text():
    tool = VoiceSynthesizeTool()
    call = ToolCall(id="t2", name="VoiceSynthesize", arguments={})
    result = asyncio.run(tool.execute(call))
    assert result.is_error


def test_synthesize_calls_voice_module_with_config(tmp_path):
    out = tmp_path / "out.opus"
    out.write_bytes(b"fake-audio")
    fake_synth = MagicMock(return_value=str(out))
    tool = VoiceSynthesizeTool()
    call = ToolCall(
        id="t3",
        name="VoiceSynthesize",
        arguments={"text": "hello", "voice": "nova", "format": "mp3", "model": "tts-1-hd"},
    )
    with patch("opencomputer.voice.synthesize_speech", fake_synth):
        result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert str(out.resolve()) in result.content
    fake_synth.assert_called_once()
    # cfg passed positional or as kwarg — verify the VoiceConfig fields landed
    args, kwargs = fake_synth.call_args
    assert args[0] == "hello"
    cfg = kwargs.get("cfg") or (args[1] if len(args) > 1 else None)
    assert cfg is not None
    assert cfg.voice == "nova"
    assert cfg.format == "mp3"
    assert cfg.model == "tts-1-hd"


def test_synthesize_surfaces_voice_module_errors_as_tool_error():
    fake_synth = MagicMock(side_effect=RuntimeError("openai down"))
    tool = VoiceSynthesizeTool()
    call = ToolCall(id="t4", name="VoiceSynthesize", arguments={"text": "hi"})
    with patch("opencomputer.voice.synthesize_speech", fake_synth):
        result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "openai down" in result.content


# ---------- VoiceTranscribeTool ----------


def test_transcribe_capability_claim_implicit():
    claims = VoiceTranscribeTool.capability_claims
    assert len(claims) == 1
    assert claims[0].capability_id == "voice.transcribe"
    assert claims[0].tier_required == ConsentTier.IMPLICIT


def test_transcribe_schema_name_pascal_case():
    tool = VoiceTranscribeTool()
    assert tool.schema.name == "VoiceTranscribe"
    assert "audio_path" in tool.schema.parameters["required"]


def test_transcribe_rejects_relative_path():
    tool = VoiceTranscribeTool()
    call = ToolCall(id="t5", name="VoiceTranscribe", arguments={"audio_path": "out.opus"})
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "absolute" in result.content


def test_transcribe_rejects_missing_path(tmp_path):
    tool = VoiceTranscribeTool()
    call = ToolCall(
        id="t6",
        name="VoiceTranscribe",
        arguments={"audio_path": str(tmp_path / "nonexistent.opus")},
    )
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "not found" in result.content


def test_transcribe_rejects_directory(tmp_path):
    tool = VoiceTranscribeTool()
    call = ToolCall(id="t7", name="VoiceTranscribe", arguments={"audio_path": str(tmp_path)})
    result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "regular file" in result.content


def test_transcribe_happy_path(tmp_path):
    audio = tmp_path / "voice.opus"
    audio.write_bytes(b"fake-audio")
    fake_trans = MagicMock(return_value="hello world")
    tool = VoiceTranscribeTool()
    call = ToolCall(
        id="t8",
        name="VoiceTranscribe",
        arguments={"audio_path": str(audio), "language": "en"},
    )
    with patch("opencomputer.voice.transcribe_audio", fake_trans):
        result = asyncio.run(tool.execute(call))
    assert not result.is_error
    assert result.content == "hello world"
    fake_trans.assert_called_once_with(str(audio), language="en")


def test_transcribe_surfaces_errors(tmp_path):
    audio = tmp_path / "voice.opus"
    audio.write_bytes(b"fake")
    fake_trans = MagicMock(side_effect=RuntimeError("network"))
    tool = VoiceTranscribeTool()
    call = ToolCall(id="t9", name="VoiceTranscribe", arguments={"audio_path": str(audio)})
    with patch("opencomputer.voice.transcribe_audio", fake_trans):
        result = asyncio.run(tool.execute(call))
    assert result.is_error
    assert "network" in result.content


# ---------- Registration smoke test ----------


def test_taxonomy_lists_voice_capabilities():
    from opencomputer.agent.consent.capability_taxonomy import F1_CAPABILITIES

    assert F1_CAPABILITIES["voice.synthesize"] == ConsentTier.IMPLICIT
    assert F1_CAPABILITIES["voice.transcribe"] == ConsentTier.IMPLICIT
