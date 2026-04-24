"""III.6 — settings-based hook configuration.

Tests cover:
- ``_parse_hooks_block`` round-trips both nested and flat-list YAML shapes.
- Malformed entries are skipped with a warning (never raised).
- ``make_shell_hook_handler`` honors the exit-code contract
  (0 → pass, 2 → block with stderr, other → pass) and the timeout fail-open.
- The env-var contract documented in
  ``opencomputer/hooks/shell_handlers.py``.
- ``_register_settings_hooks`` wires ``Config.hooks`` into the global
  hook engine so ``fire_blocking`` routes to the command wrappers.

Reference for the Claude Code side of the contract:
``sources/claude-code/plugins/plugin-dev/skills/hook-development/SKILL.md``.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from textwrap import dedent

import pytest

from opencomputer.agent.config import Config, HookCommandConfig
from opencomputer.agent.config_store import (
    _parse_hooks_block,
    load_config,
)
from opencomputer.hooks.engine import HookEngine
from opencomputer.hooks.shell_handlers import make_shell_hook_handler
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent, HookSpec

# ─── helpers ───────────────────────────────────────────────────────────


def _write_py_script(dst: Path, body: str) -> Path:
    """Write a Python script to ``dst`` and make it executable.

    Uses ``sys.executable`` as the shebang interpreter to stay portable
    across macOS / Linux CI images and to avoid depending on ``/bin/bash``
    being sh-compatible.
    """
    dst.write_text(f"#!{sys.executable}\n{dedent(body)}\n", encoding="utf-8")
    dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return dst


def _ctx(
    *,
    event: HookEvent = HookEvent.PRE_TOOL_USE,
    session_id: str = "sess-1",
    tool_name: str | None = "Write",
) -> HookContext:
    tool_call = (
        ToolCall(id="tc-1", name=tool_name, arguments={"path": "/tmp/x"})
        if tool_name is not None
        else None
    )
    return HookContext(event=event, session_id=session_id, tool_call=tool_call)


# ─── _parse_hooks_block ────────────────────────────────────────────────


def test_parse_hooks_nested_shape() -> None:
    block = {
        "PreToolUse": [
            {
                "matcher": "Edit|Write|MultiEdit",
                "command": "python3 /tmp/linter.py",
                "timeout_seconds": 15,
            }
        ],
        "Stop": [
            {"command": "bash /tmp/cleanup.sh"},
        ],
    }
    parsed = _parse_hooks_block(block)
    assert len(parsed) == 2
    pre = next(h for h in parsed if h.event == "PreToolUse")
    assert pre.matcher == "Edit|Write|MultiEdit"
    assert pre.command == "python3 /tmp/linter.py"
    assert pre.timeout_seconds == pytest.approx(15.0)
    stop = next(h for h in parsed if h.event == "Stop")
    assert stop.matcher is None
    assert stop.command == "bash /tmp/cleanup.sh"
    assert stop.timeout_seconds == pytest.approx(10.0)  # default


def test_parse_hooks_flat_list_shape() -> None:
    block = [
        {
            "event": "PreToolUse",
            "matcher": "Bash",
            "command": "python3 /tmp/check.py",
        },
        {
            "event": "SessionStart",
            "command": "python3 /tmp/setup.py",
            "timeout_seconds": 5,
        },
    ]
    parsed = _parse_hooks_block(block)
    assert len(parsed) == 2
    assert {h.event for h in parsed} == {"PreToolUse", "SessionStart"}
    by_event = {h.event: h for h in parsed}
    assert by_event["PreToolUse"].matcher == "Bash"
    assert by_event["SessionStart"].timeout_seconds == pytest.approx(5.0)


def test_parse_hooks_missing_block_returns_empty() -> None:
    assert _parse_hooks_block(None) == ()


def test_parse_hooks_malformed_entry_skipped(caplog: pytest.LogCaptureFixture) -> None:
    block = {
        "PreToolUse": [
            {"matcher": "Edit"},  # missing command — should be skipped
            {"command": "python3 /tmp/ok.py"},  # valid
        ]
    }
    with caplog.at_level("WARNING", logger="opencomputer.config"):
        parsed = _parse_hooks_block(block)
    assert len(parsed) == 1
    assert parsed[0].command == "python3 /tmp/ok.py"
    assert any("missing command" in rec.message.lower() for rec in caplog.records)


def test_parse_hooks_unknown_event_skipped(caplog: pytest.LogCaptureFixture) -> None:
    block = {
        "NotARealEvent": [{"command": "python3 /tmp/x.py"}],
        "Stop": [{"command": "python3 /tmp/ok.py"}],
    }
    with caplog.at_level("WARNING", logger="opencomputer.config"):
        parsed = _parse_hooks_block(block)
    assert len(parsed) == 1
    assert parsed[0].event == "Stop"
    assert any("unknown event" in rec.message.lower() for rec in caplog.records)


def test_parse_hooks_unknown_event_in_flat_list_skipped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Flat list entries with unknown event names also skip cleanly."""
    block = [
        {"event": "Bogus", "command": "python3 /tmp/x.py"},
        {"event": "PreToolUse", "command": "python3 /tmp/ok.py"},
    ]
    with caplog.at_level("WARNING", logger="opencomputer.config"):
        parsed = _parse_hooks_block(block)
    assert [h.event for h in parsed] == ["PreToolUse"]


def test_parse_hooks_unsupported_type_skipped(caplog: pytest.LogCaptureFixture) -> None:
    block = {
        "PreToolUse": [
            {"type": "prompt", "command": "hi", "prompt": "..."},  # unsupported
            {"type": "command", "command": "python3 /tmp/ok.py"},
        ]
    }
    with caplog.at_level("WARNING", logger="opencomputer.config"):
        parsed = _parse_hooks_block(block)
    assert len(parsed) == 1
    assert any("unsupported type" in rec.message.lower() for rec in caplog.records)


def test_load_config_round_trips_hooks_block(tmp_path: Path) -> None:
    """End-to-end: YAML file → load_config → Config.hooks populated."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        dedent(
            """
            model:
              provider: anthropic
            hooks:
              PreToolUse:
                - matcher: "Edit"
                  command: "python3 /tmp/x.py"
                  timeout_seconds: 7
            """
        ).strip(),
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert len(cfg.hooks) == 1
    h = cfg.hooks[0]
    assert h.event == "PreToolUse"
    assert h.matcher == "Edit"
    assert h.timeout_seconds == pytest.approx(7.0)


# ─── shell handler ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_handler_exit_0_is_pass(tmp_path: Path) -> None:
    script = _write_py_script(
        tmp_path / "ok.py",
        """
        import sys
        _ = sys.stdin.read()
        sys.exit(0)
        """,
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(event="PreToolUse", command=f"{sys.executable} {script}")
    )
    decision = await handler(_ctx())
    assert decision is not None
    assert decision.decision == "pass"


@pytest.mark.asyncio
async def test_shell_handler_exit_2_is_block_with_stderr(tmp_path: Path) -> None:
    script = _write_py_script(
        tmp_path / "block.py",
        """
        import sys
        _ = sys.stdin.read()
        print("write forbidden: /etc/passwd", file=sys.stderr)
        sys.exit(2)
        """,
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(event="PreToolUse", command=f"{sys.executable} {script}")
    )
    decision = await handler(_ctx())
    assert decision is not None
    assert decision.decision == "block"
    assert "write forbidden" in decision.reason


@pytest.mark.asyncio
async def test_shell_handler_exit_nonzero_is_pass(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    script = _write_py_script(
        tmp_path / "crash.py",
        """
        import sys
        _ = sys.stdin.read()
        print("oops", file=sys.stderr)
        sys.exit(99)
        """,
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(event="PreToolUse", command=f"{sys.executable} {script}")
    )
    with caplog.at_level("WARNING", logger="opencomputer.hooks.shell"):
        decision = await handler(_ctx())
    assert decision is not None
    assert decision.decision == "pass"
    assert any("rc=99" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_shell_handler_timeout_is_pass(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    script = _write_py_script(
        tmp_path / "slow.py",
        """
        import sys, time
        _ = sys.stdin.read()
        time.sleep(30)
        sys.exit(0)
        """,
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(
            event="PreToolUse",
            command=f"{sys.executable} {script}",
            timeout_seconds=0.3,
        )
    )
    with caplog.at_level("WARNING", logger="opencomputer.hooks.shell"):
        decision = await handler(_ctx())
    assert decision is not None
    assert decision.decision == "pass"
    assert any("exceeded timeout" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_shell_handler_env_vars_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subprocess must see OPENCOMPUTER_* + CLAUDE_PLUGIN_ROOT env vars."""
    # Pin OPENCOMPUTER_HOME so we can assert OPENCOMPUTER_PROFILE_HOME below.
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(profile_home))

    out_file = tmp_path / "env.txt"
    script = _write_py_script(
        tmp_path / "dump_env.py",
        f"""
        import os, sys, json
        payload = sys.stdin.read()
        dump = {{
            'OPENCOMPUTER_EVENT': os.environ.get('OPENCOMPUTER_EVENT', ''),
            'OPENCOMPUTER_TOOL_NAME': os.environ.get('OPENCOMPUTER_TOOL_NAME', ''),
            'OPENCOMPUTER_SESSION_ID': os.environ.get('OPENCOMPUTER_SESSION_ID', ''),
            'OPENCOMPUTER_PROFILE_HOME': os.environ.get('OPENCOMPUTER_PROFILE_HOME', ''),
            'CLAUDE_PLUGIN_ROOT': os.environ.get('CLAUDE_PLUGIN_ROOT', ''),
            'payload': payload,
        }}
        open({str(out_file)!r}, 'w', encoding='utf-8').write(json.dumps(dump))
        sys.exit(0)
        """,
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(event="PostToolUse", command=f"{sys.executable} {script}")
    )
    decision = await handler(
        _ctx(event=HookEvent.POST_TOOL_USE, session_id="abc-123", tool_name="Bash")
    )
    assert decision is not None and decision.decision == "pass"

    import json

    dump = json.loads(out_file.read_text(encoding="utf-8"))
    assert dump["OPENCOMPUTER_EVENT"] == "PostToolUse"
    assert dump["OPENCOMPUTER_TOOL_NAME"] == "Bash"
    assert dump["OPENCOMPUTER_SESSION_ID"] == "abc-123"
    assert dump["OPENCOMPUTER_PROFILE_HOME"] == str(profile_home)
    assert dump["CLAUDE_PLUGIN_ROOT"] == str(profile_home)
    # Stdin payload must include the event + session_id.
    payload = json.loads(dump["payload"])
    assert payload["hook_event_name"] == "PostToolUse"
    assert payload["session_id"] == "abc-123"
    assert payload["tool_name"] == "Bash"


@pytest.mark.asyncio
async def test_shell_handler_missing_executable_is_pass(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-existent command fails open rather than raising."""
    handler = make_shell_hook_handler(
        HookCommandConfig(
            event="PreToolUse",
            command="/this/does/not/exist --arg",
        )
    )
    with caplog.at_level("WARNING", logger="opencomputer.hooks.shell"):
        decision = await handler(_ctx())
    assert decision is not None
    assert decision.decision == "pass"


# ─── CLI wiring ────────────────────────────────────────────────────────


def test_cli_registers_settings_hooks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_register_settings_hooks`` must push each ``HookCommandConfig``
    onto the global hook engine such that ``fire_blocking`` routes to
    the shell-command wrappers for the declared event."""
    from opencomputer import cli as cli_module
    from opencomputer.hooks import engine as engine_module

    # Use a fresh engine so the test doesn't pollute process state.
    fresh = HookEngine()
    monkeypatch.setattr(engine_module, "engine", fresh)
    monkeypatch.setattr(cli_module, "hook_engine", fresh)

    cfg = Config(
        hooks=(
            HookCommandConfig(
                event="PreToolUse",
                command="python3 /tmp/a.py",
                matcher="Edit",
            ),
            HookCommandConfig(event="Stop", command="python3 /tmp/b.py"),
        )
    )
    n = cli_module._register_settings_hooks(cfg)
    assert n == 2

    # Internal peek — the engine should carry exactly two registrations.
    pre = fresh._hooks.get(HookEvent.PRE_TOOL_USE, [])
    stop = fresh._hooks.get(HookEvent.STOP, [])
    assert len(pre) == 1
    assert len(stop) == 1
    assert pre[0].matcher == "Edit"
    assert stop[0].matcher is None


def test_cli_skips_unknown_event_names(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An unknown event name (that somehow survived config parsing, e.g.
    via ``Config(...)`` built in code) must still fail-open with a
    WARNING rather than blowing up at CLI startup."""
    from opencomputer import cli as cli_module
    from opencomputer.hooks import engine as engine_module

    fresh = HookEngine()
    monkeypatch.setattr(engine_module, "engine", fresh)
    monkeypatch.setattr(cli_module, "hook_engine", fresh)

    cfg = Config(
        hooks=(
            HookCommandConfig(event="Nonsense", command="python3 /tmp/x.py"),
            HookCommandConfig(event="Stop", command="python3 /tmp/y.py"),
        )
    )
    with caplog.at_level("WARNING", logger="opencomputer.cli"):
        n = cli_module._register_settings_hooks(cfg)
    assert n == 1
    assert any("unknown event" in rec.message.lower() for rec in caplog.records)


def test_cli_empty_hooks_returns_zero() -> None:
    """No declared hooks = no registration = 0 returned (and the banner
    skips the extra print)."""
    from opencomputer import cli as cli_module

    assert cli_module._register_settings_hooks(Config()) == 0


@pytest.mark.asyncio
async def test_registered_hook_blocks_via_exit_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: Config.hooks → ``_register_settings_hooks`` →
    ``fire_blocking`` returns a block decision when the command exits 2."""
    from opencomputer import cli as cli_module
    from opencomputer.hooks import engine as engine_module

    fresh = HookEngine()
    monkeypatch.setattr(engine_module, "engine", fresh)
    monkeypatch.setattr(cli_module, "hook_engine", fresh)

    script = _write_py_script(
        tmp_path / "deny.py",
        """
        import sys
        _ = sys.stdin.read()
        print("no-go", file=sys.stderr)
        sys.exit(2)
        """,
    )

    cfg = Config(
        hooks=(
            HookCommandConfig(
                event="PreToolUse",
                matcher="Write",
                command=f"{sys.executable} {script}",
            ),
        )
    )
    n = cli_module._register_settings_hooks(cfg)
    assert n == 1

    ctx = _ctx()
    decision = await fresh.fire_blocking(ctx)
    assert decision is not None
    assert decision.decision == "block"
    assert "no-go" in decision.reason


@pytest.mark.asyncio
async def test_registered_hook_matcher_filters_tool_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The matcher must gate the handler — Bash calls bypass a Write-only
    hook cleanly (no subprocess spawned)."""
    from opencomputer import cli as cli_module
    from opencomputer.hooks import engine as engine_module

    fresh = HookEngine()
    monkeypatch.setattr(engine_module, "engine", fresh)
    monkeypatch.setattr(cli_module, "hook_engine", fresh)

    # Poison canary — if the handler fires for Bash, this exit-2 script
    # would make fire_blocking return "block". Matcher must prevent it.
    script = _write_py_script(
        tmp_path / "canary.py",
        """
        import sys
        sys.exit(2)
        """,
    )
    cfg = Config(
        hooks=(
            HookCommandConfig(
                event="PreToolUse",
                matcher="Write",
                command=f"{sys.executable} {script}",
            ),
        )
    )
    cli_module._register_settings_hooks(cfg)

    bash_ctx = _ctx(tool_name="Bash")
    decision = await fresh.fire_blocking(bash_ctx)
    assert decision is None  # matcher skipped, no block
