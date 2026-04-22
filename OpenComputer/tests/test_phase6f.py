"""Phase 6f tests: slash commands.

Each command is tested directly by constructing it + calling execute() on it.
A formal core SlashCommand dispatcher is a core-SDK change tracked separately;
these unit tests verify the harness's command logic independent of dispatch.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from plugin_sdk.runtime_context import RuntimeContext


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "extensions" / "coding-harness"


@pytest.fixture(autouse=True)
def _plugin_on_syspath():
    sys.path.insert(0, str(PLUGIN_ROOT))
    for mod_name in list(sys.modules):
        if mod_name.split(".")[0] in {
            "context",
            "rewind",
            "state",
            "tools",
            "hooks",
            "modes",
            "permissions",
            "slash_commands",
            "plan_mode",
        }:
            sys.modules.pop(mod_name, None)
    yield
    if str(PLUGIN_ROOT) in sys.path:
        sys.path.remove(str(PLUGIN_ROOT))


def _mutable_runtime() -> RuntimeContext:
    """RuntimeContext is frozen — but `.custom` is a mutable dict.

    Commands that want per-turn flag flipping write to runtime.custom rather
    than the top-level plan_mode/yolo_mode fields.
    """
    return RuntimeContext(plan_mode=False, yolo_mode=False, custom={})


def _ctx(tmp_path: Path):
    from context import HarnessContext
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    return HarnessContext(
        session_id="s",
        rewind_store=RewindStore(tmp_path / "rw", workspace_root=tmp_path),
        session_state=SessionStateStore(tmp_path / "ss"),
    )


# ─── /plan / /plan-off ──────────────────────────────────────────


def test_plan_on_sets_custom_flag(tmp_path):
    from slash_commands.plan import PlanOnCommand

    runtime = _mutable_runtime()
    ctx = _ctx(tmp_path)
    cmd = PlanOnCommand()

    result = asyncio.run(cmd.execute("", runtime, ctx))
    assert runtime.custom["plan_mode"] is True
    assert "Plan mode enabled" in result
    assert ctx.session_state.get("mode:plan") is True


def test_plan_off_clears_custom_flag(tmp_path):
    from slash_commands.plan import PlanOffCommand

    runtime = _mutable_runtime()
    runtime.custom["plan_mode"] = True
    ctx = _ctx(tmp_path)
    cmd = PlanOffCommand()

    result = asyncio.run(cmd.execute("", runtime, ctx))
    assert runtime.custom["plan_mode"] is False
    assert "Plan mode disabled" in result


# ─── /accept-edits ──────────────────────────────────────────────


def test_accept_edits_toggles(tmp_path):
    from slash_commands.accept_edits import AcceptEditsCommand

    runtime = _mutable_runtime()
    ctx = _ctx(tmp_path)
    cmd = AcceptEditsCommand()

    r1 = asyncio.run(cmd.execute("", runtime, ctx))
    assert runtime.custom["accept_edits"] is True
    assert "on" in r1.lower()

    r2 = asyncio.run(cmd.execute("", runtime, ctx))
    assert runtime.custom["accept_edits"] is False
    assert "off" in r2.lower()


def test_accept_edits_explicit_on_off(tmp_path):
    from slash_commands.accept_edits import AcceptEditsCommand

    runtime = _mutable_runtime()
    ctx = _ctx(tmp_path)
    cmd = AcceptEditsCommand()

    asyncio.run(cmd.execute("off", runtime, ctx))
    assert runtime.custom["accept_edits"] is False
    asyncio.run(cmd.execute("on", runtime, ctx))
    assert runtime.custom["accept_edits"] is True


# ─── /checkpoint ────────────────────────────────────────────────


def test_checkpoint_no_edited_files(tmp_path):
    from slash_commands.checkpoint import CheckpointCommand

    ctx = _ctx(tmp_path)
    runtime = _mutable_runtime()
    cmd = CheckpointCommand()
    result = asyncio.run(cmd.execute("", runtime, ctx))
    assert "No edited files" in result


def test_checkpoint_saves_tracked_files(tmp_path, monkeypatch):
    from slash_commands.checkpoint import CheckpointCommand

    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_bytes(b"print('a')")
    (tmp_path / "b.py").write_bytes(b"print('b')")

    ctx = _ctx(tmp_path)
    ctx.session_state.set("edited_files", ["a.py", "b.py"])
    runtime = _mutable_runtime()
    cmd = CheckpointCommand()
    result = asyncio.run(cmd.execute("before-refactor", runtime, ctx))
    assert "Checkpoint saved" in result
    assert "before-refactor" in result
    assert len(ctx.rewind_store.list()) == 1


# ─── /undo ──────────────────────────────────────────────────────


def test_undo_no_checkpoints(tmp_path):
    from slash_commands.undo import UndoCommand

    ctx = _ctx(tmp_path)
    runtime = _mutable_runtime()
    cmd = UndoCommand()
    result = asyncio.run(cmd.execute("", runtime, ctx))
    assert "nothing to undo" in result.lower()


def test_undo_restores_last_checkpoint(tmp_path, monkeypatch):
    from rewind.checkpoint import Checkpoint
    from slash_commands.undo import UndoCommand

    workspace = tmp_path / "w"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / "f.py").write_bytes(b"v1")

    from context import HarnessContext
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    store = RewindStore(tmp_path / "rw", workspace_root=workspace)
    store.save(Checkpoint.from_files({"f.py": b"v1"}, label="before v2"))

    (workspace / "f.py").write_bytes(b"v2")

    ctx = HarnessContext(
        session_id="s",
        rewind_store=store,
        session_state=SessionStateStore(tmp_path / "ss"),
    )
    runtime = _mutable_runtime()
    cmd = UndoCommand()
    result = asyncio.run(cmd.execute("", runtime, ctx))
    assert "Rewound 1" in result
    assert (workspace / "f.py").read_bytes() == b"v1"


def test_undo_rejects_bad_integer(tmp_path):
    from slash_commands.undo import UndoCommand

    ctx = _ctx(tmp_path)
    runtime = _mutable_runtime()
    cmd = UndoCommand()
    result = asyncio.run(cmd.execute("nope", runtime, ctx))
    assert "bad argument" in result.lower() or "nope" in result.lower()


# ─── /diff ──────────────────────────────────────────────────────


def test_diff_command_delegates_to_tool(tmp_path, monkeypatch):
    from rewind.checkpoint import Checkpoint
    from slash_commands.diff import DiffCommand

    workspace = tmp_path / "w"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / "f.py").write_bytes(b"line1\n")

    from context import HarnessContext
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    store = RewindStore(tmp_path / "rw", workspace_root=workspace)
    store.save(Checkpoint.from_files({"f.py": b"line1\n"}, label="t"))
    (workspace / "f.py").write_bytes(b"line1 changed\n")

    ctx = HarnessContext(
        session_id="s",
        rewind_store=store,
        session_state=SessionStateStore(tmp_path / "ss"),
    )
    runtime = _mutable_runtime()
    cmd = DiffCommand()
    result = asyncio.run(cmd.execute("", runtime, ctx))
    assert "line1 changed" in result or "line1" in result


# ─── SlashCommand base contract ─────────────────────────────────


def test_slash_command_names_are_distinct():
    from slash_commands.accept_edits import AcceptEditsCommand
    from slash_commands.checkpoint import CheckpointCommand
    from slash_commands.diff import DiffCommand
    from slash_commands.plan import PlanOffCommand, PlanOnCommand
    from slash_commands.undo import UndoCommand

    names = {
        c.name
        for c in (
            PlanOnCommand(),
            PlanOffCommand(),
            AcceptEditsCommand(),
            CheckpointCommand(),
            UndoCommand(),
            DiffCommand(),
        )
    }
    assert len(names) == 6
