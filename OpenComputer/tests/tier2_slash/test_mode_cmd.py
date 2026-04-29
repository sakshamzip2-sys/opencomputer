"""Tests for /mode unified command + /accept-edits shorthand."""

from __future__ import annotations

import pytest

from opencomputer.agent.slash_commands_impl.mode_cmd import ModeCommand
from plugin_sdk import PermissionMode, RuntimeContext, effective_permission_mode


@pytest.mark.asyncio
class TestModeCommand:
    async def test_no_arg_shows_current(self) -> None:
        rt = RuntimeContext()
        result = await ModeCommand().execute("", rt)
        assert "default" in result.output.lower()

    async def test_set_plan(self) -> None:
        rt = RuntimeContext()
        await ModeCommand().execute("plan", rt)
        assert effective_permission_mode(rt) == PermissionMode.PLAN
        assert rt.custom["plan_mode"] is True

    async def test_set_accept_edits(self) -> None:
        rt = RuntimeContext()
        await ModeCommand().execute("accept-edits", rt)
        assert effective_permission_mode(rt) == PermissionMode.ACCEPT_EDITS

    async def test_set_auto(self) -> None:
        rt = RuntimeContext()
        await ModeCommand().execute("auto", rt)
        assert effective_permission_mode(rt) == PermissionMode.AUTO
        assert rt.custom["yolo_session"] is True  # legacy mirror

    async def test_set_default_clears_keys(self) -> None:
        rt = RuntimeContext(custom={"permission_mode": "auto", "yolo_session": True})
        await ModeCommand().execute("default", rt)
        assert effective_permission_mode(rt) == PermissionMode.DEFAULT
        assert "permission_mode" not in rt.custom
        assert "yolo_session" not in rt.custom

    async def test_invalid_lists_options(self) -> None:
        rt = RuntimeContext()
        result = await ModeCommand().execute("bogus", rt)
        assert "default" in result.output and "auto" in result.output

    async def test_switching_clears_other_modes(self) -> None:
        # User in PLAN, switches to AUTO — should not retain plan_mode=True.
        rt = RuntimeContext(custom={"permission_mode": "plan", "plan_mode": True})
        await ModeCommand().execute("auto", rt)
        assert effective_permission_mode(rt) == PermissionMode.AUTO
        assert rt.custom["plan_mode"] is False
        assert rt.custom["yolo_session"] is True


# /accept-edits lives in extensions/coding-harness/slash_commands/accept_edits.py
# — see test coverage in tests/test_accept_edits_hook.py + extension tests.
