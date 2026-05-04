"""Tests for opencomputer.voice.tts_command (Wave 5 T9)."""

from __future__ import annotations

import pytest

from opencomputer.voice.tts_command import (
    BUILTIN_NAMES_BLOCKED,
    CommandTTSConfig,
    expand_placeholders,
    validate_provider_name,
)


def test_builtin_names_blocked_includes_known_providers():
    assert "edge" in BUILTIN_NAMES_BLOCKED
    assert "piper" in BUILTIN_NAMES_BLOCKED
    assert "openai" in BUILTIN_NAMES_BLOCKED
    assert "elevenlabs" in BUILTIN_NAMES_BLOCKED


def test_validate_rejects_builtin_name():
    with pytest.raises(ValueError, match="reserved"):
        validate_provider_name("edge")


def test_validate_accepts_custom_name():
    # Must not raise
    validate_provider_name("my-custom-tts")
    validate_provider_name("festival_local")


def test_expand_placeholders_basic():
    out = expand_placeholders(
        "say --voice {voice} {input_path}",
        input_path="/tmp/in.txt",
        output_path="/tmp/out.wav",
        voice="bob",
    )
    assert "/tmp/in.txt" in out
    assert "bob" in out


def test_expand_preserves_literal_braces():
    out = expand_placeholders(
        "echo {{literal}} > {output_path}",
        input_path="i",
        output_path="/tmp/o",
        voice="v",
    )
    assert "{literal}" in out
    assert "/tmp/o" in out


def test_expand_shell_quotes_paths_with_spaces():
    out = expand_placeholders(
        "say {input_path}",
        input_path="/tmp/has space/in.txt",
        output_path="o",
        voice="v",
    )
    # Path is preserved AND quoted (single or double)
    assert "/tmp/has space/in.txt" in out
    assert ("'" in out or '"' in out)


def test_expand_unknown_placeholder_drops():
    out = expand_placeholders(
        "say {nonexistent} {input_path}",
        input_path="/tmp/x",
        output_path="o",
    )
    # Unknown placeholder substitutes empty
    assert "/tmp/x" in out


def test_expand_text_path_falls_back_to_input_path():
    out = expand_placeholders(
        "say --text {text_path}",
        input_path="/tmp/in.txt",
        output_path="/tmp/o",
    )
    assert "/tmp/in.txt" in out


def test_expand_text_path_explicit_overrides():
    out = expand_placeholders(
        "say --text {text_path}",
        input_path="/tmp/in.txt",
        output_path="/tmp/o",
        text_path="/tmp/explicit.txt",
    )
    assert "/tmp/explicit.txt" in out
    assert "/tmp/in.txt" not in out


def test_config_validates_required_command_key():
    with pytest.raises(ValueError, match="command"):
        CommandTTSConfig.from_dict({})


def test_config_default_format_is_wav():
    cfg = CommandTTSConfig.from_dict({"command": "say {input_path}"})
    assert cfg.output_format == "wav"


def test_config_explicit_format():
    cfg = CommandTTSConfig.from_dict(
        {"command": "say {input_path}", "output_format": "mp3"},
    )
    assert cfg.output_format == "mp3"


def test_config_is_frozen():
    cfg = CommandTTSConfig.from_dict({"command": "x"})
    with pytest.raises(Exception):
        cfg.command = "y"  # type: ignore[misc]
