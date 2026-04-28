"""Tests for /skin and /personality slash commands."""
import pytest

from opencomputer.agent.slash_commands_impl.skin_personality_cmd import (
    PersonalityCommand,
    SkinCommand,
)
from plugin_sdk.runtime_context import RuntimeContext


def _fresh_runtime(**custom) -> RuntimeContext:
    return RuntimeContext(custom=dict(custom))


# --- /skin ---


@pytest.mark.asyncio
async def test_skin_no_args_shows_status_and_options():
    rt = _fresh_runtime()
    result = await SkinCommand().execute("", rt)
    assert "default" in result.output  # current
    assert "ares" in result.output  # available list
    assert "mono" in result.output


@pytest.mark.asyncio
async def test_skin_set_valid():
    rt = _fresh_runtime()
    result = await SkinCommand().execute("mono", rt)
    assert "mono" in result.output
    assert rt.custom["skin"] == "mono"


@pytest.mark.asyncio
async def test_skin_set_invalid_shows_options():
    rt = _fresh_runtime()
    result = await SkinCommand().execute("nonexistent", rt)
    assert "Unknown skin" in result.output
    assert "default" in result.output  # available list shown
    assert "skin" not in rt.custom


@pytest.mark.asyncio
async def test_skin_case_insensitive():
    rt = _fresh_runtime()
    result = await SkinCommand().execute("MONO", rt)
    assert rt.custom["skin"] == "mono"


# --- /personality ---


@pytest.mark.asyncio
async def test_personality_no_args_shows_default():
    rt = _fresh_runtime()
    result = await PersonalityCommand().execute("", rt)
    assert "helpful" in result.output  # default


@pytest.mark.asyncio
async def test_personality_set_valid():
    rt = _fresh_runtime()
    result = await PersonalityCommand().execute("concise", rt)
    assert rt.custom["personality"] == "concise"


@pytest.mark.asyncio
async def test_personality_set_invalid():
    rt = _fresh_runtime()
    result = await PersonalityCommand().execute("evil-overlord", rt)
    assert "Unknown personality" in result.output
    assert "personality" not in rt.custom


@pytest.mark.asyncio
async def test_personality_persists_after_set():
    rt = _fresh_runtime()
    await PersonalityCommand().execute("teacher", rt)
    result = await PersonalityCommand().execute("", rt)
    assert "teacher" in result.output


def test_metadata():
    for cls in (SkinCommand, PersonalityCommand):
        cmd = cls()
        assert cmd.name
        assert cmd.description
