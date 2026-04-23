"""Phase 6b tests: coding-harness plugin — tools + plan mode + discovery."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent
from plugin_sdk.injection import InjectionContext
from plugin_sdk.runtime_context import RuntimeContext

# ─── Helpers ────────────────────────────────────────────────────


def _load_module(name: str, filename: str):
    """Load a module from extensions/coding-harness/ with a unique cache name.

    Mirrors the plugin loader's synthetic-module-name approach so tests don't
    collide with other plugins that happen to have files of the same name.
    """
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "extensions" / "coding-harness" / filename
    spec = importlib.util.spec_from_file_location(f"ch_test_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"ch_test_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def _call(tool_name: str, **args):
    return ToolCall(id="t1", name=tool_name, arguments=args)


# ─── Edit tool ──────────────────────────────────────────────────


def test_edit_replaces_unique_string(tmp_path: Path) -> None:
    mod = _load_module("edit", "tools/edit.py")
    f = tmp_path / "code.py"
    f.write_text("def foo():\n    return 1\n")
    tool = mod.EditTool()
    r = asyncio.run(
        tool.execute(
            _call(
                "Edit",
                file_path=str(f),
                old_string="return 1",
                new_string="return 42",
            )
        )
    )
    assert not r.is_error, r.content
    assert f.read_text() == "def foo():\n    return 42\n"


def test_edit_errors_on_non_unique_without_replace_all(tmp_path: Path) -> None:
    mod = _load_module("edit2", "tools/edit.py")
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 1\n")
    tool = mod.EditTool()
    r = asyncio.run(
        tool.execute(
            _call("Edit", file_path=str(f), old_string="1", new_string="2"),
        )
    )
    assert r.is_error
    assert "appears 2 times" in r.content
    assert "more context" in r.content.lower() or "replace_all" in r.content
    # File unchanged
    assert f.read_text() == "x = 1\ny = 1\n"


def test_edit_replace_all(tmp_path: Path) -> None:
    mod = _load_module("edit3", "tools/edit.py")
    f = tmp_path / "code.py"
    f.write_text("x = 1\ny = 1\n")
    tool = mod.EditTool()
    r = asyncio.run(
        tool.execute(
            _call(
                "Edit",
                file_path=str(f),
                old_string="1",
                new_string="2",
                replace_all=True,
            )
        )
    )
    assert not r.is_error, r.content
    assert f.read_text() == "x = 2\ny = 2\n"


# ─── MultiEdit tool ─────────────────────────────────────────────


def test_multi_edit_atomic_rollback_on_mid_failure(tmp_path: Path) -> None:
    """If the 2nd of 3 edits fails, file must be UNCHANGED from original."""
    mod = _load_module("medit", "tools/multi_edit.py")
    f = tmp_path / "c.py"
    original = "a = 1\nb = 2\nc = 3\n"
    f.write_text(original)
    tool = mod.MultiEditTool()
    r = asyncio.run(
        tool.execute(
            _call(
                "MultiEdit",
                file_path=str(f),
                edits=[
                    {"old_string": "a = 1", "new_string": "a = 10"},
                    {"old_string": "b = NOT_PRESENT", "new_string": "b = 20"},  # fails
                    {"old_string": "c = 3", "new_string": "c = 30"},
                ],
            )
        )
    )
    assert r.is_error
    assert "Rolled back" in r.content
    # Critical invariant: file is unchanged
    assert f.read_text() == original


def test_multi_edit_all_succeed(tmp_path: Path) -> None:
    mod = _load_module("medit2", "tools/multi_edit.py")
    f = tmp_path / "c.py"
    f.write_text("a = 1\nb = 2\n")
    tool = mod.MultiEditTool()
    r = asyncio.run(
        tool.execute(
            _call(
                "MultiEdit",
                file_path=str(f),
                edits=[
                    {"old_string": "a = 1", "new_string": "a = 10"},
                    {"old_string": "b = 2", "new_string": "b = 20"},
                ],
            )
        )
    )
    assert not r.is_error, r.content
    assert f.read_text() == "a = 10\nb = 20\n"


# ─── TodoWrite ──────────────────────────────────────────────────


def test_todo_write_persists_to_sqlite(tmp_path: Path) -> None:
    """TodoWrite writes to session_state table; reads survive 'resume'."""
    mod = _load_module("tw", "tools/todo_write.py")

    # D3: the module no longer imports opencomputer.agent.config. Instead
    # the plugin's register() threads api.session_db_path through
    # set_default_db_path, or a test can pass db_path directly to the
    # tool constructor. Use the instance override for full isolation.
    db = tmp_path / "test.db"
    tool = mod.TodoWriteTool(db_path=db)
    tool.set_session_id("session-xyz")
    todos = [
        {"id": "1", "content": "Add logging", "status": "pending"},
        {
            "id": "2",
            "content": "Write tests",
            "status": "in_progress",
            "activeForm": "Writing tests",
        },
    ]
    r = asyncio.run(tool.execute(_call("TodoWrite", todos=todos)))
    assert not r.is_error, r.content

    # Roundtrip: simulate `--resume`. read_todos_for_session now takes
    # an optional db_path so callers don't need the module-level default.
    got = mod.read_todos_for_session("session-xyz", db_path=db)
    assert len(got) == 2
    assert got[1]["status"] == "in_progress"


def test_todo_write_rejects_multiple_in_progress(tmp_path: Path) -> None:
    mod = _load_module("tw2", "tools/todo_write.py")
    db = tmp_path / "t.db"
    tool = mod.TodoWriteTool(db_path=db)
    r = asyncio.run(
        tool.execute(
            _call(
                "TodoWrite",
                todos=[
                    {"id": "1", "content": "A", "status": "in_progress"},
                    {"id": "2", "content": "B", "status": "in_progress"},
                ],
            )
        )
    )
    assert r.is_error
    assert "Only one" in r.content or "in_progress" in r.content


# ─── Background process lifecycle ───────────────────────────────


def test_background_process_lifecycle() -> None:
    """Full lifecycle in a SINGLE event loop — asyncio processes can't cross loops."""
    mod = _load_module("bg", "tools/background.py")

    start = mod.StartProcessTool()
    check = mod.CheckOutputTool()
    kill = mod.KillProcessTool()

    async def full_lifecycle():
        r = await start.execute(
            _call("start_process", command="echo hello && sleep 5")
        )
        assert not r.is_error, r.content
        pid = int(r.content.split("pid=")[1].split(")")[0])

        # Give the echo a moment to land in the buffer
        await asyncio.sleep(0.5)

        r = await check.execute(_call("check_output", pid=pid))
        assert not r.is_error, r.content
        assert "hello" in r.content

        r = await kill.execute(_call("kill_process", pid=pid))
        assert not r.is_error, r.content

    asyncio.run(full_lifecycle())


def test_background_check_output_unknown_pid() -> None:
    mod = _load_module("bg2", "tools/background.py")
    check = mod.CheckOutputTool()
    r = asyncio.run(check.execute(_call("check_output", pid=999999)))
    assert r.is_error
    assert "no background process" in r.content


# ─── Plan mode injection + hook ────────────────────────────────


def test_plan_mode_injection_fires_only_when_flag_set() -> None:
    mod = _load_module("pm", "plan_mode.py")
    provider = mod.PlanModeInjectionProvider()

    ctx_on = InjectionContext(messages=(), runtime=RuntimeContext(plan_mode=True))
    out = provider.collect(ctx_on)
    assert out and "PLAN MODE" in out

    ctx_off = InjectionContext(messages=(), runtime=RuntimeContext(plan_mode=False))
    assert provider.collect(ctx_off) is None


def test_plan_mode_hook_blocks_destructive_tools() -> None:
    mod = _load_module("pm2", "plan_mode.py")

    ctx_edit = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(id="1", name="Edit", arguments={}),
        runtime=RuntimeContext(plan_mode=True),
    )
    dec = asyncio.run(mod.plan_mode_block_hook(ctx_edit))
    assert dec is not None
    assert dec.decision == "block"
    assert "plan mode" in dec.reason.lower()


def test_plan_mode_hook_passes_through_read_tools() -> None:
    """Read/Grep/Glob MUST still work in plan mode."""
    mod = _load_module("pm3", "plan_mode.py")

    ctx_read = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(id="1", name="Read", arguments={}),
        runtime=RuntimeContext(plan_mode=True),
    )
    dec = asyncio.run(mod.plan_mode_block_hook(ctx_read))
    assert dec is None  # not blocked


def test_plan_mode_hook_noop_when_flag_off() -> None:
    """Outside plan mode, the hook never blocks anything."""
    mod = _load_module("pm4", "plan_mode.py")

    ctx_edit_no_plan = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=ToolCall(id="1", name="Edit", arguments={}),
        runtime=RuntimeContext(plan_mode=False),
    )
    dec = asyncio.run(mod.plan_mode_block_hook(ctx_edit_no_plan))
    assert dec is None


# ─── Plugin discovery ──────────────────────────────────────────


def test_coding_harness_plugin_manifest_discoverable() -> None:
    from opencomputer.plugins.discovery import discover

    repo_root = Path(__file__).resolve().parent.parent
    ext_dir = repo_root / "extensions"
    candidates = discover([ext_dir])
    ids = [c.manifest.id for c in candidates]
    assert "coding-harness" in ids
    ch = next(c for c in candidates if c.manifest.id == "coding-harness")
    assert ch.manifest.kind == "mixed"
    assert ch.manifest.entry == "plugin"


def test_code_reviewer_skill_discoverable() -> None:
    """The bundled code-reviewer skill inside the plugin should be discoverable."""
    import frontmatter

    repo_root = Path(__file__).resolve().parent.parent
    skill_md = (
        repo_root
        / "extensions"
        / "coding-harness"
        / "skills"
        / "code-reviewer"
        / "SKILL.md"
    )
    assert skill_md.exists()
    post = frontmatter.load(skill_md)
    assert post.metadata.get("name") == "Code reviewer"
    desc = post.metadata.get("description", "")
    # Should contain trigger phrases
    assert "pull request" in desc.lower() or "review" in desc.lower()
    assert "diff" in desc.lower()
