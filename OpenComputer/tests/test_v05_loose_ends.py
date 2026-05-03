"""v0.5 loose ends: selective restore CLI/slash wiring + tool-risk slash."""
from __future__ import annotations

from opencomputer.agent.slash_commands import (
    register_builtin_slash_commands,
)
from opencomputer.plugins.registry import registry as _registry


def test_policy_tool_risk_slash_command_registered():
    register_builtin_slash_commands()
    assert "policy-tool-risk" in _registry.slash_commands


def _make_ctx(**overrides):
    from io import StringIO

    from rich.console import Console

    from opencomputer.cli_ui.slash_handlers import SlashContext

    base = dict(
        console=Console(file=StringIO()),
        session_id="s",
        config=object(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=lambda: [],
    )
    base.update(overrides)
    return SlashContext(**base)


def test_snapshot_restore_handler_signature_supports_filters():
    """The SlashContext callback signature accepts (sid, only, skip)."""
    ctx = _make_ctx()
    n = ctx.on_snapshot_restore("dummy", None, None)
    assert n == 0


def test_snapshot_list_files_handler_default_returns_empty():
    ctx = _make_ctx()
    files = ctx.on_snapshot_list_files("dummy")
    assert files == []


def test_slash_dispatcher_parses_only_and_skip_flags(tmp_path):
    """The /snapshot restore dispatcher parses --only / --skip flags
    and forwards them to on_snapshot_restore."""
    from io import StringIO

    from rich.console import Console

    from opencomputer.cli_ui.slash_handlers import (
        SlashContext,
        _handle_snapshot,
    )

    captured: dict = {}

    def fake_restore(sid, only, skip):
        captured["sid"] = sid
        captured["only"] = only
        captured["skip"] = skip
        return 1

    ctx = SlashContext(
        console=Console(file=StringIO()),
        session_id="s",
        config=object(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=lambda: [],
        on_snapshot_restore=fake_restore,
    )
    _handle_snapshot(
        ctx,
        ["restore", "snap_abc", "--only", "config.yaml,sessions.db"],
    )
    assert captured["sid"] == "snap_abc"
    assert captured["only"] == ["config.yaml", "sessions.db"]
    assert captured["skip"] is None


def test_slash_dispatcher_parses_skip_flag(tmp_path):
    from io import StringIO

    from rich.console import Console

    from opencomputer.cli_ui.slash_handlers import (
        SlashContext,
        _handle_snapshot,
    )

    captured: dict = {}

    def fake_restore(sid, only, skip):
        captured["sid"] = sid
        captured["only"] = only
        captured["skip"] = skip
        return 1

    ctx = SlashContext(
        console=Console(file=StringIO()),
        session_id="s",
        config=object(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {},
        get_session_list=lambda: [],
        on_snapshot_restore=fake_restore,
    )
    _handle_snapshot(
        ctx, ["restore", "snap_abc", "--skip", "config.yaml"],
    )
    assert captured["only"] is None
    assert captured["skip"] == ["config.yaml"]


def test_slash_dispatcher_list_files_command(tmp_path):
    from opencomputer.cli_ui.slash_handlers import _handle_snapshot

    files_called_with: list[str] = []

    def fake_list_files(sid):
        files_called_with.append(sid)
        return ["config.yaml", "sessions.db"]

    ctx = _make_ctx(on_snapshot_list_files=fake_list_files)
    _handle_snapshot(ctx, ["list-files", "snap_xyz"])
    assert files_called_with == ["snap_xyz"]
