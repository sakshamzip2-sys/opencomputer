"""Phase 6f tests: slash commands.

Each command is tested directly by constructing it + calling execute() on it.
Phase 12b6 Task D8 formalized the SlashCommand contract in plugin_sdk, so
the harness's 6 commands now subclass ``plugin_sdk.SlashCommand`` and capture
the ``HarnessContext`` in ``__init__``; ``execute(args, runtime)`` returns
``SlashCommandResult``. These unit tests cover the harness-side logic
independent of the dispatcher (which has its own test file).
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
    cmd = PlanOnCommand(harness_ctx=ctx)

    result = asyncio.run(cmd.execute("", runtime))
    assert runtime.custom["plan_mode"] is True
    assert "Plan mode enabled" in result.output
    assert ctx.session_state.get("mode:plan") is True


def test_plan_off_clears_custom_flag(tmp_path):
    from slash_commands.plan import PlanOffCommand

    runtime = _mutable_runtime()
    runtime.custom["plan_mode"] = True
    ctx = _ctx(tmp_path)
    cmd = PlanOffCommand(harness_ctx=ctx)

    result = asyncio.run(cmd.execute("", runtime))
    assert runtime.custom["plan_mode"] is False
    assert "Plan mode disabled" in result.output


# ─── /accept-edits ──────────────────────────────────────────────


def test_accept_edits_toggles(tmp_path):
    from slash_commands.accept_edits import AcceptEditsCommand

    runtime = _mutable_runtime()
    ctx = _ctx(tmp_path)
    cmd = AcceptEditsCommand(harness_ctx=ctx)

    r1 = asyncio.run(cmd.execute("", runtime))
    assert runtime.custom["accept_edits"] is True
    assert "on" in r1.output.lower()

    r2 = asyncio.run(cmd.execute("", runtime))
    assert runtime.custom["accept_edits"] is False
    assert "off" in r2.output.lower()


def test_accept_edits_explicit_on_off(tmp_path):
    from slash_commands.accept_edits import AcceptEditsCommand

    runtime = _mutable_runtime()
    ctx = _ctx(tmp_path)
    cmd = AcceptEditsCommand(harness_ctx=ctx)

    asyncio.run(cmd.execute("off", runtime))
    assert runtime.custom["accept_edits"] is False
    asyncio.run(cmd.execute("on", runtime))
    assert runtime.custom["accept_edits"] is True


# ─── /checkpoint ────────────────────────────────────────────────


def test_checkpoint_no_edited_files(tmp_path):
    from slash_commands.checkpoint import CheckpointCommand

    ctx = _ctx(tmp_path)
    runtime = _mutable_runtime()
    cmd = CheckpointCommand(harness_ctx=ctx)
    result = asyncio.run(cmd.execute("", runtime))
    assert "No edited files" in result.output


def test_checkpoint_saves_tracked_files(tmp_path, monkeypatch):
    from slash_commands.checkpoint import CheckpointCommand

    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_bytes(b"print('a')")
    (tmp_path / "b.py").write_bytes(b"print('b')")

    ctx = _ctx(tmp_path)
    ctx.session_state.set("edited_files", ["a.py", "b.py"])
    runtime = _mutable_runtime()
    cmd = CheckpointCommand(harness_ctx=ctx)
    result = asyncio.run(cmd.execute("before-refactor", runtime))
    assert "Checkpoint saved" in result.output
    assert "before-refactor" in result.output
    assert len(ctx.rewind_store.list()) == 1


# ─── /undo ──────────────────────────────────────────────────────


def test_undo_no_checkpoints(tmp_path):
    from slash_commands.undo import UndoCommand

    ctx = _ctx(tmp_path)
    runtime = _mutable_runtime()
    cmd = UndoCommand(harness_ctx=ctx)
    result = asyncio.run(cmd.execute("", runtime))
    assert "nothing to undo" in result.output.lower()


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
    cmd = UndoCommand(harness_ctx=ctx)
    result = asyncio.run(cmd.execute("", runtime))
    assert "Rewound 1" in result.output
    assert (workspace / "f.py").read_bytes() == b"v1"


def test_undo_rejects_bad_integer(tmp_path):
    from slash_commands.undo import UndoCommand

    ctx = _ctx(tmp_path)
    runtime = _mutable_runtime()
    cmd = UndoCommand(harness_ctx=ctx)
    result = asyncio.run(cmd.execute("nope", runtime))
    assert "bad argument" in result.output.lower() or "nope" in result.output.lower()


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
    cmd = DiffCommand(harness_ctx=ctx)
    result = asyncio.run(cmd.execute("", runtime))
    assert "line1 changed" in result.output or "line1" in result.output


# ─── SlashCommand base contract ─────────────────────────────────


def test_slash_command_names_are_distinct():
    from slash_commands.accept_edits import AcceptEditsCommand
    from slash_commands.checkpoint import CheckpointCommand
    from slash_commands.diff import DiffCommand
    from slash_commands.plan import PlanOffCommand, PlanOnCommand
    from slash_commands.undo import UndoCommand

    # Name is a class attribute — no need to construct fully.
    names = {
        PlanOnCommand.name,
        PlanOffCommand.name,
        AcceptEditsCommand.name,
        CheckpointCommand.name,
        UndoCommand.name,
        DiffCommand.name,
    }
    assert len(names) == 6


# ─── Skills registry + auto-activation ──────────────────────────


def test_skill_registry_discovers_bundled_skills():
    from skills.registry import discover

    entries = discover(PLUGIN_ROOT / "skills")
    ids = {e.id for e in entries}
    assert {"code-reviewer", "test-runner", "refactorer"} <= ids
    # Order is alphabetical (prompt-cache stability).
    ordered_ids = [e.id for e in entries]
    assert ordered_ids == sorted(ordered_ids)


def test_skill_registry_parses_frontmatter():
    from skills.registry import discover

    entries = discover(PLUGIN_ROOT / "skills")
    by_id = {e.id: e for e in entries}
    reviewer = by_id["code-reviewer"]
    assert reviewer.name == "Code reviewer"
    assert "review" in reviewer.description.lower()
    assert reviewer.version


def test_skill_match_strong_overlap():
    from pathlib import Path

    from skills.registry import SkillEntry, match_skill

    entries = [
        SkillEntry(
            id="code-reviewer",
            name="Code reviewer",
            description="review a pull request review a diff code review",
            version="0.1.0",
            path=Path("/tmp/x"),
        ),
        SkillEntry(
            id="test-runner",
            name="Test runner",
            description="run the tests run pytest run the test suite",
            version="0.1.0",
            path=Path("/tmp/y"),
        ),
    ]
    # "review" + "diff" — two overlapping tokens with code-reviewer desc.
    m = match_skill("please review the diff for this PR", entries)
    assert m is not None and m.id == "code-reviewer"

    # "run" + "tests" + "pytest" — strong overlap with test-runner desc.
    m2 = match_skill("please run the tests using pytest", entries)
    assert m2 is not None and m2.id == "test-runner"


def test_skill_match_no_overlap_returns_none():
    from pathlib import Path

    from skills.registry import SkillEntry, match_skill

    entries = [
        SkillEntry(
            id="x",
            name="x",
            description="review diff pull request",
            version="0",
            path=Path("/t"),
        ),
    ]
    m = match_skill("what is the weather today", entries)
    assert m is None


def test_skill_activation_provider_injects_on_match(monkeypatch):
    from skills.activation import SkillActivationInjectionProvider

    from plugin_sdk.injection import InjectionContext
    from plugin_sdk.runtime_context import RuntimeContext  # noqa: F401

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    provider = SkillActivationInjectionProvider(
        skills_dir=PLUGIN_ROOT / "skills"
    )
    ctx = InjectionContext(
        messages=(
            _Msg(
                "user",
                "please review the diff and code review for bugs before merging",
            ),
        ),
        runtime=_mutable_runtime(),
    )
    out = asyncio.run(provider.collect(ctx))
    assert out is not None
    assert "Activated skill" in out
    assert "Code reviewer" in out or "reviewer" in out.lower()


def test_skill_activation_provider_no_match_returns_none():
    from skills.activation import SkillActivationInjectionProvider

    from plugin_sdk.injection import InjectionContext

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    provider = SkillActivationInjectionProvider(
        skills_dir=PLUGIN_ROOT / "skills"
    )
    ctx = InjectionContext(
        messages=(_Msg("user", "what is the weather today"),),
        runtime=_mutable_runtime(),
    )
    assert asyncio.run(provider.collect(ctx)) is None
