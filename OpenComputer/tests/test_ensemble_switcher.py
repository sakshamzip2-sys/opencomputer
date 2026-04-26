"""Tests for ensemble.PersonaSwitcher + PersonaSlashCommand (Phase 7.A)."""

from __future__ import annotations

import pytest

from opencomputer.ensemble.persona_command import PersonaSlashCommand
from opencomputer.ensemble.switcher import PersonaNotFound, PersonaSwitcher
from plugin_sdk.runtime_context import RuntimeContext


def _make_profiles(tmp_path, names: list[str]) -> None:
    """Create per-profile subdirs with SOUL + MEMORY files."""
    for n in names:
        d = tmp_path / "profiles" / n
        d.mkdir(parents=True)
        (d / "SOUL.md").write_text(f"# {n} identity")
        (d / "MEMORY.md").write_text(f"{n} memory facts")


# ---------- PersonaSwitcher ----------


def test_switch_loads_target_soul_and_memory(tmp_path):
    _make_profiles(tmp_path, ["coder", "analyst"])
    sw = PersonaSwitcher(profiles_root=tmp_path / "profiles", current="coder")
    assert "coder identity" in sw.active_soul()
    assert "coder memory" in sw.active_memory()
    sw.switch_to("analyst")
    assert sw.current == "analyst"
    assert "analyst identity" in sw.active_soul()
    assert "analyst memory" in sw.active_memory()


def test_switch_to_unknown_raises(tmp_path):
    _make_profiles(tmp_path, ["coder"])
    sw = PersonaSwitcher(profiles_root=tmp_path / "profiles", current="coder")
    with pytest.raises(PersonaNotFound, match="ghost"):
        sw.switch_to("ghost")


def test_switch_to_self_is_noop(tmp_path):
    _make_profiles(tmp_path, ["coder"])
    events: list[dict] = []
    sw = PersonaSwitcher(
        profiles_root=tmp_path / "profiles",
        current="coder",
        on_switch=events.append,
    )
    sw.switch_to("coder")
    assert events == []
    assert sw.switch_count == 0


def test_known_profiles_lists_subdirs(tmp_path):
    _make_profiles(tmp_path, ["coder", "analyst", "writer"])
    sw = PersonaSwitcher(profiles_root=tmp_path / "profiles", current="coder")
    assert sw.known_profiles() == ["analyst", "coder", "writer"]


def test_known_profiles_returns_empty_when_dir_missing(tmp_path):
    sw = PersonaSwitcher(profiles_root=tmp_path / "ghost", current="coder")
    assert sw.known_profiles() == []


def test_switch_emits_handoff_event(tmp_path):
    _make_profiles(tmp_path, ["a", "b"])
    events: list[dict] = []
    sw = PersonaSwitcher(
        profiles_root=tmp_path / "profiles",
        current="a",
        on_switch=events.append,
    )
    sw.switch_to("b")
    assert events == [{"from": "a", "to": "b"}]
    assert sw.switch_count == 1


def test_callback_failure_does_not_block_switch(tmp_path):
    _make_profiles(tmp_path, ["a", "b"])
    def boom(_event):
        raise RuntimeError("callback boom")
    sw = PersonaSwitcher(
        profiles_root=tmp_path / "profiles",
        current="a",
        on_switch=boom,
    )
    sw.switch_to("b")  # must not raise
    assert sw.current == "b"


def test_active_files_empty_when_missing(tmp_path):
    """A profile dir without SOUL/MEMORY files returns empty strings."""
    (tmp_path / "profiles" / "barebones").mkdir(parents=True)
    sw = PersonaSwitcher(
        profiles_root=tmp_path / "profiles", current="barebones",
    )
    assert sw.active_soul() == ""
    assert sw.active_memory() == ""


# ---------- /persona slash command ----------


@pytest.mark.asyncio
async def test_persona_command_lists_when_no_args(tmp_path):
    _make_profiles(tmp_path, ["coder", "analyst"])
    sw = PersonaSwitcher(profiles_root=tmp_path / "profiles", current="coder")
    cmd = PersonaSlashCommand(sw)
    result = await cmd.execute("", RuntimeContext())
    assert "Active persona: coder" in result.output
    assert "analyst" in result.output


@pytest.mark.asyncio
async def test_persona_command_switches_with_arg(tmp_path):
    _make_profiles(tmp_path, ["coder", "analyst"])
    sw = PersonaSwitcher(profiles_root=tmp_path / "profiles", current="coder")
    cmd = PersonaSlashCommand(sw)
    result = await cmd.execute("analyst", RuntimeContext())
    assert "switched" in result.output
    assert sw.current == "analyst"


@pytest.mark.asyncio
async def test_persona_command_unknown_returns_clean_error(tmp_path):
    _make_profiles(tmp_path, ["coder"])
    sw = PersonaSwitcher(profiles_root=tmp_path / "profiles", current="coder")
    cmd = PersonaSlashCommand(sw)
    result = await cmd.execute("ghost", RuntimeContext())
    assert "Error" in result.output or "not found" in result.output
    assert sw.current == "coder"  # unchanged


@pytest.mark.asyncio
async def test_persona_command_lists_marks_active(tmp_path):
    _make_profiles(tmp_path, ["coder", "analyst"])
    sw = PersonaSwitcher(profiles_root=tmp_path / "profiles", current="analyst")
    cmd = PersonaSlashCommand(sw)
    result = await cmd.execute("", RuntimeContext())
    assert "(active)" in result.output


@pytest.mark.asyncio
async def test_persona_command_with_empty_profiles_dir(tmp_path):
    sw = PersonaSwitcher(
        profiles_root=tmp_path / "ghost", current="coder",
    )
    cmd = PersonaSlashCommand(sw)
    result = await cmd.execute("", RuntimeContext())
    assert "no personas configured" in result.output
