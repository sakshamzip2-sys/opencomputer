"""B2 — `oc hooks list/test/clear/revoke` CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.agent.hook_history import clear_history, record_fire
from opencomputer.cli_hooks import hooks_app

runner = CliRunner()


def setup_function() -> None:
    clear_history()


def test_list_returns_known_events() -> None:
    r = runner.invoke(hooks_app, ["list", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    events = {row["event"] for row in data}
    # plugin_sdk.hooks.HookEvent declares 17 events as of May 2026.
    # Assert we see the most-load-bearing ones; tolerate count variance.
    assert "UserPromptSubmit" in events
    assert "PreToolUse" in events
    assert "SessionStart" in events
    assert len(events) >= 9  # tolerant lower bound; 17 expected


def test_list_shows_recent_fire() -> None:
    record_fire("UserPromptSubmit", "plugin:foo", ok=True, summary="hello")
    r = runner.invoke(hooks_app, ["list", "--json"])
    assert r.exit_code == 0
    data = json.loads(r.output)
    rec = next(row for row in data if row["event"] == "UserPromptSubmit")
    assert rec["last_source"] == "plugin:foo"
    assert rec["last_result"] == "ok"


def test_list_shows_err_for_failed_fire() -> None:
    record_fire("PreToolUse", "plugin:bad", ok=False, summary="boom")
    r = runner.invoke(hooks_app, ["list", "--json"])
    data = json.loads(r.output)
    rec = next(row for row in data if row["event"] == "PreToolUse")
    assert rec["last_result"] == "err"


def test_clear_empties_history() -> None:
    record_fire("UserPromptSubmit", "p1", ok=True, summary="")
    r = runner.invoke(hooks_app, ["clear"])
    assert r.exit_code == 0
    r2 = runner.invoke(hooks_app, ["list", "--json"])
    data = json.loads(r2.output)
    rec = next(row for row in data if row["event"] == "UserPromptSubmit")
    assert rec["last_fired_utc"] is None


def test_test_dry_run_default() -> None:
    r = runner.invoke(
        hooks_app,
        ["test", "UserPromptSubmit", "--payload", json.dumps({"prompt": "hi"})],
    )
    assert r.exit_code == 0, r.output
    out = r.output.lower()
    assert "dry-run" in out or "would fire" in out


def test_test_invalid_payload_exits_nonzero() -> None:
    r = runner.invoke(hooks_app, ["test", "UserPromptSubmit", "--payload", "{not json}"])
    assert r.exit_code != 0
    assert "json" in r.output.lower()


def test_test_unknown_event_in_dry_run_does_not_crash() -> None:
    r = runner.invoke(hooks_app, ["test", "NoSuchEvent"])
    assert r.exit_code == 0, r.output


def test_revoke_writes_settings_local(tmp_path: Path, monkeypatch) -> None:
    settings_path = tmp_path / "settings.local.json"
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    r = runner.invoke(hooks_app, ["revoke", "plugin:badguy"])
    assert r.exit_code == 0, r.output
    data = json.loads(settings_path.read_text())
    assert "plugin:badguy" in data["disabled_hooks"]


def test_revoke_dedups(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    runner.invoke(hooks_app, ["revoke", "plugin:x"])
    runner.invoke(hooks_app, ["revoke", "plugin:x"])
    data = json.loads((tmp_path / "settings.local.json").read_text())
    assert data["disabled_hooks"].count("plugin:x") == 1


# ─── G1 tests — `oc hooks test --execute` (Hermes Doc-2 residuals) ─────


def test_hooks_test_execute_invokes_registered_handler(monkeypatch, tmp_path) -> None:
    """`oc hooks test PreToolUse --execute` actually fires registered handlers.

    Verifies the engine dispatch path is exercised, not just enumerated.
    Touches the global engine singleton — clears registrations before/after
    so we don't bleed state to neighbouring tests.
    """
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    from opencomputer.hooks.engine import engine
    from plugin_sdk.hooks import HookDecision, HookEvent, HookSpec

    captured: list[str] = []

    async def my_handler(ctx):
        captured.append(ctx.event.value)
        return HookDecision(decision="pass")

    engine.unregister_all()
    engine.register(HookSpec(event=HookEvent.PRE_TOOL_USE, handler=my_handler))
    try:
        r = runner.invoke(
            hooks_app,
            ["test", "PreToolUse", "--execute", "--for-tool", "Read"],
        )
        assert r.exit_code == 0, r.output
        assert "PreToolUse" in r.output
        assert captured == ["PreToolUse"]
    finally:
        engine.unregister_all()


def test_hooks_test_execute_no_handlers_returns_zero(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    from opencomputer.hooks.engine import engine

    engine.unregister_all()
    try:
        r = runner.invoke(hooks_app, ["test", "PostToolUse", "--execute"])
        assert r.exit_code == 0
        out = r.output.lower()
        assert "0 handlers" in out or "no handlers" in out
    finally:
        engine.unregister_all()


def test_hooks_test_execute_handler_raises_is_caught(monkeypatch, tmp_path) -> None:
    """Engine swallows handler exceptions; CLI should not crash."""
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    from opencomputer.hooks.engine import engine
    from plugin_sdk.hooks import HookEvent, HookSpec

    async def boom(ctx):
        raise RuntimeError("boom")

    engine.unregister_all()
    engine.register(HookSpec(event=HookEvent.PRE_TOOL_USE, handler=boom))
    try:
        r = runner.invoke(
            hooks_app,
            ["test", "PreToolUse", "--execute", "--for-tool", "Read"],
        )
        assert r.exit_code == 0, r.output
    finally:
        engine.unregister_all()


def test_hooks_test_execute_unknown_event_errors() -> None:
    r = runner.invoke(hooks_app, ["test", "BogusEventName", "--execute"])
    assert r.exit_code != 0
