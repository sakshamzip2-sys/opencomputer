"""Tests for the /persona-mode slash command."""
from __future__ import annotations

import asyncio

from plugin_sdk.runtime_context import RuntimeContext


def test_persona_mode_lists_personas_when_no_args():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    rt.custom["active_persona_id"] = "coding"
    result = asyncio.run(cmd.execute("", rt))

    assert "Active persona: coding" in result.output
    assert "companion" in result.output
    assert "coding" in result.output
    assert "(override: none)" in result.output


def test_persona_mode_lists_shows_override_when_set():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    rt.custom["active_persona_id"] = "coding"
    rt.custom["persona_id_override"] = "companion"
    result = asyncio.run(cmd.execute("", rt))

    assert "(override: companion)" in result.output


def test_persona_mode_sets_override():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    result = asyncio.run(cmd.execute("companion", rt))

    assert rt.custom.get("persona_id_override") == "companion"
    assert "companion" in result.output.lower()


def test_persona_mode_auto_clears_override():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    rt.custom["persona_id_override"] = "companion"
    result = asyncio.run(cmd.execute("auto", rt))

    assert "persona_id_override" not in rt.custom or not rt.custom.get(
        "persona_id_override"
    )
    assert "auto" in result.output.lower() or "cleared" in result.output.lower()


def test_persona_mode_rejects_unknown_id():
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    result = asyncio.run(cmd.execute("not_a_real_persona", rt))

    assert "Unknown" in result.output or "not found" in result.output.lower()
    assert "persona_id_override" not in rt.custom or not rt.custom.get(
        "persona_id_override"
    )


def test_persona_mode_set_evicts_prompt_snapshot_via_runtime_flag():
    """Setting an override should drop a marker the agent loop can read
    to invalidate its prompt snapshot. We use runtime.custom['_persona_dirty']
    as that marker — the loop reads + clears it on the next turn."""
    from opencomputer.agent.slash_commands_impl.persona_mode_cmd import (
        PersonaModeCommand,
    )

    cmd = PersonaModeCommand()
    rt = RuntimeContext()
    asyncio.run(cmd.execute("companion", rt))

    assert rt.custom.get("_persona_dirty") is True


def test_persona_mode_command_is_registered():
    """The /persona-mode command must be in the built-ins registry so
    dispatch can find it."""
    from opencomputer.agent.slash_commands import (
        get_registered_commands,
        register_builtin_slash_commands,
    )

    register_builtin_slash_commands()
    names = {getattr(c, "name", "") for c in get_registered_commands()}
    assert "persona-mode" in names
