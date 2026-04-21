"""Tests for Phase 1.5 additions: skill_manage, Grep, Glob, hook engine, plugin discovery."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


def _call(tool_name: str, **args):
    from plugin_sdk.core import ToolCall

    return ToolCall(id="t1", name=tool_name, arguments=args)


# ─── skill_manage ──────────────────────────────────────────────────


def test_skill_manage_create_list_view_patch_delete(tmp_path: Path) -> None:
    from opencomputer.tools.skill_manage import SkillManageTool

    tool = SkillManageTool()

    with patch("opencomputer.tools.skill_manage._skills_root", lambda: tmp_path):
        # create
        content = (
            "---\nname: debug-nginx\ndescription: Use when nginx 502s\n---\n\n"
            "Check proxy_read_timeout.\n"
        )
        r = asyncio.run(
            tool.execute(_call("skill_manage", action="create", name="debug-nginx", content=content))
        )
        assert not r.is_error, r.content
        assert "debug-nginx" in r.content

        # list
        r = asyncio.run(tool.execute(_call("skill_manage", action="list")))
        assert "debug-nginx" in r.content

        # view
        r = asyncio.run(tool.execute(_call("skill_manage", action="view", name="debug-nginx")))
        assert "nginx 502s" in r.content

        # patch
        r = asyncio.run(
            tool.execute(
                _call(
                    "skill_manage",
                    action="patch",
                    name="debug-nginx",
                    find="proxy_read_timeout",
                    replace="proxy_read_timeout 60s",
                )
            )
        )
        assert not r.is_error, r.content

        # delete
        r = asyncio.run(tool.execute(_call("skill_manage", action="delete", name="debug-nginx")))
        assert not r.is_error, r.content
        assert not (tmp_path / "debug-nginx").exists()


def test_skill_manage_rejects_bad_frontmatter(tmp_path: Path) -> None:
    from opencomputer.tools.skill_manage import SkillManageTool

    tool = SkillManageTool()
    with patch("opencomputer.tools.skill_manage._skills_root", lambda: tmp_path):
        # no frontmatter
        r = asyncio.run(
            tool.execute(_call("skill_manage", action="create", name="bad", content="no frontmatter"))
        )
        assert r.is_error
        assert "frontmatter" in r.content.lower()


# ─── Grep / Glob ───────────────────────────────────────────────────


def test_grep_finds_pattern(tmp_path: Path) -> None:
    from opencomputer.tools.grep import GrepTool

    (tmp_path / "a.txt").write_text("hello world\nnothing here\nhello again\n")
    (tmp_path / "b.txt").write_text("no match\n")

    tool = GrepTool()
    r = asyncio.run(tool.execute(_call("Grep", pattern="hello", path=str(tmp_path))))
    assert not r.is_error, r.content
    assert "hello world" in r.content
    assert "hello again" in r.content


def test_glob_finds_files_by_pattern(tmp_path: Path) -> None:
    from opencomputer.tools.glob import GlobTool

    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")

    tool = GlobTool()
    r = asyncio.run(tool.execute(_call("Glob", pattern="*.py", path=str(tmp_path))))
    assert "a.py" in r.content
    assert "b.py" in r.content
    assert "c.txt" not in r.content


# ─── hook engine ───────────────────────────────────────────────────


def test_hook_engine_fires_blocking_handlers() -> None:
    from opencomputer.hooks.engine import HookEngine
    from plugin_sdk.core import ToolCall
    from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

    eng = HookEngine()
    calls: list[str] = []

    async def handler(ctx: HookContext) -> HookDecision:
        calls.append(ctx.tool_call.name if ctx.tool_call else "")
        if ctx.tool_call and ctx.tool_call.name == "DangerousTool":
            return HookDecision(decision="block", reason="nope")
        return HookDecision(decision="pass")

    eng.register(HookSpec(event=HookEvent.PRE_TOOL_USE, handler=handler, matcher=".*", fire_and_forget=False))

    ctx_ok = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(id="1", name="SafeTool", arguments={}),
    )
    result = asyncio.run(eng.fire_blocking(ctx_ok))
    assert result is None  # no block

    ctx_bad = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(id="2", name="DangerousTool", arguments={}),
    )
    result = asyncio.run(eng.fire_blocking(ctx_bad))
    assert result is not None
    assert result.decision == "block"


# ─── plugin discovery ──────────────────────────────────────────────


def test_plugin_discovery_scans_manifests(tmp_path: Path) -> None:
    from opencomputer.plugins.discovery import discover

    p1 = tmp_path / "plugin-one"
    p1.mkdir()
    (p1 / "plugin.json").write_text(
        json.dumps(
            {
                "id": "plugin-one",
                "name": "Plugin One",
                "version": "1.0.0",
                "description": "first plugin",
                "entry": "main",
            }
        )
    )

    p2 = tmp_path / "broken"
    p2.mkdir()
    (p2 / "plugin.json").write_text("{ not valid json")

    # directory with no manifest — should be ignored
    (tmp_path / "not-a-plugin").mkdir()

    candidates = discover([tmp_path])
    ids = [c.manifest.id for c in candidates]
    assert "plugin-one" in ids
    assert "broken" not in ids
    assert len(candidates) == 1


def test_plugin_discovery_handles_missing_search_path(tmp_path: Path) -> None:
    from opencomputer.plugins.discovery import discover

    result = discover([tmp_path / "does-not-exist"])
    assert result == []
