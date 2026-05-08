"""``oc hooks`` — list / test / clear / revoke for debug observability.

The hook system has 17 lifecycle events declared in
plugin_sdk.hooks.HookEvent. Settings, plugins, and config.yaml can
register handlers. With no CLI surface, "why didn't my hook fire"
required reading source. This module adds the missing observability
layer.

Subcommands:
    oc hooks list [--json]
    oc hooks test EVENT [--payload JSON] [--execute]
    oc hooks clear
    oc hooks revoke PLUGIN_ID
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.hook_history import (
    all_events,
    clear_history,
    iter_history,
)

hooks_app = typer.Typer(help="Inspect and manage agent hooks.")
_console = Console()


def _profile_dir() -> Path:
    raw = os.environ.get("OC_PROFILE_DIR") or str(Path.home() / ".opencomputer" / "default")
    return Path(raw).expanduser()


def _known_events() -> list[str]:
    """Return all declared HookEvent values, falling back to history keys."""
    try:
        from plugin_sdk.hooks import HookEvent

        return sorted(e.value for e in HookEvent)
    except Exception:  # noqa: BLE001
        return sorted(all_events())


def _last_fire(event: str) -> dict | None:
    records = list(iter_history(event))
    if not records:
        return None
    rec = records[-1]
    return {
        "ts_utc": rec.ts_utc,
        "source": rec.source_id,
        "ok": rec.ok,
        "summary": rec.summary,
    }


@hooks_app.command("list")
def cmd_list(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List all known hook events with last-fire metadata."""
    rows: list[dict] = []
    for event in _known_events():
        last = _last_fire(event)
        rows.append(
            {
                "event": event,
                "last_fired_utc": (
                    datetime.fromtimestamp(last["ts_utc"], tz=UTC).isoformat() if last else None
                ),
                "last_source": last["source"] if last else None,
                "last_result": (("ok" if last["ok"] else "err") if last else None),
                "last_summary": last["summary"] if last else None,
            }
        )

    if json_out:
        typer.echo(json.dumps(rows))
        return

    table = Table(title="Hook events")
    table.add_column("Event", style="cyan")
    table.add_column("Last fired (UTC)", style="dim")
    table.add_column("Source")
    table.add_column("Result")
    table.add_column("Summary")
    for row in rows:
        table.add_row(
            row["event"],
            row["last_fired_utc"] or "—",
            row["last_source"] or "—",
            row["last_result"] or "—",
            (row["last_summary"] or "")[:40],
        )
    _console.print(table)


@hooks_app.command("test")
def cmd_test(
    event: str = typer.Argument(..., help="Hook event name (e.g. UserPromptSubmit)."),
    payload: str = typer.Option("{}", "--payload", help="JSON-encoded synthetic payload."),
    for_tool: str = typer.Option(
        "",
        "--for-tool",
        help="Tool name for Pre/PostToolUse synthetic ctx.tool_call.name.",
    ),
    execute: bool = typer.Option(
        False, "--execute", help="Actually dispatch (default: dry-run)."
    ),
) -> None:
    """Fire a synthetic hook event. Default is dry-run.

    With ``--execute``, builds a synthetic :class:`HookContext` and routes
    through :func:`engine.fire_blocking` for events that can block
    (PRE_TOOL_USE, PRE_LLM_CALL, PRE_GATEWAY_DISPATCH, PRE_APPROVAL_REQUEST)
    or :func:`engine.fire` (fire-and-forget) for the rest.
    """
    try:
        payload_obj = json.loads(payload)
    except json.JSONDecodeError as exc:
        _console.print(f"[red]Invalid --payload JSON:[/red] {exc}")
        raise typer.Exit(1)

    if not execute:
        _console.print(f"[yellow]dry-run:[/yellow] would fire {event} with {payload_obj!r}")
        # Best-effort: surface registered handlers from the engine without
        # invoking them.
        try:
            from opencomputer.hooks.engine import engine
            from plugin_sdk.hooks import HookEvent

            try:
                event_enum = HookEvent(event)
            except ValueError:
                _console.print(
                    f"  [dim](unknown event {event!r}; "
                    f"known events: {[e.value for e in HookEvent]})[/dim]"
                )
                return
            specs = engine._ordered_specs(event_enum)  # noqa: SLF001
            if not specs:
                _console.print("  [dim](no handlers registered for this event)[/dim]")
            for spec in specs:
                handler_id = getattr(spec.handler, "__qualname__", repr(spec.handler))
                _console.print(f"  would invoke: {handler_id}")
        except Exception as exc:  # noqa: BLE001
            _console.print(f"  [dim](handler enumeration unavailable: {exc})[/dim]")
        return

    # 2026-05-08 G1 — real dispatch. Build a synthetic HookContext and
    # invoke the engine through the production code path.
    try:
        from opencomputer.hooks.engine import engine
        from plugin_sdk.core import ToolCall
        from plugin_sdk.hooks import HookContext, HookEvent

        try:
            event_enum = HookEvent(event)
        except ValueError:
            _console.print(
                f"[red]Unknown event {event!r}[/red]; "
                f"known events: {[e.value for e in HookEvent]}"
            )
            raise typer.Exit(1)

        # ToolCall is built only when --for-tool is provided so non-tool
        # events don't get a misleading stub call.
        tool_call = None
        if for_tool:
            tool_call = ToolCall(
                id="oc-hooks-test-synthetic",
                name=for_tool,
                arguments=payload_obj if isinstance(payload_obj, dict) else {},
            )
        ctx = HookContext(
            event=event_enum,
            session_id=str(payload_obj.get("session_id", "oc-hooks-test"))
            if isinstance(payload_obj, dict)
            else "oc-hooks-test",
            tool_call=tool_call,
        )

        specs = engine._ordered_specs(event_enum)  # noqa: SLF001
        if not specs:
            _console.print(f"[dim]0 handlers registered for {event}[/dim]")
            return

        blocking_events = {
            HookEvent.PRE_TOOL_USE,
            HookEvent.PRE_LLM_CALL,
            HookEvent.PRE_GATEWAY_DISPATCH,
            HookEvent.PRE_APPROVAL_REQUEST,
        }
        if event_enum in blocking_events:
            decision = asyncio.run(engine.fire_blocking(ctx))
            if decision is None:
                _console.print(
                    f"[green]{event}[/green]: {len(specs)} handler(s) "
                    f"ran, all returned pass"
                )
            else:
                _console.print(
                    f"[yellow]{event}[/yellow]: first non-pass decision "
                    f"= [bold]{decision.decision}[/bold] "
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


@hooks_app.command("clear")
def cmd_clear() -> None:
    """Clear in-memory hook fire history."""
    n = clear_history()
    _console.print(f"[green]Cleared {n} fire records.[/green]")


@hooks_app.command("doctor")
def cmd_doctor(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Diagnostic health check: gateway hooks, settings hooks, recent activity.

    Surface health issues (broken HOOK.yaml, missing handle(), bad command
    paths) before they manifest as silent fail-open behaviour at runtime.
    Mirrors ``hermes hooks doctor`` from the Hermes Doc-2 reference doc.
    """
    rows: list[dict[str, str]] = []

    # 1. Gateway file-discovery hooks — walk hooks_root directly so we
    # can surface broken hook directories that ``discover_hooks`` skips.
    try:
        import importlib.util as _ilu

        import yaml as _yaml

        from opencomputer.gateway.event_hooks import (
            KNOWN_EVENTS,
            discover_hooks,
            hooks_root,
        )

        root = hooks_root()
        if not root.exists() or not root.is_dir():
            rows.append(
                {
                    "severity": "INFO",
                    "check": "gateway-hooks-dir",
                    "detail": f"{root} does not exist (0 gateway file-discovery hooks)",
                }
            )
        else:
            valid_specs = discover_hooks(root)
            valid_names = {hk.name for hk in valid_specs}
            all_dirs = [d for d in sorted(root.iterdir()) if d.is_dir()]
            rows.append(
                {
                    "severity": "INFO",
                    "check": "gateway-hooks-count",
                    "detail": (
                        f"{len(valid_specs)} valid / {len(all_dirs)} "
                        f"gateway hook directories at {root}"
                    ),
                }
            )

            # Per-directory validation: surface broken ones as ERROR rows.
            for entry in all_dirs:
                manifest_path = entry / "HOOK.yaml"
                handler_path = entry / "handler.py"
                if not manifest_path.is_file() or not handler_path.is_file():
                    rows.append(
                        {
                            "severity": "ERROR",
                            "check": f"gateway-hook:{entry.name}",
                            "detail": "missing HOOK.yaml or handler.py",
                        }
                    )
                    continue
                try:
                    manifest = (
                        _yaml.safe_load(
                            manifest_path.read_text(encoding="utf-8")
                        )
                        or {}
                    )
                except _yaml.YAMLError as exc:
                    rows.append(
                        {
                            "severity": "ERROR",
                            "check": f"gateway-hook:{entry.name}",
                            "detail": f"malformed HOOK.yaml: {exc}",
                        }
                    )
                    continue
                events = manifest.get("events") or []
                if not isinstance(events, list) or not all(
                    isinstance(e, str) for e in events
                ):
                    rows.append(
                        {
                            "severity": "ERROR",
                            "check": f"gateway-hook:{entry.name}",
                            "detail": "HOOK.yaml 'events' must be list[str]",
                        }
                    )
                    continue
                # Validate handler.py defines async handle without importing —
                # discover_hooks already does the import check; if name not
                # in valid_names, it failed import.
                if entry.name not in valid_names:
                    rows.append(
                        {
                            "severity": "ERROR",
                            "check": f"gateway-hook:{entry.name}",
                            "detail": (
                                "handler.py failed to import or missing "
                                "async def handle(event_type, context)"
                            ),
                        }
                    )
                    # Defensive: keep _ilu import side-effect explicit
                    # to silence lint if somehow this module compiles.
                    _ = _ilu
                    continue
                # Valid — check event names
                unknown = [
                    e
                    for e in events
                    if not (
                        e in KNOWN_EVENTS
                        or any(
                            e.startswith(known.rstrip("*"))
                            for known in KNOWN_EVENTS
                            if known.endswith(":*")
                        )
                        or e.startswith("command:")
                    )
                ]
                if unknown:
                    rows.append(
                        {
                            "severity": "WARN",
                            "check": f"gateway-hook:{entry.name}",
                            "detail": f"unknown events: {unknown}",
                        }
                    )
                else:
                    rows.append(
                        {
                            "severity": "OK",
                            "check": f"gateway-hook:{entry.name}",
                            "detail": f"events={events}",
                        }
                    )
    except Exception as exc:  # noqa: BLE001
        rows.append(
            {
                "severity": "ERROR",
                "check": "gateway-hooks-discovery",
                "detail": f"discovery raised: {type(exc).__name__}: {exc}",
            }
        )

    # 2. Settings hooks (config.yaml hooks: block)
    try:
        from opencomputer.agent.config import default_config

        cfg = default_config()
        sh = getattr(cfg, "hooks", None)
        if sh:
            for event_name, configs in sh.items():
                for cmd_config in configs or []:
                    cmd = getattr(cmd_config, "command", "") or ""
                    parts = cmd.split()
                    exe = parts[0] if parts else ""
                    if exe.startswith("/") and not os.path.exists(exe):
                        rows.append(
                            {
                                "severity": "WARN",
                                "check": f"settings-hook:{event_name}",
                                "detail": f"executable not found: {exe}",
                            }
                        )
                    elif exe.startswith("/"):
                        try:
                            st = os.stat(exe)
                            if not (st.st_mode & stat.S_IXUSR):
                                rows.append(
                                    {
                                        "severity": "WARN",
                                        "check": f"settings-hook:{event_name}",
                                        "detail": f"not user-executable: {exe}",
                                    }
                                )
                            else:
                                rows.append(
                                    {
                                        "severity": "OK",
                                        "check": f"settings-hook:{event_name}",
                                        "detail": f"command={cmd[:80]}",
                                    }
                                )
                        except OSError as exc:
                            rows.append(
                                {
                                    "severity": "WARN",
                                    "check": f"settings-hook:{event_name}",
                                    "detail": f"stat failed: {exc}",
                                }
                            )
                    else:
                        rows.append(
                            {
                                "severity": "INFO",
                                "check": f"settings-hook:{event_name}",
                                "detail": f"PATH-resolved command: {cmd[:80]}",
                            }
                        )
        else:
            rows.append(
                {
                    "severity": "INFO",
                    "check": "settings-hooks",
                    "detail": "no hooks: block in config.yaml",
                }
            )
    except Exception as exc:  # noqa: BLE001
        rows.append(
            {
                "severity": "INFO",
                "check": "settings-hooks",
                "detail": f"config not loadable: {type(exc).__name__}",
            }
        )

    # 3. Recent fire history — surface staleness
    try:
        events_with_fires = list(all_events())
        if events_with_fires:
            for event_name in events_with_fires[:5]:
                records = list(iter_history(event_name))
                if records:
                    last = records[-1]
                    rows.append(
                        {
                            "severity": "OK" if last.ok else "WARN",
                            "check": f"recent-fire:{event_name}",
                            "detail": (
                                f"{last.ts_utc:.0f} src={last.source_id[:40]} "
                                f"ok={last.ok}"
                            ),
                        }
                    )
        else:
            rows.append(
                {
                    "severity": "INFO",
                    "check": "recent-fires",
                    "detail": "no hook fires recorded yet",
                }
            )
    except Exception:  # noqa: BLE001
        pass

    # 4. Note: OC has no shell-hook allowlist by design
    rows.append(
        {
            "severity": "INFO",
            "check": "shell-hook-allowlist",
            "detail": (
                "OC has no allowlist (config.yaml-edit IS consent); "
                "OPENCOMPUTER_ACCEPT_HOOKS env var is a no-op"
            ),
        }
    )

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


@hooks_app.command("revoke")
def cmd_revoke(
    plugin_id: str = typer.Argument(..., help="Plugin id to disable hooks for."),
) -> None:
    """Disable a plugin's hooks via settings.local.json."""
    target = _profile_dir() / "settings.local.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        try:
            data = json.loads(target.read_text())
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    revoked = list(data.get("disabled_hooks", []))
    if plugin_id not in revoked:
        revoked.append(plugin_id)
    data["disabled_hooks"] = revoked
    target.write_text(json.dumps(data, indent=2))
    _console.print(f"[green]Revoked hooks for[/green] {plugin_id}")
    _console.print(f"  written to: {target}")
