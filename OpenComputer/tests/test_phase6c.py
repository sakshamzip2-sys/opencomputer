"""Phase 6c tests: Rewind foundation.

Covers: Checkpoint, RewindStore, SessionStateStore, HarnessContext,
RewindTool, and the auto_checkpoint PreToolUse hook.

Plugin files live under `extensions/coding-harness/` (with a hyphen, so it
cannot be imported as a Python package). We use a `sys.path` shim identical
to what the plugin loader does at runtime: insert the plugin root, then
import submodule paths directly.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import time
from pathlib import Path

import pytest

from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent


REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "extensions" / "coding-harness"


@pytest.fixture(autouse=True)
def _plugin_on_syspath():
    """Put coding-harness on sys.path so submodules (rewind.store etc.) import."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    # Purge any cached modules whose names collide with harness internals.
    for mod_name in list(sys.modules):
        if mod_name.split(".")[0] in {
            "context",
            "rewind",
            "state",
            "tools",
            "hooks",
            "plan_mode",
        }:
            sys.modules.pop(mod_name, None)
    yield
    if str(PLUGIN_ROOT) in sys.path:
        sys.path.remove(str(PLUGIN_ROOT))


# ─── Checkpoint ─────────────────────────────────────────────────


def test_checkpoint_content_hash_is_stable() -> None:
    from rewind.checkpoint import Checkpoint

    files = {"a.py": b"print(1)\n", "b.py": b"print(2)\n"}
    c1 = Checkpoint.from_files(files, label="t")
    c2 = Checkpoint.from_files(files, label="t")
    assert c1.id == c2.id

    c3 = Checkpoint.from_files({"a.py": b"print(3)\n"}, label="t")
    assert c3.id != c1.id
    assert c1.label == "t"
    assert "a.py" in c1.files


# ─── RewindStore ────────────────────────────────────────────────


def test_rewind_store_save_and_load(tmp_path: Path) -> None:
    from rewind.checkpoint import Checkpoint
    from rewind.store import RewindStore

    store = RewindStore(tmp_path / "rw")
    cp = Checkpoint.from_files({"hello.py": b"print('hi')\n"}, label="t1")
    store.save(cp)
    back = store.load(cp.id)
    assert back is not None
    assert back.files["hello.py"] == b"print('hi')\n"
    assert back.label == "t1"


def test_rewind_store_lists_newest_first(tmp_path: Path) -> None:
    from rewind.checkpoint import Checkpoint
    from rewind.store import RewindStore

    store = RewindStore(tmp_path / "rw")
    a = Checkpoint.from_files({"x": b"1"}, label="a")
    store.save(a)
    time.sleep(0.01)
    b = Checkpoint.from_files({"x": b"2"}, label="b")
    store.save(b)

    listing = store.list()
    assert [cp.id for cp in listing] == [b.id, a.id]


def test_rewind_store_restore_writes_files(tmp_path: Path) -> None:
    from rewind.checkpoint import Checkpoint
    from rewind.store import RewindStore

    workspace = tmp_path / "proj"
    workspace.mkdir()
    store = RewindStore(tmp_path / "rw", workspace_root=workspace)

    (workspace / "a.py").write_bytes(b"old")
    cp = Checkpoint.from_files({"a.py": b"restored"}, label="t")
    store.save(cp)
    store.restore(cp.id)

    assert (workspace / "a.py").read_bytes() == b"restored"


def test_rewind_store_restore_missing_raises(tmp_path: Path) -> None:
    from rewind.store import RewindStore

    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    with pytest.raises(KeyError):
        store.restore("deadbeef")


def test_rewind_store_shielded_save(tmp_path: Path) -> None:
    from rewind.checkpoint import Checkpoint
    from rewind.store import RewindStore

    store = RewindStore(tmp_path / "rw")
    cp = Checkpoint.from_files({"x.py": b"v"}, label="s")
    asyncio.run(store.save_shielded(cp))
    assert store.load(cp.id) is not None


def test_rewind_store_subagent_isolation(tmp_path: Path) -> None:
    from rewind.checkpoint import Checkpoint
    from rewind.store import RewindStore

    parent = RewindStore(tmp_path / "rw")
    cp_parent = Checkpoint.from_files({"p.py": b"parent"}, label="p")
    parent.save(cp_parent)

    sub = RewindStore(tmp_path / "rw", subagent_id="sub1")
    cp_sub = Checkpoint.from_files({"s.py": b"sub"}, label="s")
    sub.save(cp_sub)

    parent_ids = {c.id for c in parent.list()}
    sub_ids = {c.id for c in sub.list()}
    assert cp_parent.id in parent_ids
    assert cp_sub.id in sub_ids
    # Isolation: parent sees none of subagent's checkpoints.
    assert cp_sub.id not in parent_ids
    assert cp_parent.id not in sub_ids


# ─── SessionStateStore ──────────────────────────────────────────


def test_session_state_round_trip(tmp_path: Path) -> None:
    from state.store import SessionStateStore

    s = SessionStateStore(tmp_path)
    s.set("edited_files", ["a.py", "b.py"])
    assert s.get("edited_files") == ["a.py", "b.py"]
    assert s.get("missing", default=42) == 42


def test_session_state_mark_once(tmp_path: Path) -> None:
    from state.store import SessionStateStore

    s = SessionStateStore(tmp_path)
    assert s.mark_once("rule:no-semicolons") is True
    assert s.mark_once("rule:no-semicolons") is False
    assert s.is_marked("rule:no-semicolons") is True
    assert s.is_marked("rule:other") is False


# ─── HarnessContext ─────────────────────────────────────────────


def test_harness_context_progress_fanout(tmp_path: Path) -> None:
    from context import HarnessContext
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    events: list[dict] = []
    ctx = HarnessContext(
        session_id="s",
        rewind_store=RewindStore(tmp_path / "rw"),
        session_state=SessionStateStore(tmp_path / "ss"),
        emit_progress_fn=events.append,
    )
    ctx.emit_progress({"pct": 10, "msg": "a"})
    ctx.emit_progress({"pct": 50, "msg": "b"})
    assert len(events) == 2 and events[0]["pct"] == 10


def test_harness_context_progress_noop_without_fn(tmp_path: Path) -> None:
    from context import HarnessContext
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    ctx = HarnessContext(
        session_id="s",
        rewind_store=RewindStore(tmp_path / "rw"),
        session_state=SessionStateStore(tmp_path / "ss"),
    )
    # Must not raise.
    ctx.emit_progress({"pct": 50})


# ─── RewindTool ─────────────────────────────────────────────────


def test_rewind_tool_restores_last_checkpoint(tmp_path: Path) -> None:
    from context import HarnessContext
    from rewind.checkpoint import Checkpoint
    from rewind.store import RewindStore
    from state.store import SessionStateStore
    from tools.rewind import RewindTool

    workspace = tmp_path / "w"
    workspace.mkdir()
    (workspace / "f.py").write_bytes(b"v1")

    store = RewindStore(tmp_path / "rw", workspace_root=workspace)
    store.save(Checkpoint.from_files({"f.py": b"v1"}, label="before v2"))

    (workspace / "f.py").write_bytes(b"v2")
    ctx = HarnessContext(
        session_id="s",
        rewind_store=store,
        session_state=SessionStateStore(tmp_path / "ss"),
    )
    tool = RewindTool(ctx=ctx)
    result = asyncio.run(
        tool.execute(ToolCall(id="r1", name="Rewind", arguments={"steps": 1}))
    )
    assert not result.is_error
    assert "restored" in result.content.lower()
    assert (workspace / "f.py").read_bytes() == b"v1"


def test_rewind_tool_handles_no_checkpoints(tmp_path: Path) -> None:
    from context import HarnessContext
    from rewind.store import RewindStore
    from state.store import SessionStateStore
    from tools.rewind import RewindTool

    ctx = HarnessContext(
        session_id="s",
        rewind_store=RewindStore(tmp_path / "rw"),
        session_state=SessionStateStore(tmp_path / "ss"),
    )
    tool = RewindTool(ctx=ctx)
    result = asyncio.run(
        tool.execute(ToolCall(id="r1", name="Rewind", arguments={}))
    )
    assert result.is_error
    assert "no checkpoints" in result.content.lower()


def test_rewind_tool_list_mode(tmp_path: Path) -> None:
    from context import HarnessContext
    from rewind.checkpoint import Checkpoint
    from rewind.store import RewindStore
    from state.store import SessionStateStore
    from tools.rewind import RewindTool

    store = RewindStore(tmp_path / "rw", workspace_root=tmp_path)
    store.save(Checkpoint.from_files({"a.py": b"1"}, label="first"))
    store.save(Checkpoint.from_files({"a.py": b"2"}, label="second"))
    ctx = HarnessContext(
        session_id="s",
        rewind_store=store,
        session_state=SessionStateStore(tmp_path / "ss"),
    )
    tool = RewindTool(ctx=ctx)
    result = asyncio.run(
        tool.execute(
            ToolCall(id="r", name="Rewind", arguments={"list_checkpoints": True})
        )
    )
    assert not result.is_error
    assert "first" in result.content and "second" in result.content


# ─── auto_checkpoint hook ───────────────────────────────────────


def test_auto_checkpoint_snapshots_before_destructive_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from context import HarnessContext
    from hooks.auto_checkpoint import build_auto_checkpoint_hook_spec
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    workspace = tmp_path / "w"
    workspace.mkdir()
    (workspace / "x.py").write_bytes(b"before")
    monkeypatch.chdir(workspace)

    store = RewindStore(tmp_path / "rw", workspace_root=workspace)
    state = SessionStateStore(tmp_path / "ss")
    ctx = HarnessContext(
        session_id="s", rewind_store=store, session_state=state
    )

    spec = build_auto_checkpoint_hook_spec(harness_ctx=ctx)
    tc = ToolCall(id="1", name="Edit", arguments={"path": "x.py"})
    hctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=tc,
        runtime=None,
    )

    decision = asyncio.run(spec.handler(hctx))
    assert decision is None  # auto_checkpoint never blocks

    listing = store.list()
    assert len(listing) == 1
    assert listing[0].files["x.py"] == b"before"
    # The edited_files list was also updated.
    assert "x.py" in state.get("edited_files", [])


def test_auto_checkpoint_noop_on_non_destructive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from context import HarnessContext
    from hooks.auto_checkpoint import build_auto_checkpoint_hook_spec
    from rewind.store import RewindStore
    from state.store import SessionStateStore

    workspace = tmp_path / "w"
    workspace.mkdir()
    (workspace / "x.py").write_bytes(b"before")
    monkeypatch.chdir(workspace)

    store = RewindStore(tmp_path / "rw", workspace_root=workspace)
    state = SessionStateStore(tmp_path / "ss")
    ctx = HarnessContext(
        session_id="s", rewind_store=store, session_state=state
    )
    spec = build_auto_checkpoint_hook_spec(harness_ctx=ctx)
    tc = ToolCall(id="1", name="Read", arguments={"path": "x.py"})
    hctx = HookContext(
        event=HookEvent.PRE_TOOL_USE,
        session_id="s",
        tool_call=tc,
        runtime=None,
    )

    decision = asyncio.run(spec.handler(hctx))
    assert decision is None
    assert store.list() == []  # no checkpoint for a Read


# ─── Plugin wiring ──────────────────────────────────────────────


def test_plugin_registers_rewind_tool_and_auto_checkpoint_hook(
    tmp_path: Path,
) -> None:
    """End-to-end: plugin.register(api) adds Rewind + second hook."""

    class _FakeAPI:
        session_id = "t"
        workspace_root = None

        def __init__(self):
            self.tools = []
            self.hooks = []
            self.injections = []

        def register_tool(self, t):
            self.tools.append(t)

        def register_hook(self, s):
            self.hooks.append(s)

        def register_injection_provider(self, p):
            self.injections.append(p)

    # Load plugin.py via importlib — mirror the plugin loader's synthetic-name
    # pattern so we don't pollute sys.modules across tests.
    spec = importlib.util.spec_from_file_location(
        "ch_test_plugin_v2", PLUGIN_ROOT / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ch_test_plugin_v2"] = mod
    spec.loader.exec_module(mod)

    api = _FakeAPI()
    mod.register(api)

    tool_names = {t.schema.name for t in api.tools}
    # Original 6 + Rewind = 7
    assert "Rewind" in tool_names
    assert len(api.tools) == 7
    # Plan-block + auto-checkpoint = 2 hooks
    assert len(api.hooks) == 2
    # Plan-mode injection provider still registered
    assert len(api.injections) == 1
