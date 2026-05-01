"""Realtime voice context composition: tools, system prompt, resumed history.

Locks the wire-format expectations for the four CLI flags shipped with
``opencomputer voice realtime``:
* registered tools surface to the model
* persona block + identity preamble compose into the system prompt
* resumed-session messages get summarised into the prompt
* user ``--instructions`` always come last so they win
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencomputer.voice.realtime_context import (
    compose_system_prompt,
    load_profile_persona,
    load_recent_messages,
    registered_tools_for_realtime,
)


# ─── tools ───────────────────────────────────────────────────────────


def _fake_registry(schemas: list) -> object:
    """Stand-in for ``ToolRegistry`` — only ``schemas()`` is called."""
    return SimpleNamespace(schemas=lambda: schemas)


def test_registered_tools_for_realtime_maps_each_schema() -> None:
    from plugin_sdk.tool_contract import ToolSchema

    reg = _fake_registry([
        ToolSchema(
            name="Bash",
            description="Run a shell command.",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
        ),
        ToolSchema(
            name="screenshot",
            description="Capture and analyze the screen.",
            parameters={"type": "object", "properties": {}},
        ),
    ])

    out = registered_tools_for_realtime(reg)

    assert len(out) == 2
    assert out[0].type == "function"
    assert out[0].name == "Bash"
    assert out[0].description.startswith("Run a shell")
    assert out[0].parameters["properties"]["command"]["type"] == "string"
    assert out[1].name == "screenshot"


def test_registered_tools_for_realtime_empty_registry_returns_empty() -> None:
    assert registered_tools_for_realtime(_fake_registry([])) == ()


def test_registered_tools_strips_integer_enum_gemini_rejects() -> None:
    """Gemini's schema parser closes the WS with ``invalid frame payload
    data`` on ``type: integer`` + ``enum: [60, 1440, …]`` (it tries to
    read enum values as TYPE_STRING). The sanitizer drops the enum on
    non-string types so the call doesn't fail end-to-end."""
    from plugin_sdk.tool_contract import ToolSchema

    reg = _fake_registry([
        ToolSchema(
            name="discord_server",
            description="Discord server actions.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["pin_message", "unpin_message"]},
                    "auto_archive_duration": {
                        "type": "integer",
                        "enum": [60, 1440, 4320, 10080],
                        "description": "Thread archive duration minutes.",
                    },
                },
            },
        ),
    ])

    out = registered_tools_for_realtime(reg)
    props = out[0].parameters["properties"]
    # String-typed enums survive — they're what Gemini accepts natively.
    assert props["action"]["enum"] == ["pin_message", "unpin_message"]
    # Integer-typed enums get stripped; type + description still there.
    assert "enum" not in props["auto_archive_duration"]
    assert props["auto_archive_duration"]["type"] == "integer"
    assert "description" in props["auto_archive_duration"]


def test_sanitizer_recurses_into_nested_objects_and_arrays() -> None:
    """The sanitizer must walk arbitrarily-nested schemas — drop integer
    enums in nested objects and inside ``items``."""
    from opencomputer.voice.realtime_context import _sanitize_schema_for_realtime

    nested = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {
                    "inner_int": {"type": "integer", "enum": [1, 2, 3]},
                    "inner_str": {"type": "string", "enum": ["a", "b"]},
                },
            },
            "list": {
                "type": "array",
                "items": {"type": "integer", "enum": [10, 20]},
            },
        },
    }
    out = _sanitize_schema_for_realtime(nested)
    assert "enum" not in out["properties"]["outer"]["properties"]["inner_int"]
    assert out["properties"]["outer"]["properties"]["inner_str"]["enum"] == ["a", "b"]
    assert "enum" not in out["properties"]["list"]["items"]


# ─── system prompt composition ───────────────────────────────────────


def test_compose_with_no_inputs_still_returns_identity_preamble() -> None:
    """Even with nothing supplied, the composed prompt teaches the model
    that it's OpenComputer and how many tools are available — otherwise
    the model thinks it's raw Gemini."""
    out = compose_system_prompt(tool_count=0)
    assert out is not None
    assert "OpenComputer" in out
    assert "0 tools" in out


def test_compose_includes_tool_count() -> None:
    out = compose_system_prompt(tool_count=12)
    assert "12 tools" in out


def test_compose_user_instructions_come_last() -> None:
    """The user's --instructions take precedence — they appear AFTER the
    identity / persona / resumed-summary so anything contradictory wins."""
    out = compose_system_prompt(
        tool_count=5,
        user_instructions="Always reply in haiku.",
        profile_persona="You are friendly.",
        resumed_session_summary="Previously: discussed Python.",
    )
    assert out is not None
    haiku_idx = out.find("Always reply in haiku.")
    persona_idx = out.find("You are friendly.")
    resumed_idx = out.find("Previously: discussed Python.")
    identity_idx = out.find("OpenComputer")
    assert identity_idx < persona_idx < resumed_idx < haiku_idx


def test_compose_skips_empty_sections() -> None:
    out = compose_system_prompt(
        tool_count=3,
        user_instructions=None,
        profile_persona=None,
        resumed_session_summary=None,
    )
    # Only the identity block survives.
    assert out is not None
    assert "OpenComputer" in out
    assert out.count("\n\n") == 0  # no empty sections separated by blank lines


# ─── persona loading ─────────────────────────────────────────────────


def test_load_profile_persona_returns_empty_when_no_soul_md(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert load_profile_persona() == ""


def test_load_profile_persona_returns_soul_md_contents(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    soul = tmp_path / "SOUL.md"
    soul.write_text("You are warm, curious, and direct.", encoding="utf-8")
    assert load_profile_persona() == "You are warm, curious, and direct."


# ─── resumed-session messages ────────────────────────────────────────


def test_load_recent_messages_empty_when_db_missing(tmp_path, monkeypatch) -> None:
    """No DB → return empty string. Voice should degrade gracefully."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    assert load_recent_messages("nonexistent-session") == ""


def test_load_recent_messages_truncates_long_lines(tmp_path, monkeypatch) -> None:
    """Single-message snippet over 280 chars gets ellipsised so the
    composed prompt stays bounded."""
    # Build a fake SessionDB-like object via monkeypatching the import
    long_text = "x" * 600

    class _FakeDB:
        def __init__(self, _path): pass
        def get_messages(self, sid):
            return [
                SimpleNamespace(role="user", content=long_text),
                SimpleNamespace(role="assistant", content="short reply"),
            ]

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    db_path = tmp_path / "sessions.db"
    db_path.touch()
    monkeypatch.setattr("opencomputer.agent.state.SessionDB", _FakeDB)

    out = load_recent_messages("s1", limit=5)
    assert "Recent context from a prior chat session" in out
    assert "User:" in out and "Assistant:" in out
    assert "x" * 280 in out and "x" * 281 not in out  # truncated at 280
    assert "…" in out


def test_load_recent_messages_filters_unknown_roles(tmp_path, monkeypatch) -> None:
    class _FakeDB:
        def __init__(self, _path): pass
        def get_messages(self, sid):
            return [
                SimpleNamespace(role="system", content="should be filtered"),
                SimpleNamespace(role="tool", content="also filtered"),
                SimpleNamespace(role="user", content="kept"),
            ]

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "sessions.db").touch()
    monkeypatch.setattr("opencomputer.agent.state.SessionDB", _FakeDB)

    out = load_recent_messages("s1")
    assert "should be filtered" not in out
    assert "also filtered" not in out
    assert "kept" in out


def test_load_recent_messages_returns_empty_on_no_messages(tmp_path, monkeypatch) -> None:
    class _FakeDB:
        def __init__(self, _path): pass
        def get_messages(self, sid): return []

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    (tmp_path / "sessions.db").touch()
    monkeypatch.setattr("opencomputer.agent.state.SessionDB", _FakeDB)

    assert load_recent_messages("s1") == ""
