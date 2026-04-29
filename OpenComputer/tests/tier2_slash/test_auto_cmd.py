"""Tests for /auto slash command + /yolo deprecated alias."""

from __future__ import annotations

import pytest

from opencomputer.agent.slash_commands_impl.auto_cmd import (
    AutoCommand,
    YoloCommand,
)
from plugin_sdk import PermissionMode, RuntimeContext, effective_permission_mode


def _fresh_runtime(custom: dict | None = None) -> RuntimeContext:
    return RuntimeContext(custom=dict(custom) if custom else {})


@pytest.mark.asyncio
class TestAutoCommand:
    async def test_on_sets_canonical_and_legacy(self) -> None:
        rt = _fresh_runtime()
        result = await AutoCommand().execute("on", rt)
        assert result.handled is True
        assert rt.custom["permission_mode"] == "auto"
        assert rt.custom["yolo_session"] is True  # legacy compat
        assert effective_permission_mode(rt) == PermissionMode.AUTO

    async def test_off_clears_both(self) -> None:
        rt = _fresh_runtime({"permission_mode": "auto", "yolo_session": True})
        await AutoCommand().execute("off", rt)
        assert rt.custom.get("permission_mode") in (None, "default")
        assert rt.custom.get("yolo_session") in (None, False)

    async def test_status_no_mutation(self) -> None:
        rt = _fresh_runtime({"permission_mode": "auto", "yolo_session": True})
        result = await AutoCommand().execute("status", rt)
        assert "ON" in result.output
        assert rt.custom["permission_mode"] == "auto"  # unchanged

    async def test_toggle_on_then_off(self) -> None:
        rt = _fresh_runtime()
        await AutoCommand().execute("", rt)  # toggle on
        assert effective_permission_mode(rt) == PermissionMode.AUTO
        await AutoCommand().execute("", rt)  # toggle off
        assert effective_permission_mode(rt) == PermissionMode.DEFAULT

    async def test_off_does_not_clobber_other_modes(self) -> None:
        # If user is in PLAN mode, /auto off should not clear plan_mode.
        rt = _fresh_runtime({"permission_mode": "plan", "plan_mode": True})
        await AutoCommand().execute("off", rt)
        assert effective_permission_mode(rt) == PermissionMode.PLAN

    async def test_unknown_subcommand_shows_usage(self) -> None:
        rt = _fresh_runtime()
        result = await AutoCommand().execute("bogus", rt)
        assert "Usage" in result.output
        assert effective_permission_mode(rt) == PermissionMode.DEFAULT


@pytest.mark.asyncio
class TestYoloDeprecationAlias:
    async def test_yolo_on_forwards_to_auto(self) -> None:
        rt = _fresh_runtime()
        result = await YoloCommand().execute("on", rt)
        assert effective_permission_mode(rt) == PermissionMode.AUTO
        assert "deprecated" in result.output.lower()

    async def test_yolo_status_forwards(self) -> None:
        rt = _fresh_runtime({"permission_mode": "auto", "yolo_session": True})
        result = await YoloCommand().execute("status", rt)
        assert "ON" in result.output
