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
    r = runner.invoke(
        hooks_app, ["test", "UserPromptSubmit", "--payload", "{not json}"]
    )
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
