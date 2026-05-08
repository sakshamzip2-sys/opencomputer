# Hermes Execcode-Hooks Residuals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close 5 verified residual gaps from the Hermes "Code Execution & Event Hooks" reference doc that PR #496 left out (debug surfaces + protocol-parity gaps that pass the makes-sense filter).

**Architecture:** Additive only. Five small, independent changes across `cli_hooks.py`, `shell_handlers.py`, `plugin_sdk/hooks.py`, `agent/loop.py`, `agent/config.py`, `tools/ptc.py`, and `tools/execute_code.py`. Test-first per task. Worktree-isolated to avoid colliding with two parallel sessions (config-v2, security-v2).

**Tech Stack:** Python 3.13, pytest, typer, dataclasses, asyncio.

**Spec:** `OpenComputer/docs/superpowers/specs/2026-05-08-hermes-execcode-hooks-residuals-design.md` (committed in `6186991d`).

**Worktree:** `/Users/saksham/Vscode/claude/.claude/worktrees/hermes-execcode-hooks-residuals-2026-05-08/` on branch `worktree-hermes-execcode-hooks-residuals-2026-05-08`.

**File map:**

| File | Change | Task |
|---|---|---|
| `OpenComputer/opencomputer/cli_hooks.py` | Modify `cmd_test` to support `--execute`; add new `cmd_doctor` | T1, T2 |
| `OpenComputer/tests/test_cli_hooks.py` | Add tests for `--execute` and `doctor` | T1, T2 |
| `OpenComputer/opencomputer/hooks/shell_handlers.py` | Add stdout JSON parsing path with precedence over exit code | T3, T4 |
| `OpenComputer/tests/test_settings_hooks.py` | Add tests for both JSON shapes + context injection | T3, T4 |
| `OpenComputer/plugin_sdk/hooks.py` | Add `inject_context: str \| None = None` field to `HookDecision` | T4 |
| `OpenComputer/opencomputer/agent/loop.py` | In `_fire_pre_llm_call`: collect `inject_context` from blocking decisions, append to user message | T4 |
| `OpenComputer/opencomputer/agent/config.py` | Add `CodeExecutionConfig` dataclass with `max_tool_calls` slot | T5 |
| `OpenComputer/opencomputer/tools/ptc.py` | Make `_MAX_RPC_CALLS` overridable via `run_ptc(max_tool_calls=...)` | T5 |
| `OpenComputer/opencomputer/tools/execute_code.py` | Read `max_tool_calls` from config and pass to `run_ptc` | T5 |
| `OpenComputer/tests/test_pr8_exec_trace_and_bus_hooks.py` (or new file) | Test override + default | T5 |
| `OpenComputer/CLAUDE.md` | Update III.6 with augmented stdout JSON contract | T6 |
| `OpenComputer/docs/refs/hermes-agent/2026-05-08-kanban-goals-execcode-hooks-parity.md` | Append §2.5 follow-up reference | T6 |

---

### Task 1: G1 — `oc hooks test --execute` actually fires

**Files:**
- Modify: `OpenComputer/opencomputer/cli_hooks.py:cmd_test` (lines ~106-152, replacing the `_console.print("[red]--execute is not yet implemented;[/red] use dry-run for now.")` branch)
- Test: `OpenComputer/tests/test_cli_hooks.py` (add new test cases)

- [ ] **Step 1: Read the current `cmd_test` to anchor the edit**

Run: `sed -n '100,160p' OpenComputer/opencomputer/cli_hooks.py`

Expected: see the `--execute is not yet implemented` branch at the end of the function. Confirm the function structure: argument parsing, payload JSON load, dry-run branch, the unimplemented `--execute` branch.

- [ ] **Step 2: Write the failing test for `--execute` dispatching to a registered handler**

Add to `OpenComputer/tests/test_cli_hooks.py`:

```python
import asyncio
import json
from typer.testing import CliRunner

from opencomputer.cli_hooks import hooks_app
from opencomputer.hooks.engine import engine
from plugin_sdk.hooks import HookDecision, HookEvent, HookSpec


def test_hooks_test_execute_invokes_registered_handler(monkeypatch, tmp_path):
    """`oc hooks test PreToolUse --execute` actually fires registered handlers.

    Verifies the engine dispatch path is exercised, not just enumerated.
    """
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    captured: list[str] = []

    async def my_handler(ctx):
        captured.append(ctx.event.value)
        return HookDecision(decision="pass")

    engine.unregister_all()
    engine.register(HookSpec(event=HookEvent.PRE_TOOL_USE, handler=my_handler))
    try:
        runner = CliRunner()
        result = runner.invoke(
            hooks_app,
            ["test", "PreToolUse", "--execute", "--for-tool", "Read"],
        )
        assert result.exit_code == 0, result.output
        assert "PreToolUse" in result.output
        assert captured == ["PreToolUse"]
    finally:
        engine.unregister_all()


def test_hooks_test_execute_no_handlers_returns_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    engine.unregister_all()
    runner = CliRunner()
    result = runner.invoke(
        hooks_app, ["test", "PostToolUse", "--execute"]
    )
    assert result.exit_code == 0
    assert "no handlers" in result.output.lower() or "0 handlers" in result.output.lower()


def test_hooks_test_execute_handler_raises_is_caught(monkeypatch, tmp_path):
    """Engine swallows handler exceptions; CLI should not crash."""
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))

    async def boom(ctx):
        raise RuntimeError("boom")

    engine.unregister_all()
    engine.register(HookSpec(event=HookEvent.PRE_TOOL_USE, handler=boom))
    try:
        runner = CliRunner()
        result = runner.invoke(
            hooks_app,
            ["test", "PreToolUse", "--execute", "--for-tool", "Read"],
        )
        assert result.exit_code == 0
    finally:
        engine.unregister_all()


def test_hooks_test_execute_unknown_event_errors():
    runner = CliRunner()
    result = runner.invoke(
        hooks_app, ["test", "BogusEventName", "--execute"]
    )
    assert result.exit_code != 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd OpenComputer && pytest tests/test_cli_hooks.py::test_hooks_test_execute_invokes_registered_handler tests/test_cli_hooks.py::test_hooks_test_execute_no_handlers_returns_zero tests/test_cli_hooks.py::test_hooks_test_execute_handler_raises_is_caught tests/test_cli_hooks.py::test_hooks_test_execute_unknown_event_errors -v`

Expected: all four FAIL — current `--execute` branch raises `typer.Exit(2)`.

- [ ] **Step 4: Implement `--execute` to fire via the engine**

Replace the `--execute is not yet implemented` branch at the bottom of `cmd_test` (around line 144-152). Also add a `--for-tool` option to populate `tool_call.name`.

Replace the function signature line:
```python
def cmd_test(
    event: str = typer.Argument(..., help="Hook event name (e.g. UserPromptSubmit)."),
    payload: str = typer.Option("{}", "--payload", help="JSON-encoded synthetic payload."),
    execute: bool = typer.Option(False, "--execute", help="Actually dispatch (default: dry-run)."),
) -> None:
```

with:
```python
def cmd_test(
    event: str = typer.Argument(..., help="Hook event name (e.g. UserPromptSubmit)."),
    payload: str = typer.Option("{}", "--payload", help="JSON-encoded synthetic payload."),
    for_tool: str = typer.Option(
        "", "--for-tool",
        help="Tool name for Pre/PostToolUse synthetic ctx.tool_call.name.",
    ),
    execute: bool = typer.Option(False, "--execute", help="Actually dispatch (default: dry-run)."),
) -> None:
```

Replace the trailing `--execute is not yet implemented` branch with the dispatch implementation. Insert before the final `raise typer.Exit(2)`:

```python
    # Real dispatch — invoke the engine.
    try:
        from opencomputer.hooks.engine import engine
        from plugin_sdk.hooks import HookContext, HookEvent
        from plugin_sdk.core import ToolCall

        try:
            event_enum = HookEvent(event)
        except ValueError:
            _console.print(
                f"[red]Unknown event {event!r}[/red]; "
                f"known events: {[e.value for e in HookEvent]}"
            )
            raise typer.Exit(1)

        # Synthesise a HookContext. We populate only what's safe to fake.
        # Tool-name-bearing events get a stub ToolCall when --for-tool is given.
        tool_call = None
        if for_tool:
            tool_call = ToolCall(
                id="oc-hooks-test-synthetic",
                name=for_tool,
                arguments=payload_obj if isinstance(payload_obj, dict) else {},
            )
        ctx = HookContext(
            event=event_enum,
            session_id=str(payload_obj.get("session_id", "oc-hooks-test")),
            tool_call=tool_call,
        )

        # Lower-cased decision name for output. fire_blocking() returns the first
        # non-pass decision; fire() is fire-and-forget. Surface both shapes.
        specs = engine._ordered_specs(event_enum)  # noqa: SLF001
        if not specs:
            _console.print(f"[dim]0 handlers registered for {event}[/dim]")
            return

        if event_enum in (
            HookEvent.PRE_TOOL_USE,
            HookEvent.PRE_LLM_CALL,
            HookEvent.PRE_GATEWAY_DISPATCH,
            HookEvent.PRE_APPROVAL_REQUEST,
        ):
            decision = asyncio.run(engine.fire_blocking(ctx))
            if decision is None:
                _console.print(
                    f"[green]{event}[/green]: {len(specs)} handler(s) ran, "
                    f"all returned pass"
                )
            else:
                _console.print(
                    f"[yellow]{event}[/yellow]: first non-pass decision = "
                    f"[bold]{decision.decision}[/bold] "
                    f"reason={decision.reason!r}"
                )
        else:
            asyncio.run(engine.fire(ctx))
            _console.print(
                f"[green]{event}[/green]: dispatched to {len(specs)} "
                f"fire-and-forget handler(s)"
            )
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 — surface to user
        _console.print(f"[red]CLI error during dispatch:[/red] {exc}")
        raise typer.Exit(2) from exc
```

Also add `import asyncio` at the top of the file if missing.

- [ ] **Step 5: Run all the new tests to verify they pass**

Run: `cd OpenComputer && pytest tests/test_cli_hooks.py -v`

Expected: all tests PASS, including any pre-existing ones still green.

- [ ] **Step 6: Run ruff on the touched file**

Run: `ruff check OpenComputer/opencomputer/cli_hooks.py OpenComputer/tests/test_cli_hooks.py`

Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/opencomputer/cli_hooks.py OpenComputer/tests/test_cli_hooks.py
git commit -m "feat(hooks): G1 — oc hooks test --execute fires synthetic events

Replaces the 'not yet implemented' stub with real dispatch via the engine.

- New --for-tool option populates ctx.tool_call.name for Pre/PostToolUse
- Blocking events (PRE_TOOL_USE, PRE_LLM_CALL, PRE_GATEWAY_DISPATCH,
  PRE_APPROVAL_REQUEST) use engine.fire_blocking and surface the first
  non-pass decision
- Fire-and-forget events use engine.fire and report dispatch count
- Unknown event names exit 1 with known-events list
- Handler exceptions are swallowed by the engine (existing behaviour);
  CLI exits 0 with the engine's own log entry as record

Closes the 'why didn't my hook fire' debug story.

4 new tests in test_cli_hooks.py.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: G2 — `oc hooks doctor` health diagnostics

**Files:**
- Modify: `OpenComputer/opencomputer/cli_hooks.py` — add `cmd_doctor` and helpers
- Test: `OpenComputer/tests/test_cli_hooks.py` — add doctor tests

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli_hooks.py`:

```python
import os
import stat
import textwrap


def test_doctor_reports_no_gateway_hooks_as_info(monkeypatch, tmp_path):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(hooks_app, ["doctor"])
    assert result.exit_code == 0
    assert "0 gateway file-discovery hooks" in result.output


def test_doctor_reports_valid_gateway_hook_as_ok(monkeypatch, tmp_path):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    hook_dir = tmp_path / "hooks" / "logger"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text(textwrap.dedent("""
        events:
          - gateway:startup
        description: log startups
    """).strip())
    (hook_dir / "handler.py").write_text(textwrap.dedent("""
        async def handle(event_type, context):
            return None
    """).strip())

    runner = CliRunner()
    result = runner.invoke(hooks_app, ["doctor"])
    assert result.exit_code == 0
    assert "logger" in result.output
    assert "OK" in result.output


def test_doctor_reports_broken_handler_as_error(monkeypatch, tmp_path):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    hook_dir = tmp_path / "hooks" / "broken"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events:\n  - gateway:startup\n")
    (hook_dir / "handler.py").write_text("def NOT_handle(): pass\n")

    runner = CliRunner()
    result = runner.invoke(hooks_app, ["doctor"])
    assert result.exit_code == 0
    assert "broken" in result.output
    assert "ERROR" in result.output or "error" in result.output.lower()


def test_doctor_json_mode_returns_parseable_json(monkeypatch, tmp_path):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(hooks_app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    # Always at least one row from the gateway-hooks summary line.
    assert any(row.get("check", "").startswith("gateway") for row in payload)


def test_doctor_reports_unknown_event_in_hookyaml_as_warn(monkeypatch, tmp_path):
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    hook_dir = tmp_path / "hooks" / "typo"
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text("events:\n  - gatway:startup\n")  # typo
    (hook_dir / "handler.py").write_text(
        "async def handle(event_type, context):\n    return None\n"
    )

    runner = CliRunner()
    result = runner.invoke(hooks_app, ["doctor"])
    assert result.exit_code == 0
    # Surface unknown event: WARN
    assert "WARN" in result.output or "warn" in result.output.lower()


def test_doctor_settings_hook_missing_executable_warn(monkeypatch, tmp_path):
    """A configured shell-hook command pointing at a non-existent path → WARN."""
    monkeypatch.setenv("OC_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent("""
        hooks:
          PreToolUse:
            - matcher: ".*"
              command: "/nonexistent/script.sh"
              timeout_seconds: 5
    """).strip())

    runner = CliRunner()
    result = runner.invoke(hooks_app, ["doctor"])
    assert result.exit_code == 0
    # Settings hook with bad path → mentioned in output
    assert "/nonexistent/script.sh" in result.output or "settings" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd OpenComputer && pytest tests/test_cli_hooks.py::test_doctor_reports_no_gateway_hooks_as_info tests/test_cli_hooks.py::test_doctor_reports_valid_gateway_hook_as_ok tests/test_cli_hooks.py::test_doctor_reports_broken_handler_as_error tests/test_cli_hooks.py::test_doctor_json_mode_returns_parseable_json tests/test_cli_hooks.py::test_doctor_reports_unknown_event_in_hookyaml_as_warn tests/test_cli_hooks.py::test_doctor_settings_hook_missing_executable_warn -v`

Expected: all six FAIL with "no command 'doctor'".

- [ ] **Step 3: Implement `cmd_doctor`**

Add to the bottom of `OpenComputer/opencomputer/cli_hooks.py`:

```python
@hooks_app.command("doctor")
def cmd_doctor(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Diagnostic health check: gateway hooks, settings hooks, recent activity.

    Surface health issues (broken HOOK.yaml, missing handle(), bad command
    paths) before they manifest as silent fail-open behaviour at runtime.
    """
    rows: list[dict[str, str]] = []

    # 1. Gateway file-discovery hooks
    try:
        from opencomputer.gateway.event_hooks import (
            KNOWN_EVENTS,
            discover_hooks,
            hooks_root,
        )

        root = hooks_root()
        if not root.exists():
            rows.append({
                "severity": "INFO",
                "check": "gateway-hooks-dir",
                "detail": f"{root} does not exist (0 gateway file-discovery hooks)",
            })
        else:
            hook_specs = discover_hooks(root)
            rows.append({
                "severity": "INFO",
                "check": "gateway-hooks-count",
                "detail": f"{len(hook_specs)} gateway file-discovery hook(s) at {root}",
            })
            for hk in hook_specs:
                # Validate event names against KNOWN_EVENTS prefixes
                unknown = [
                    e for e in hk.events
                    if not (
                        e in KNOWN_EVENTS
                        or any(e.startswith(known.rstrip("*")) for known in KNOWN_EVENTS if known.endswith(":*"))
                        or e.startswith("command:")
                    )
                ]
                if unknown:
                    rows.append({
                        "severity": "WARN",
                        "check": f"gateway-hook:{hk.name}",
                        "detail": f"unknown events: {unknown}",
                    })
                elif hk.handler is None:
                    rows.append({
                        "severity": "ERROR",
                        "check": f"gateway-hook:{hk.name}",
                        "detail": "handler.py missing or has no async def handle(...)",
                    })
                else:
                    rows.append({
                        "severity": "OK",
                        "check": f"gateway-hook:{hk.name}",
                        "detail": f"events={hk.events}",
                    })
    except Exception as exc:  # noqa: BLE001
        rows.append({
            "severity": "ERROR",
            "check": "gateway-hooks-discovery",
            "detail": f"discovery raised: {type(exc).__name__}: {exc}",
        })

    # 2. Settings hooks (config.yaml hooks: block)
    try:
        from opencomputer.agent.config import load_config

        cfg = load_config()
        sh = getattr(cfg, "hooks", None)
        if sh:
            for event_name, configs in sh.items():
                for cmd_config in configs or []:
                    cmd = getattr(cmd_config, "command", "")
                    # First token is the executable
                    parts = cmd.split()
                    exe = parts[0] if parts else ""
                    if exe.startswith("/") and not os.path.exists(exe):
                        rows.append({
                            "severity": "WARN",
                            "check": f"settings-hook:{event_name}",
                            "detail": f"executable not found: {exe}",
                        })
                    elif exe.startswith("/"):
                        st = os.stat(exe)
                        if not (st.st_mode & stat.S_IXUSR):
                            rows.append({
                                "severity": "WARN",
                                "check": f"settings-hook:{event_name}",
                                "detail": f"not user-executable: {exe}",
                            })
                        else:
                            rows.append({
                                "severity": "OK",
                                "check": f"settings-hook:{event_name}",
                                "detail": f"command={cmd[:80]}",
                            })
                    else:
                        rows.append({
                            "severity": "INFO",
                            "check": f"settings-hook:{event_name}",
                            "detail": f"PATH-resolved command: {cmd[:80]}",
                        })
        else:
            rows.append({
                "severity": "INFO",
                "check": "settings-hooks",
                "detail": "no hooks: block in config.yaml",
            })
    except Exception as exc:  # noqa: BLE001
        rows.append({
            "severity": "INFO",
            "check": "settings-hooks",
            "detail": f"config not loadable: {type(exc).__name__}",
        })

    # 3. Recent fire history — surface staleness
    try:
        from opencomputer.agent.hook_history import all_events, iter_history

        events_with_fires = list(all_events())
        if events_with_fires:
            for event_name in events_with_fires[:5]:
                records = list(iter_history(event_name))
                if records:
                    last = records[-1]
                    rows.append({
                        "severity": "OK" if last.ok else "WARN",
                        "check": f"recent-fire:{event_name}",
                        "detail": f"{last.ts_utc:.0f} src={last.source_id[:40]} ok={last.ok}",
                    })
        else:
            rows.append({
                "severity": "INFO",
                "check": "recent-fires",
                "detail": "no hook fires recorded yet",
            })
    except Exception:  # noqa: BLE001
        pass

    # 4. Note: OC has no shell-hook allowlist by design
    rows.append({
        "severity": "INFO",
        "check": "shell-hook-allowlist",
        "detail": (
            "OC has no allowlist (config.yaml-edit IS consent); "
            "OPENCOMPUTER_ACCEPT_HOOKS env var is a no-op"
        ),
    })

    if json_out:
        typer.echo(json.dumps(rows))
        return

    table = Table(title="Hooks doctor")
    table.add_column("Severity", style="cyan")
    table.add_column("Check")
    table.add_column("Detail")
    for row in rows:
        sev_style = {
            "OK": "green",
            "INFO": "dim",
            "WARN": "yellow",
            "ERROR": "red",
        }.get(row["severity"], "white")
        table.add_row(
            f"[{sev_style}]{row['severity']}[/{sev_style}]",
            row["check"],
            row["detail"][:120],
        )
    _console.print(table)
```

Add the missing imports at the top:

```python
import os
import stat
```

(`stat` may already be imported indirectly; if `ruff check` flags it, `import stat` is correct.)

- [ ] **Step 4: Run the new tests**

Run: `cd OpenComputer && pytest tests/test_cli_hooks.py -v -k doctor`

Expected: all six new tests PASS.

- [ ] **Step 5: Run ruff**

Run: `ruff check OpenComputer/opencomputer/cli_hooks.py OpenComputer/tests/test_cli_hooks.py`

Expected: clean. Fix any issues inline.

- [ ] **Step 6: Smoke check**

Run: `cd OpenComputer && python -m opencomputer.cli_hooks doctor 2>&1 | head -30` (or `python -c "from opencomputer.cli_hooks import hooks_app; from typer.testing import CliRunner; print(CliRunner().invoke(hooks_app, ['doctor']).output)"`)

Expected: prints a Rich table with at least the gateway-hooks-dir + shell-hook-allowlist rows.

- [ ] **Step 7: Commit**

```bash
git add OpenComputer/opencomputer/cli_hooks.py OpenComputer/tests/test_cli_hooks.py
git commit -m "feat(hooks): G2 — oc hooks doctor health diagnostics

New 'doctor' subcommand surfaces hook-system health one row per check:

- Gateway file-discovery hooks: count + per-hook validation (HOOK.yaml
  events recognised, handler.py defines async handle())
- Settings hooks: each command's executable resolution + executable bit
- Recent fire history (per-event last-fire timestamp + ok/error)
- Shell-hook allowlist note (OC has none — config.yaml-edit IS consent)

Severity buckets: OK / INFO / WARN / ERROR. Rich table by default;
--json flag returns flat list for programmatic consumption.

Discovery never raises out of doctor — broken HOOK.yaml or import
errors are surfaced as ERROR rows, not exceptions.

6 new tests covering: empty hooks dir, valid hook, broken handler.py,
JSON mode, unknown event in HOOK.yaml, missing executable in settings.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: G3 — Shell-hook stdout JSON wire protocol

**Files:**
- Modify: `OpenComputer/opencomputer/hooks/shell_handlers.py` — augment `_run` to parse stdout JSON before falling back to exit code
- Test: `OpenComputer/tests/test_settings_hooks.py` (or new `test_shell_hook_stdout_protocol.py`) — protocol tests

- [ ] **Step 1: Inspect existing shell-hook tests for the test pattern**

Run: `cd OpenComputer && grep -l "make_shell_hook_handler\|HookCommandConfig" tests/ | head -5`

Expected: at least one test file using `HookCommandConfig` + a small bash script payload. Use the same fixture pattern.

- [ ] **Step 2: Write failing tests**

Add to a new file `OpenComputer/tests/test_shell_hook_stdout_protocol.py`:

```python
"""G3 — shell-hook stdout JSON wire protocol (Hermes + Claude-Code shapes)."""

from __future__ import annotations

import asyncio
import os
import stat
import textwrap

import pytest

from opencomputer.agent.config import HookCommandConfig
from opencomputer.hooks.shell_handlers import make_shell_hook_handler
from plugin_sdk.core import ToolCall
from plugin_sdk.hooks import HookContext, HookEvent


def _write_script(tmp_path, body: str):
    p = tmp_path / "hook.sh"
    p.write_text("#!/usr/bin/env bash\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


def _ctx(event: HookEvent = HookEvent.PRE_TOOL_USE) -> HookContext:
    return HookContext(
        event=event,
        session_id="sess-test",
        tool_call=ToolCall(id="t-1", name="Read", arguments={"path": "/tmp/x"}),
    )


def test_stdout_hermes_block_shape_blocks_with_message(tmp_path):
    """`{"action":"block","message":"why"}` on stdout → block."""
    script = _write_script(tmp_path, 'cat - >/dev/null; printf \'%s\' \'{"action":"block","message":"hermes block"}\'')
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "block"
    assert decision.reason == "hermes block"


def test_stdout_claude_code_block_shape_blocks_with_reason(tmp_path):
    """`{"decision":"block","reason":"why"}` on stdout → block."""
    script = _write_script(tmp_path, 'cat - >/dev/null; printf \'%s\' \'{"decision":"block","reason":"cc block"}\'')
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "block"
    assert decision.reason == "cc block"


def test_stdout_approve_shape_passes(tmp_path):
    """`{"action":"approve"}` → pass."""
    script = _write_script(tmp_path, 'cat - >/dev/null; printf \'%s\' \'{"action":"approve"}\'')
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"


def test_stdout_empty_object_passes(tmp_path):
    """`{}` → pass (Hermes idiomatic no-op)."""
    script = _write_script(tmp_path, 'cat - >/dev/null; printf \'%s\' \'{}\'')
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"


def test_stdout_malformed_json_falls_back_to_exit_code(tmp_path):
    """Invalid JSON on stdout + exit 2 + stderr → exit-code path wins."""
    script = _write_script(
        tmp_path,
        'cat - >/dev/null; echo "this is not json" >&1; echo "blocked by exit code" >&2; exit 2',
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "block"
    assert "blocked by exit code" in decision.reason


def test_stdout_block_wins_over_clean_exit_zero(tmp_path):
    """stdout JSON `block` + exit 0 → block wins (precedence rule)."""
    script = _write_script(
        tmp_path,
        'cat - >/dev/null; printf \'%s\' \'{"action":"block","message":"json block"}\'; exit 0',
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "block"
    assert decision.reason == "json block"


def test_stdout_unrecognized_keys_pass(tmp_path):
    """JSON object with no recognised keys → pass."""
    script = _write_script(tmp_path, 'cat - >/dev/null; printf \'%s\' \'{"some_other_key":"value"}\'')
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx()))
    assert decision is not None
    assert decision.decision == "pass"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd OpenComputer && pytest tests/test_shell_hook_stdout_protocol.py -v`

Expected: tests with `block` shapes FAIL (current handler ignores stdout JSON entirely; only test_stdout_malformed_json passes coincidentally because exit 2 path still works).

- [ ] **Step 4: Augment `make_shell_hook_handler` to parse stdout JSON**

Edit `OpenComputer/opencomputer/hooks/shell_handlers.py`. Replace the section starting at `rc = proc.returncode` (line ~207) and ending at the function's final `return HookDecision(decision="pass")`.

Find this block:
```python
        rc = proc.returncode
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""

        if rc == 0:
            return HookDecision(decision="pass")
        if rc == 2:
            # Matches Claude Code's convention: exit 2 = blocking error
            # with stderr as the reason fed back to the model.
            reason = stderr_text or "blocked by settings hook"
            return HookDecision(decision="block", reason=reason)

        _log.warning(
            "settings hook: command %r exited with rc=%s (stderr=%r); passing",
            config.command,
            rc,
            stderr_text,
        )
        return HookDecision(decision="pass")
```

Replace with:
```python
        rc = proc.returncode
        stdout_text = (
            stdout_bytes.decode("utf-8", errors="replace").strip() if stdout_bytes else ""
        )
        stderr_text = (
            stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
        )

        # 2026-05-08 G3 — Hermes Doc-2 stdout JSON wire protocol.
        # If stdout parses as a JSON object, recognised keys take precedence
        # over the exit-code path. This lets a script return
        # {"action":"block","message":"..."} (Hermes canonical) or
        # {"decision":"block","reason":"..."} (Claude Code) with exit 0.
        if stdout_text:
            try:
                stdout_obj = json.loads(stdout_text)
            except json.JSONDecodeError:
                stdout_obj = None
            if isinstance(stdout_obj, dict):
                resolved = _decision_from_stdout(stdout_obj, ctx.event)
                if resolved is not None:
                    return resolved
                # Recognised JSON but no actionable shape → pass
                # (with debug trace for unrecognised keys).
                if not _stdout_has_known_keys(stdout_obj):
                    _log.debug(
                        "settings hook %r: stdout JSON had no recognised "
                        "keys (%s); passing",
                        config.command,
                        list(stdout_obj.keys()),
                    )
                return HookDecision(decision="pass")

        if rc == 0:
            return HookDecision(decision="pass")
        if rc == 2:
            reason = stderr_text or "blocked by settings hook"
            return HookDecision(decision="block", reason=reason)

        _log.warning(
            "settings hook: command %r exited with rc=%s (stderr=%r); passing",
            config.command,
            rc,
            stderr_text,
        )
        return HookDecision(decision="pass")
```

Add the two helper functions just below the module-level `_log` (above `_ctx_payload`):

```python
_RECOGNISED_STDOUT_KEYS: frozenset[str] = frozenset(
    {"action", "decision", "message", "reason", "context"}
)


def _stdout_has_known_keys(obj: dict[str, Any]) -> bool:
    """True if the parsed stdout JSON object has at least one recognised key."""
    return bool(_RECOGNISED_STDOUT_KEYS.intersection(obj.keys()))


def _decision_from_stdout(
    obj: dict[str, Any],
    event: "HookEvent",
) -> HookDecision | None:
    """Translate a parsed stdout JSON object into a :class:`HookDecision`.

    Returns ``None`` when the object's keys don't unambiguously map to a
    decision (caller falls back to the exit-code path or returns pass).
    Returns a ``HookDecision`` for recognised shapes:

    * Hermes canonical ``{"action": "block", "message": "..."}``
    * Claude Code ``{"decision": "block", "reason": "..."}``
    * Either ``{"action": "approve"|"allow"}`` / ``{"decision": "approve"}`` → pass
    * (G4 plugs into this — context injection from PRE_LLM_CALL only)
    """
    # G4 — context injection on PRE_LLM_CALL. Lives here because the
    # decision branch and the context branch may co-exist (a script that
    # injects context AND signals approve).
    inject = obj.get("context")
    inject_str = str(inject).strip() if isinstance(inject, str) and inject.strip() else None

    raw_action = obj.get("action")
    raw_decision = obj.get("decision")

    def _is_block(value: object) -> bool:
        return isinstance(value, str) and value.lower() == "block"

    def _is_approve(value: object) -> bool:
        return isinstance(value, str) and value.lower() in ("approve", "allow", "pass")

    if _is_block(raw_action) or _is_block(raw_decision):
        message = obj.get("message") or obj.get("reason") or "blocked by settings hook"
        return HookDecision(decision="block", reason=str(message))
    if _is_approve(raw_action) or _is_approve(raw_decision):
        # Pass with optional injected context.
        if inject_str and event == HookEvent.PRE_LLM_CALL:
            return HookDecision(decision="pass", inject_context=inject_str)
        return HookDecision(decision="pass")
    if inject_str:
        # Context-only response (no explicit action) — pass + maybe inject.
        if event == HookEvent.PRE_LLM_CALL:
            return HookDecision(decision="pass", inject_context=inject_str)
        return HookDecision(decision="pass")
    # No recognised key combination — caller decides
    return None
```

Add the import at the top of the file:
```python
from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookHandler
```

(`HookEvent` is the addition; the others are already imported.)

- [ ] **Step 5: Run G3 tests to verify they pass**

Run: `cd OpenComputer && pytest tests/test_shell_hook_stdout_protocol.py -v`

Expected: all seven tests PASS.

Note: the `inject_context=` kwarg in `_decision_from_stdout` won't compile yet because Task 4 adds the field to `HookDecision`. **For this Task 3 commit only**, replace `HookDecision(decision="pass", inject_context=inject_str)` calls in `_decision_from_stdout` with `HookDecision(decision="pass")` and add a `# G4 will plug context here` comment. The G4 commit will replace these.

After this revert, re-run: `pytest tests/test_shell_hook_stdout_protocol.py -v`. Expected: all seven tests still PASS (none of them check `inject_context` — that's Task 4 territory).

- [ ] **Step 6: Run existing settings-hook tests to check no regression**

Run: `cd OpenComputer && pytest tests/test_settings_hooks.py tests/test_phase_doc2_hooks.py -v`

Expected: all green.

- [ ] **Step 7: Run ruff**

Run: `ruff check OpenComputer/opencomputer/hooks/shell_handlers.py OpenComputer/tests/test_shell_hook_stdout_protocol.py`

Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add OpenComputer/opencomputer/hooks/shell_handlers.py OpenComputer/tests/test_shell_hook_stdout_protocol.py
git commit -m "feat(hooks): G3 — shell-hook stdout JSON wire protocol (Hermes + CC shapes)

Augments make_shell_hook_handler to parse stdout JSON before the
exit-code path. Both Hermes canonical and Claude Code shapes are
accepted:

  {\"action\":\"block\",\"message\":\"...\"}     (Hermes)
  {\"decision\":\"block\",\"reason\":\"...\"}    (Claude Code)
  {\"action\":\"approve\"|\"allow\"}             (explicit pass)
  {\"decision\":\"approve\"}                     (explicit pass)
  {} or unrecognised keys                        (pass with debug log)

Precedence: stdout JSON wins when both stdout JSON and exit code are
present. Exit-code fallback is preserved verbatim — existing OC
shell-hook scripts (which print {} or empty) hit the unchanged
exit-code path and behave identically.

Shell scripts ported from Hermes that emit JSON now work in OC unchanged.

Helper _decision_from_stdout is the single decision-shape translator —
G4 (context injection) plugs into it but currently always returns
pass+no-inject because HookDecision.inject_context lands in the next
commit.

7 new tests covering both shapes, malformed-JSON fallback, and the
precedence rule.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: G4 — Shell-hook `{"context":"..."}` injection on PRE_LLM_CALL

**Files:**
- Modify: `OpenComputer/plugin_sdk/hooks.py` — add `inject_context: str | None = None` to `HookDecision`
- Modify: `OpenComputer/opencomputer/hooks/shell_handlers.py` — uncomment the `inject_context=` kwarg in `_decision_from_stdout` (added in Task 3)
- Modify: `OpenComputer/opencomputer/agent/loop.py` — collect `inject_context` from blocking PRE_LLM_CALL decisions and append to user message
- Test: `OpenComputer/tests/test_shell_hook_stdout_protocol.py` — context-injection tests

- [ ] **Step 1: Add `inject_context` field to `HookDecision`**

Edit `OpenComputer/plugin_sdk/hooks.py`. Find the `HookDecision` dataclass (around line 193-208). Replace its body with:

```python
@dataclass(frozen=True, slots=True)
class HookDecision:
    """A hook's response. PreToolUse hooks use `decision` to approve/block."""

    # Wave 5 T13 — added "skip", "rewrite", "allow" verdicts for
    # PreGatewayDispatch (Hermes 1ef1e4c66). "skip" drops the message
    # silently, "rewrite" replaces gateway_event_text via rewritten_text,
    # "allow" is a positive ack equivalent to "pass" but documents that
    # the hook explicitly inspected and approved.
    decision: Literal[
        "approve", "block", "pass", "skip", "rewrite", "allow",
    ] = "pass"
    reason: str = ""
    modified_message: str = ""  # if set, injected as a system reminder
    #: Wave 5 T13 — for decision="rewrite", the new event text.
    rewritten_text: str | None = None
    #: 2026-05-08 G4 — text to inject into the user message for
    #: PRE_LLM_CALL only. Mirrors Hermes' shell-hook stdout
    #: ``{"context": "..."}`` shape and the existing plugin-side
    #: pre_llm_call return-value contract. Ignored for non-PRE_LLM_CALL
    #: events (callers in loop.py decide the apply-condition).
    inject_context: str | None = None
```

- [ ] **Step 2: Restore `inject_context=` kwargs in `_decision_from_stdout`**

Edit `OpenComputer/opencomputer/hooks/shell_handlers.py`. In `_decision_from_stdout`, replace the two placeholder lines that say `# G4 will plug context here` with the real kwargs:

* Where `HookDecision(decision="pass")` is returned alongside an `inject_str` and event is PRE_LLM_CALL, change to `HookDecision(decision="pass", inject_context=inject_str)`.

After Task 3 already wrote the structure with `inject_context=inject_str` then reverted them, this step re-applies them. The two locations to update are inside `_decision_from_stdout` where the function returns `HookDecision(decision="pass", inject_context=inject_str)`.

- [ ] **Step 3: Write the failing tests**

Add to `OpenComputer/tests/test_shell_hook_stdout_protocol.py`:

```python
def test_stdout_context_injection_only_on_pre_llm_call(tmp_path):
    """`{"context":"..."}` on PRE_LLM_CALL → decision=pass, inject_context=text."""
    script = _write_script(
        tmp_path,
        'cat - >/dev/null; printf \'%s\' \'{"context":"Today is Friday"}\'',
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.PRE_LLM_CALL)))
    assert decision is not None
    assert decision.decision == "pass"
    assert decision.inject_context == "Today is Friday"


def test_stdout_context_ignored_on_non_pre_llm_call(tmp_path):
    """`{"context":"..."}` on POST_TOOL_USE → no inject (just pass)."""
    script = _write_script(
        tmp_path,
        'cat - >/dev/null; printf \'%s\' \'{"context":"ignored"}\'',
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.POST_TOOL_USE)))
    assert decision is not None
    assert decision.decision == "pass"
    assert decision.inject_context is None


def test_stdout_approve_plus_context_works_on_pre_llm_call(tmp_path):
    """{"action":"approve","context":"..."} on PRE_LLM_CALL → pass + inject."""
    script = _write_script(
        tmp_path,
        'cat - >/dev/null; printf \'%s\' \'{"action":"approve","context":"branch=main"}\'',
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )
    decision = asyncio.run(handler(_ctx(HookEvent.PRE_LLM_CALL)))
    assert decision is not None
    assert decision.decision == "pass"
    assert decision.inject_context == "branch=main"
```

- [ ] **Step 4: Run new tests to verify they fail without loop wiring**

Run: `cd OpenComputer && pytest tests/test_shell_hook_stdout_protocol.py::test_stdout_context_injection_only_on_pre_llm_call tests/test_shell_hook_stdout_protocol.py::test_stdout_context_ignored_on_non_pre_llm_call tests/test_shell_hook_stdout_protocol.py::test_stdout_approve_plus_context_works_on_pre_llm_call -v`

Expected: all three tests should now PASS just from Steps 1-2 — they verify the HookDecision shape, not the loop integration. If they don't pass, debug the `_decision_from_stdout` translator.

- [ ] **Step 5: Verify wiring into agent/loop.py is correct**

Read the current PRE_LLM_CALL fire-point:

Run: `sed -n '3580,3640p' OpenComputer/opencomputer/agent/loop.py`

Expected: see the existing `_fire_pre_llm_call` helper that calls `engine.fire` (fire-and-forget).

We need to make this call `fire_blocking` instead so we can collect `inject_context` from each handler's decision, then append to the user message.

- [ ] **Step 6: Wire `inject_context` into the user message**

Edit `OpenComputer/opencomputer/agent/loop.py`. Find the `_fire_pre_llm_call` helper (or equivalent — search for `PRE_LLM_CALL`). The current pattern is fire-and-forget. We need to replace it with a blocking call that collects `inject_context` strings from every handler's decision and appends them to the user message before the LLM call.

Search for the canonical fire-point:

Run: `grep -n "PRE_LLM_CALL\|fire.*pre_llm\|_fire_pre_llm" OpenComputer/opencomputer/agent/loop.py`

Locate the function that fires PRE_LLM_CALL. Replace its single `engine.fire(ctx)` call with a blocking variant that collects `inject_context`. Conceptually:

```python
# Within whatever method currently fires PRE_LLM_CALL,
# replace `engine.fire(ctx)` (or fire-and-forget call) with:
collected_contexts: list[str] = []
async for decision in engine.fire_blocking_each(ctx):
    if decision and decision.inject_context:
        collected_contexts.append(decision.inject_context)
# Then, BEFORE calling provider.complete, append:
if collected_contexts:
    appended = "\n\n".join(collected_contexts)
    user_message_text += "\n\n" + appended
```

The exact integration depends on how `_fire_pre_llm_call` is structured. If `engine.fire_blocking` returns only the FIRST non-pass decision, we need a new method `fire_blocking_each` that returns an async iterator. Add it to `OpenComputer/opencomputer/hooks/engine.py`:

```python
async def fire_blocking_each(self, ctx: HookContext):
    """Fire all hooks for ``ctx.event`` and yield each handler's decision.

    Like :meth:`fire_blocking` but does NOT short-circuit on the first
    non-pass decision. Used by PRE_LLM_CALL where multiple handlers may
    contribute ``inject_context`` strings that all need to be collected.
    """
    for _, _, spec in self._hooks.get(ctx.event, []):
        if not self._matches(spec, ctx):
            continue
        try:
            if spec.timeout_ms and spec.timeout_ms > 0:
                decision = await asyncio.wait_for(
                    spec.handler(ctx),
                    timeout=spec.timeout_ms / 1000.0,
                )
            else:
                decision = await spec.handler(ctx)
        except (TimeoutError, Exception):  # noqa: BLE001 — fail-open
            continue
        yield decision
```

Then in `loop.py`, the call sites:

Find the PRE_LLM_CALL fire-point. Replace the fire-and-forget call with:

```python
        # 2026-05-08 G4 — collect inject_context from every blocking
        # PRE_LLM_CALL handler and append to user message.
        injected_contexts: list[str] = []
        async for decision in _hook_engine.fire_blocking_each(_pre_llm_ctx):
            if decision is not None and decision.inject_context:
                injected_contexts.append(decision.inject_context)
        if injected_contexts:
            joined = "\n\n".join(injected_contexts)
            # Append to user message — same shape used by InjectionEngine.
            user_message_for_provider = user_message_for_provider + "\n\n" + joined
```

(Replace `_pre_llm_ctx` and `user_message_for_provider` with the actual variable names in the current loop.)

- [ ] **Step 7: Write integration test for end-to-end injection**

Add to `OpenComputer/tests/test_shell_hook_stdout_protocol.py`:

```python
def test_pre_llm_call_inject_context_appended_to_user_message(tmp_path, monkeypatch):
    """End-to-end: registered shell hook emits {"context":"..."} → injected."""
    from opencomputer.hooks.engine import engine
    from opencomputer.hooks.shell_handlers import make_shell_hook_handler
    from plugin_sdk.hooks import HookSpec

    script = _write_script(
        tmp_path,
        'cat - >/dev/null; printf \'%s\' \'{"context":"git: clean"}\'',
    )
    handler = make_shell_hook_handler(
        HookCommandConfig(command=str(script), timeout_seconds=5.0)
    )

    engine.unregister_all()
    engine.register(HookSpec(event=HookEvent.PRE_LLM_CALL, handler=handler))
    try:
        contexts = []
        ctx = _ctx(HookEvent.PRE_LLM_CALL)

        async def collect():
            async for decision in engine.fire_blocking_each(ctx):
                if decision and decision.inject_context:
                    contexts.append(decision.inject_context)

        asyncio.run(collect())
        assert contexts == ["git: clean"]
    finally:
        engine.unregister_all()
```

- [ ] **Step 8: Run the full G4 test set**

Run: `cd OpenComputer && pytest tests/test_shell_hook_stdout_protocol.py -v`

Expected: all four new G4 tests + previous seven G3 tests PASS.

- [ ] **Step 9: Run the broader hook test suite to catch regressions**

Run: `cd OpenComputer && pytest tests/test_settings_hooks.py tests/test_phase_doc2_hooks.py tests/test_hook_expansion.py tests/test_cli_hooks.py -v`

Expected: all green.

- [ ] **Step 10: Run ruff**

Run: `ruff check OpenComputer/plugin_sdk/hooks.py OpenComputer/opencomputer/hooks/engine.py OpenComputer/opencomputer/hooks/shell_handlers.py OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/test_shell_hook_stdout_protocol.py`

Expected: clean.

- [ ] **Step 11: Commit**

```bash
git add OpenComputer/plugin_sdk/hooks.py OpenComputer/opencomputer/hooks/engine.py OpenComputer/opencomputer/hooks/shell_handlers.py OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/test_shell_hook_stdout_protocol.py
git commit -m "feat(hooks): G4 — shell-hook context injection on PRE_LLM_CALL

A 5-line bash script can now inject context into every turn:

  #!/usr/bin/env bash
  cat - >/dev/null
  status=\$(git status --porcelain 2>/dev/null) && [[ -n \"\$status\" ]] \\
    && jq --null-input --arg s \"\$status\" '{context: (\"Uncommitted:\\n\" + \$s)}' \\
    || printf '{}\\n'

Wired in two places:

- HookDecision gains an inject_context: str | None = None field
  (additive — existing handlers / decisions unaffected).
- shell_handlers._decision_from_stdout reads {\"context\":\"...\"} from
  parsed stdout JSON; populates inject_context only when event ==
  PRE_LLM_CALL. Non-PRE_LLM_CALL events: ignored (Hermes parity).
- engine.fire_blocking_each is the new fan-out variant: yields every
  handler's decision instead of short-circuiting on the first non-pass.
- agent/loop.py PRE_LLM_CALL fire-point switches from fire-and-forget
  to fire_blocking_each, collects all inject_context strings, joins
  with double newlines, appends to user message before provider call.

Symmetric with existing plugin-side pre_llm_call return-value contract
({\"context\": \"text\"}). Mirrors the InjectionEngine shape used by
DynamicInjectionProvider.

4 new tests covering: PRE_LLM_CALL inject, non-PRE_LLM_CALL ignored,
combined approve+context, end-to-end engine fan-out.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: G5 — `code_execution.max_tool_calls` config + PTC enforcement

**Files:**
- Modify: `OpenComputer/opencomputer/agent/config.py` — add `CodeExecutionConfig` dataclass
- Modify: `OpenComputer/opencomputer/tools/ptc.py` — accept `max_tool_calls` arg, plumb into prologue
- Modify: `OpenComputer/opencomputer/tools/execute_code.py` — read config and pass through
- Test: `OpenComputer/tests/test_execute_code_max_tool_calls.py` (new)

- [ ] **Step 1: Inspect current `code_execution` config plumbing**

Run: `grep -n "code_execution\|CodeExecution" OpenComputer/opencomputer/agent/config.py OpenComputer/opencomputer/tools/execute_code.py`

Expected: `execute_code.py` reads `code_execution.terminal.env_passthrough` from `default_config()`. `agent/config.py` may not have a typed `CodeExecutionConfig` dataclass yet.

- [ ] **Step 2: Write failing tests**

Create `OpenComputer/tests/test_execute_code_max_tool_calls.py`:

```python
"""G5 — code_execution.max_tool_calls override."""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_max_tool_calls_default_is_50():
    from opencomputer.tools.ptc import _MAX_RPC_CALLS

    # Implementation detail check — default cap stays at Hermes-spec 50.
    assert _MAX_RPC_CALLS == 50


@pytest.mark.asyncio
async def test_run_ptc_honours_max_tool_calls_override(monkeypatch, tmp_path):
    """A run_ptc(max_tool_calls=3) call caps the prologue at 3 RPC calls."""
    from opencomputer.tools.ptc import run_ptc
    from opencomputer.tools.registry import ToolRegistry

    # Use a stub registry with a Read-like tool that returns predictable text.
    registry = ToolRegistry()

    # Build a script that tries to make 5 RPC calls; should fail at the 4th.
    code = """
import sys
ok = 0
for i in range(5):
    try:
        Read(path='/tmp/does-not-exist-x')
    except RuntimeError as e:
        if 'cap exceeded' in str(e) or 'limit' in str(e).lower():
            print(f'CAPPED_AT_{i+1}')
            sys.exit(0)
    ok += 1
print(f'NEVER_CAPPED ok={ok}')
"""
    result = await run_ptc(
        code,
        registry=registry,
        allowed_tools=("Read",),
        timeout_s=15.0,
        max_tool_calls=3,
    )
    # The prologue should refuse the 4th call.
    assert "CAPPED_AT_" in result.stdout, result.stdout + "|" + result.stderr


@pytest.mark.asyncio
async def test_execute_code_reads_max_tool_calls_from_config(monkeypatch, tmp_path):
    """`ExecuteCode.execute(...)` plumbs config.code_execution.max_tool_calls."""
    from opencomputer.agent.config import CodeExecutionConfig

    cfg = CodeExecutionConfig()
    assert cfg.max_tool_calls == 50  # default

    cfg2 = CodeExecutionConfig(max_tool_calls=10)
    assert cfg2.max_tool_calls == 10
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd OpenComputer && pytest tests/test_execute_code_max_tool_calls.py -v`

Expected: tests FAIL — `CodeExecutionConfig` import errors and `run_ptc` rejects unknown `max_tool_calls` kwarg.

- [ ] **Step 4: Add `CodeExecutionConfig` dataclass**

Edit `OpenComputer/opencomputer/agent/config.py`. Find a logical place near other config dataclasses. Add:

```python
@dataclass
class CodeExecutionConfig:
    """Settings for the ExecuteCode / PythonExec tool family.

    All fields optional; defaults match Hermes Doc-2 spec values.
    """

    timeout_seconds: float = 300.0
    max_tool_calls: int = 50
    terminal: dict[str, Any] = field(default_factory=dict)
```

If a `code_execution: dict | CodeExecutionConfig` field already exists on the parent `Config`, leave the YAML schema accepting both shapes; if not, add it as `code_execution: CodeExecutionConfig = field(default_factory=CodeExecutionConfig)` with the YAML loader coercing dict → dataclass.

- [ ] **Step 5: Plumb `max_tool_calls` through `run_ptc`**

Edit `OpenComputer/opencomputer/tools/ptc.py`. Find `_build_prologue(allowed)` and change its signature to accept `max_tool_calls: int = _MAX_RPC_CALLS`. Replace the line `f"_ptc_max_calls = {_MAX_RPC_CALLS}",` with `f"_ptc_max_calls = {max_tool_calls}",`.

Find `run_ptc(...)` and add a `max_tool_calls: int | None = None` parameter. If non-None, pass through to `_build_prologue`. Otherwise fall back to `_MAX_RPC_CALLS`.

- [ ] **Step 6: Read config in `ExecuteCode.execute`**

Edit `OpenComputer/opencomputer/tools/execute_code.py`. In the `try:` block where it currently reads `code_execution.terminal.env_passthrough`, also read `code_execution.max_tool_calls`. Pass to `run_ptc(max_tool_calls=...)`.

Diff:
```python
        # Resolve env passthrough from config.yaml: code_execution.terminal.env_passthrough
        passthrough: tuple[str, ...] = ()
+        max_tool_calls: int | None = None
        try:
            from opencomputer.agent.config import default_config

            cfg = default_config()
            ce = getattr(cfg, "code_execution", None)
            if ce is not None:
                terminal_cfg = getattr(ce, "terminal", None)
                if isinstance(terminal_cfg, dict):
                    pt = terminal_cfg.get("env_passthrough")
                    if isinstance(pt, list):
                        passthrough = tuple(str(x) for x in pt)
+                # Hermes Doc-2: code_execution.max_tool_calls
+                mtc = getattr(ce, "max_tool_calls", None)
+                if isinstance(mtc, int) and mtc > 0:
+                    max_tool_calls = mtc
        except Exception:
            pass
```

And in the `await run_ptc(...)` call, add `max_tool_calls=max_tool_calls,` to the kwargs.

- [ ] **Step 7: Run G5 tests**

Run: `cd OpenComputer && pytest tests/test_execute_code_max_tool_calls.py -v`

Expected: all three tests PASS.

- [ ] **Step 8: Run broader execute_code / PTC tests for regression**

Run: `cd OpenComputer && pytest tests/test_pr8_exec_trace_and_bus_hooks.py -v -k ptc`

Run: `cd OpenComputer && pytest -k "execute_code or ptc" -v`

Expected: green.

- [ ] **Step 9: Run ruff**

Run: `ruff check OpenComputer/opencomputer/agent/config.py OpenComputer/opencomputer/tools/ptc.py OpenComputer/opencomputer/tools/execute_code.py OpenComputer/tests/test_execute_code_max_tool_calls.py`

Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add OpenComputer/opencomputer/agent/config.py OpenComputer/opencomputer/tools/ptc.py OpenComputer/opencomputer/tools/execute_code.py OpenComputer/tests/test_execute_code_max_tool_calls.py
git commit -m "feat(execute_code): G5 — code_execution.max_tool_calls config + PTC enforcement

Closes the last documented Hermes Doc-2 config slot. Default stays at
50 (Hermes spec). Override via config.yaml:

  code_execution:
    max_tool_calls: 100

Implementation:
- New CodeExecutionConfig dataclass in agent/config.py with
  timeout_seconds, max_tool_calls, terminal slots.
- run_ptc gains max_tool_calls: int | None = None kwarg; threads
  through _build_prologue so the in-script _ptc_max_calls constant
  reflects the override.
- ExecuteCode.execute reads max_tool_calls from default_config() and
  passes through.

3 new tests covering: default = 50, run_ptc override caps at N, config
override propagates.

Closes the silent footgun where a buggy script doing
'while True: read_file()' would consume API quota until the 300s
timeout fired (cap was hardcoded — not configurable).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 6: Documentation surface update

**Files:**
- Modify: `OpenComputer/CLAUDE.md` — III.6 settings hooks contract
- Modify: `OpenComputer/docs/refs/hermes-agent/2026-05-08-kanban-goals-execcode-hooks-parity.md` — append §2.5

- [ ] **Step 1: Read CLAUDE.md III.6 to anchor the edit**

Run: `grep -n "III.6 settings\|Settings-based hooks\|^**Settings-based hooks" OpenComputer/CLAUDE.md`

Expected: hit on the `**Settings-based hooks` heading. Read 30-40 lines around it for context.

- [ ] **Step 2: Update CLAUDE.md III.6 with the augmented stdout JSON contract**

Edit the `Exit-code contract` paragraph in CLAUDE.md III.6 to reflect the augmented protocol. Replace the current paragraph that says only:

> Exit-code contract (matches Claude Code): `0` → pass (tool runs), `2` → block with stderr fed back as the reason, any other code → fail-open (warn + pass)…

With:

```markdown
**Wire protocol** (augmented 2026-05-08 — Hermes Doc-2 G3/G4):

* **stdout JSON (preferred)** — when the script's stdout parses as a JSON object, recognised keys take precedence over the exit code:
  - `{"action": "block", "message": "..."}` → block (Hermes canonical)
  - `{"decision": "block", "reason": "..."}` → block (Claude Code)
  - `{"action": "approve" | "allow"}` or `{"decision": "approve"}` → pass
  - `{"context": "..."}` → on PRE_LLM_CALL only, append text to user message; ignored on other events
  - `{}` or unrecognised keys → pass
  - malformed JSON → fall back to exit-code path
* **Exit-code (fallback)** — when stdout is empty or non-JSON: `0` → pass, `2` → block with stderr as reason, anything else → fail-open warn+pass.
* **Timeouts and crashes** — fail-open. A wedged hook must never wedge the loop.
```

- [ ] **Step 3: Append §2.5 to the parity findings doc**

Edit `OpenComputer/docs/refs/hermes-agent/2026-05-08-kanban-goals-execcode-hooks-parity.md`. After §2.4 (the table of "Doc 2 — gaps closed in this PR") and before `## 3. Phase 3 — execute_code (this PR)`, insert:

```markdown
### 2.5 Doc 2 — residual gaps closed in follow-up PR (2026-05-08)

A second pass on this same Hermes doc identified 5 smaller residuals that pass the makes-sense filter and ship in PR #<NNN> (worktree `feat/hermes-execcode-hooks-residuals-2026-05-08`):

| Residual | What we did |
|---|---|
| `oc hooks test --execute` was unimplemented | Now fires synthetic events through the engine (blocking + fire-and-forget paths). Adds `--for-tool` to populate `ctx.tool_call.name` for Pre/PostToolUse. |
| No `oc hooks doctor` operability surface | New subcommand surfaces gateway-hook health (HOOK.yaml validity, handler.py imports), settings-hook executable resolution, recent-fire timestamps, and a note that OC has no allowlist by design. |
| Shell hooks accepted only exit-code-based block | Now also accepts both Hermes canonical `{"action":"block","message":"..."}` and Claude Code `{"decision":"block","reason":"..."}` on stdout. Stdout JSON wins when both are present. Exit-code path preserved verbatim. |
| Shell hooks could not inject context | New `inject_context` field on `HookDecision`. Shell scripts emit `{"context":"..."}` on stdout for PRE_LLM_CALL; engine fan-out collects all and appends to user message. Lets a 5-line bash script inject git status without writing a Python plugin. |
| `code_execution.max_tool_calls` was hardcoded at 50 | Now configurable via `code_execution.max_tool_calls` in `config.yaml`. Closes the silent footgun where a buggy `while True: read()` script could consume API quota until the 300s timeout fired. |

These five close the spec-level parity gap with the Hermes "Code Execution & Event Hooks" reference doc; the parity question for this specific reference is closed pending future Hermes doc updates.

Out of scope (deliberate, with reopen triggers):

- Shell-hook allowlist + per-`(event, command)` consent prompt + `--accept-hooks` flag → OC's design says editing `config.yaml` IS consent. ~200 LOC for marginal value. **Reopen if** a user reports a real "didn't realize I shipped a hook" incident.
- `hermes_tools` import-shim aliases in execute_code prologue → pure sugar; OC tool stubs are PascalCase by convention. **Reopen if** cross-port script-pasting becomes friction.
```

- [ ] **Step 4: Commit**

```bash
git add OpenComputer/CLAUDE.md OpenComputer/docs/refs/hermes-agent/2026-05-08-kanban-goals-execcode-hooks-parity.md
git commit -m "docs(hooks): augmented JSON wire protocol + Hermes-doc-2 residuals follow-up note

CLAUDE.md III.6 — replace exit-code-only contract paragraph with the
augmented Hermes Doc-2 protocol (stdout JSON wins; exit-code fallback
preserved).

docs/refs/hermes-agent/2026-05-08-kanban-goals-execcode-hooks-parity.md
— append §2.5 documenting the 5 residual gaps closed in this PR's
worktree (G1 hooks test --execute, G2 hooks doctor, G3 stdout JSON,
G4 context injection, G5 max_tool_calls config). Closes the parity
question for this specific Hermes reference.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 7: Final validation gate

- [ ] **Step 1: Run the full test suite**

Run: `cd OpenComputer && pytest tests/ -q`

Expected: green. Watch for any cross-cutting regression in the existing 9000+ tests. Per memory `feedback_full_suite_audit.md`, do not skip this.

- [ ] **Step 2: Run ruff across the whole touched surface**

Run: `cd OpenComputer && ruff check opencomputer/ plugin_sdk/ tests/`

Expected: clean.

- [ ] **Step 3: Push branch + open PR**

```bash
git push -u origin worktree-hermes-execcode-hooks-residuals-2026-05-08

gh pr create --title "feat(hooks): Hermes Doc-2 residuals — 5 gaps after PR #496" --body "$(cat <<'EOF'
## Summary

Closes 5 verified residual gaps from the Hermes 'Code Execution & Event Hooks' reference doc that PR #496 didn't ship — debug surfaces and protocol-parity gaps that pass the makes-sense filter.

- **G1.** `oc hooks test --execute` actually fires synthetic events
- **G2.** `oc hooks doctor` health diagnostics
- **G3.** Shell-hook stdout JSON wire protocol (Hermes canonical + Claude Code shapes)
- **G4.** Shell-hook `{"context":"..."}` injection on PRE_LLM_CALL (lets a 5-line bash script inject git status)
- **G5.** `code_execution.max_tool_calls` config + PTC enforcement

Out of scope (deliberate): shell-hook allowlist + consent prompt + `--accept-hooks` flag (OC's design says config.yaml-edit IS consent; ~200 LOC for marginal value); `hermes_tools` import shim (sugar).

Spec: `OpenComputer/docs/superpowers/specs/2026-05-08-hermes-execcode-hooks-residuals-design.md`
Plan: `OpenComputer/docs/superpowers/plans/2026-05-08-hermes-execcode-hooks-residuals.md`

## Test plan
- [x] All new tests pass per task (~24 new tests across 6 commits)
- [x] Existing settings-hook + cli-hooks + phase-doc2 tests still green
- [x] Full pytest suite green
- [x] Ruff clean

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Expected: PR URL printed.

---

## Self-review

**1. Spec coverage:** Each spec section maps to a task — G1→T1, G2→T2, G3→T3, G4→T4, G5→T5, doc updates→T6, validation→T7. ✓

**2. Placeholder scan:** No "TBD" / "fill in details" / "implement appropriate" / "similar to Task N" without code. Each step has either exact code or an exact command. ✓

**3. Type consistency:** `HookDecision.inject_context: str | None` used consistently in T4 step 1 and step 2. `CodeExecutionConfig.max_tool_calls: int = 50` used consistently in T5 step 4 and step 6. ✓

**4. Cross-task dependencies:** T4 depends on T3 (shared `_decision_from_stdout` helper). T3 step 5 explicitly notes the temporary revert; T4 step 2 explicitly restores. ✓

**5. Test independence:** T1, T2, T5 are independent. T3 and T4 share a test file but each commit's tests stand alone (T3's seven tests don't reference `inject_context`; T4's three do). ✓
