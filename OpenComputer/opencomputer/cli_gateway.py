"""Typer subcommand group: ``oc gateway *``.

Single command verb for daemon ops + setup + service lifecycle + DM pairing
+ home-channel routing. Backwards-compat: bare ``oc gateway`` runs the
foreground daemon; ``--install-daemon`` flag still works (deprecated).

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T1.8)
Spec: docs/superpowers/specs/2026-05-08-messaging-gateway-parity-design.md (§5.1)
"""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


# ── Typer apps ──────────────────────────────────────────────────────────────


gateway_app = typer.Typer(
    name="gateway",
    help="Run, configure, and manage the messaging gateway daemon.",
    invoke_without_command=True,
    no_args_is_help=False,
)
pairing_app = typer.Typer(
    name="pairing",
    help="DM pairing — approve users by one-time code.",
    no_args_is_help=True,
)


# ── Helper: profile home resolution ─────────────────────────────────────────


def _profile_home() -> Path:
    """Return the active profile's home dir."""
    try:
        from opencomputer.agent.config_store import config_file_path

        return config_file_path().parent
    except Exception:  # noqa: BLE001
        return Path(
            os.environ.get("OPENCOMPUTER_HOME", str(Path.home() / ".opencomputer"))
        ) / "default"


# ── Foreground runner — delegates to the existing cli.gateway() body ────────


def _run_foreground(install_daemon: bool = False, daemon_profile: str = "default") -> None:
    """Equivalent to the historic ``oc gateway`` — runs the daemon in fg.

    The actual body lives in ``opencomputer.cli`` (which we cannot easily
    import here without circulars). We re-implement the daemon-bootstrap
    inline; the source of truth is mirrored from the original
    ``cli.gateway()`` command.
    """
    # Defer all heavy imports to inside the call — keeps Typer help fast.
    import asyncio

    if install_daemon:
        from opencomputer.service.factory import get_backend

        backend = get_backend()
        result = backend.install(profile=daemon_profile, extra_args="gateway")
        typer.echo(f"Installed {result.backend} service at {result.config_path}")
        for note in result.notes:
            typer.echo(f"note: {note}")
        raise typer.Exit(0)

    from opencomputer.agent.config_store import load_config
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.gateway.server import Gateway
    from opencomputer.mcp.client import MCPManager
    from opencomputer.plugins.registry import registry as plugin_registry
    from opencomputer.tools.registry import registry as tool_registry

    cfg = load_config()

    # Best-effort wiring of the new pairing-code + allowlist-gate machinery.
    # Falls back gracefully if the new modules aren't fully integrated yet.
    try:
        from opencomputer.channels.allowlist import AllowlistGate
        from opencomputer.channels.pairing_codes import PairingCodeStore
        from opencomputer.gateway.reset_policy import (
            ResetPolicy,
            ResetPolicyChecker,
            ResetPolicyConfig,
        )

        home = _profile_home()
        pairing_store = PairingCodeStore(home)
        allowlist_gate = AllowlistGate(profile_home=home, pairing_store=pairing_store)
        cfg_gw = cfg.gateway
        # Compose the per-platform overrides from raw dict into ResetPolicy
        # objects (the YAML loader keeps them as plain dicts).
        by_platform = {}
        for plat, raw in (cfg_gw.reset_by_platform or {}).items():
            if isinstance(raw, dict):
                by_platform[plat] = ResetPolicy(
                    mode=raw.get("mode", cfg_gw.reset_mode),
                    daily_at_hour=int(
                        raw.get("daily_at_hour", cfg_gw.reset_daily_at_hour)
                    ),
                    idle_minutes=int(
                        raw.get("idle_minutes", cfg_gw.reset_idle_minutes)
                    ),
                )
        rp_cfg = ResetPolicyConfig(
            default=ResetPolicy(
                mode=cfg_gw.reset_mode,
                daily_at_hour=cfg_gw.reset_daily_at_hour,
                idle_minutes=cfg_gw.reset_idle_minutes,
            ),
            by_platform=by_platform,
        )
        reset_policy = ResetPolicyChecker(rp_cfg)
        last_seen_path = home / "gateway" / "last_seen.json"
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[dim]gateway: pairing/reset wiring unavailable: {exc}[/dim]"
        )
        allowlist_gate = None
        reset_policy = None
        last_seen_path = None

    # Resolve provider via the same path the existing CLI uses. Lazy
    # import keeps `oc gateway --help` quick.
    from opencomputer.cli import (  # type: ignore
        _apply_model_overrides,
        _check_provider_key,
        _configure_logging_once,
        _discover_and_register_agents,
        _discover_plugins,
        _register_builtin_tools,
        _register_settings_hooks,
        _resolve_provider,
    )
    from opencomputer.tools.delegate import DelegateTool

    _configure_logging_once()
    _check_provider_key(cfg.model.provider)
    _register_builtin_tools()
    n_plugins = _discover_plugins()
    _apply_model_overrides()
    _discover_and_register_agents()
    _register_settings_hooks(cfg)
    provider = _resolve_provider(cfg.model.provider)
    loop = AgentLoop(provider=provider, config=cfg)
    DelegateTool.set_factory(lambda: AgentLoop(provider=provider, config=cfg))

    # Register OC's built-in injection providers for the gateway surface.
    # One call wires ThinkingInjector, PathGlobRulesProvider,
    # HandoffInjectionProvider (ContextVar-aware resolver) and
    # LifeEventInjectionProvider — each idempotent + fail-soft.
    from opencomputer.agent.injection_registration import (
        register_default_injection_providers,
    )
    register_default_injection_providers("gateway")

    # mcp-openclaw-port M2 — session_scoped MCP is intentionally NOT wired
    # into gateway mode. The gateway dispatches one AgentLoop across many
    # sessions through one shared tool registry; per-session MCP would
    # require a per-session tool registry refactor (out of M2 scope).
    # Document the constraint loudly so a user who flipped the flag in
    # config.yaml expecting gateway behaviour doesn't get a silent no-op.
    if cfg.mcp.session_scoped:
        import logging as _log_mod
        _log_mod.getLogger("opencomputer.cli_gateway").warning(
            "MCPConfig.session_scoped=True is set but gateway mode does not "
            "honor per-session MCP runtimes today (chat mode only). All "
            "gateway sessions share one process-global MCPManager. See "
            "docs/plans/mcp-openclaw-port.md §3.M2 for the constraint."
        )
    mcp_mgr = MCPManager(tool_registry=tool_registry)
    # Gap G — publish the active MCPManager so LazyBundleStubTool can
    # find it at first-tool-call wakeup.
    try:
        from opencomputer.mcp.manager_registry import set_active_manager

        set_active_manager(mcp_mgr)
    except Exception:  # noqa: BLE001 — wakeup is opt-in; never block
        pass

    gw = Gateway(loop=loop, config=cfg.gateway)
    # Wire the new gates if present (Gateway forwards to its Dispatch).
    if allowlist_gate is not None and hasattr(gw, "set_allowlist_gate"):
        gw.set_allowlist_gate(allowlist_gate)
    if reset_policy is not None and hasattr(gw, "set_reset_policy"):
        gw.set_reset_policy(reset_policy, last_seen_path=last_seen_path)
    for platform_name, adapter in plugin_registry.channels.items():
        console.print(
            f"[dim]registering channel:[/dim] [cyan]{platform_name}[/cyan]"
        )
        gw.register_adapter(adapter)

    if not gw.adapters:
        console.print(
            "[bold yellow]warning:[/bold yellow] no channel adapters registered. "
            "Set TELEGRAM_BOT_TOKEN (or another channel token) and ensure the "
            "channel plugin is discovered."
        )
        console.print(f"[dim]plugins loaded: {n_plugins}[/dim]")
        raise typer.Exit(1)

    console.print(
        f"[bold cyan]OpenComputer gateway[/bold cyan] — "
        f"{len(gw.adapters)} channel(s), model={cfg.model.model}"
    )
    console.print("[dim]ctrl+c to stop[/dim]\n")

    async def _run():
        if cfg.mcp.servers:
            asyncio.create_task(
                mcp_mgr.connect_all(
                    list(cfg.mcp.servers),
                    osv_check_enabled=cfg.mcp.osv_check_enabled,
                    osv_check_fail_closed=cfg.mcp.osv_check_fail_closed,
                )
            )
        try:
            await gw.serve_forever()
        finally:
            await mcp_mgr.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]gateway stopped[/dim]")


# ── Default callback (bare `oc gateway`) ───────────────────────────────────


@gateway_app.callback()
def _default(
    ctx: typer.Context,
    install_daemon: bool = typer.Option(
        False,
        "--install-daemon",
        help="DEPRECATED: use `oc gateway install`. Still works.",
        hidden=True,
    ),
    daemon_profile: str = typer.Option(
        "default",
        "--daemon-profile",
        help="DEPRECATED: use `oc gateway install --profile`.",
        hidden=True,
    ),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if install_daemon:
        warnings.warn(
            "`oc gateway --install-daemon` is deprecated; use "
            "`oc gateway install` instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _install_service(profile=daemon_profile, system=False)
        raise typer.Exit(0)
    _run_foreground()


# ── run / setup ────────────────────────────────────────────────────────────


@gateway_app.command("run", help="Run gateway in foreground.")
def cmd_run() -> None:
    _run_foreground()


@gateway_app.command("setup", help="Interactive wizard scoped to messaging platforms.")
def cmd_setup() -> None:
    """Run the section-driven wizard with sections filtered to messaging."""
    try:
        from opencomputer.cli_setup.wizard import run_setup as run_setup_new

        run_setup_new(non_interactive=False)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]wizard failed:[/red] {exc}")
        raise typer.Exit(1)


# ── Service lifecycle (install/uninstall/start/stop/restart) ───────────────


def _install_service(*, profile: str, system: bool) -> None:
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    result = backend.install(profile=profile, extra_args="gateway")
    typer.echo(f"Installed {result.backend} service at {result.config_path}")
    for note in result.notes:
        typer.echo(f"  note: {note}")


def _uninstall_service(*, profile: str, system: bool) -> None:
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    if hasattr(backend, "uninstall"):
        # ``ServiceBackend.uninstall`` is profile-aware (mirrors
        # ``install``): pass the chosen ``profile`` so the backend
        # removes THAT profile's service unit, not the default one.
        result = backend.uninstall(profile=profile)
        typer.echo(f"Uninstalled {result.backend} service")
        for note in result.notes:
            typer.echo(f"  note: {note}")
    else:
        typer.echo("uninstall not supported by current backend")


@gateway_app.command("install", help="Install gateway as a user/system service.")
def cmd_install(
    system: bool = typer.Option(
        False, "--system", help="Linux: install boot-time system service."
    ),
    profile: str = typer.Option(
        "default", "--profile", help="Profile name (multi-install support)."
    ),
) -> None:
    _install_service(profile=profile, system=system)


@gateway_app.command("uninstall", help="Remove the gateway service.")
def cmd_uninstall(
    system: bool = typer.Option(False, "--system"),
    profile: str = typer.Option("default", "--profile"),
) -> None:
    _uninstall_service(profile=profile, system=system)


@gateway_app.command("start", help="Start the gateway service.")
def cmd_start(system: bool = typer.Option(False, "--system")) -> None:
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    ok = backend.start()
    typer.echo("started" if ok else "start failed")
    raise typer.Exit(0 if ok else 1)


@gateway_app.command("stop", help="Stop the gateway service.")
def cmd_stop(system: bool = typer.Option(False, "--system")) -> None:
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    ok = backend.stop()
    typer.echo("stopped" if ok else "stop failed")
    raise typer.Exit(0 if ok else 1)


@gateway_app.command("restart", help="Restart with optional drain timeout.")
def cmd_restart(
    drain_timeout: int = typer.Option(
        30,
        "--drain-timeout",
        help="Seconds to wait for in-flight messages before stopping.",
    ),
    system: bool = typer.Option(False, "--system"),
) -> None:
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    # Drain phase — best-effort: writes a flag file the daemon polls.
    try:
        flag = _profile_home() / "gateway" / "drain.flag"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(str(drain_timeout), encoding="utf-8")
        typer.echo(f"draining (≤{drain_timeout}s)…")
    except OSError:
        pass
    ok_stop = backend.stop()
    if not ok_stop:
        typer.echo("warning: stop failed; attempting start anyway")
    ok_start = backend.start()
    typer.echo("restarted" if ok_start else "restart failed")
    raise typer.Exit(0 if ok_start else 1)


@gateway_app.command("status", help="Show service + manual-PID state.")
def cmd_status(profile: str = typer.Option("default", "--profile")) -> None:
    """Render a Rich panel describing the gateway runtime state."""
    try:
        from opencomputer.cli_gateway_status import (
            get_gateway_runtime_snapshot,
        )

        snap = get_gateway_runtime_snapshot(profile=profile)
        _render_snapshot(snap)
    except ImportError:
        # Fallback: thin status from the existing service backend.
        from opencomputer.service.factory import get_backend

        backend = get_backend()
        s = backend.status()
        console.print(
            Panel(
                f"installed: {s.file_present}\n"
                f"enabled: {s.enabled}\n"
                f"running: {s.running} (pid={s.pid})",
                title="OpenComputer gateway",
            )
        )


def _render_snapshot(snap) -> None:
    table = Table(box=None, show_header=False)
    table.add_column(style="cyan", no_wrap=True)
    table.add_column(style="white")
    table.add_row("Manager", snap.manager)
    table.add_row(
        "Service",
        ("installed" if snap.service_installed else "not installed")
        + (" (active)" if snap.service_running else ""),
    )
    if snap.main_pid:
        table.add_row("MainPID", str(snap.main_pid))
    if snap.gateway_pids:
        table.add_row(
            "Manual PIDs", ", ".join(str(p) for p in snap.gateway_pids)
        )
    if snap.foreign_home_pids:
        rows = [f"{p.pid} ({p.home})" for p in snap.foreign_home_pids]
        table.add_row("Foreign homes", "; ".join(rows))
    panel_style = "yellow" if snap.has_process_service_mismatch else "cyan"
    console.print(Panel(table, title="OpenComputer gateway", border_style=panel_style))
    if snap.has_process_service_mismatch:
        console.print(
            "[yellow]warning:[/yellow] service unit installed + running but "
            "service status says inactive — try `oc gateway stop && oc gateway start`."
        )


@gateway_app.command("logs", help="Tail gateway logs (delegates to service backend).")
def cmd_logs(
    n: int = typer.Option(100, "-n", "--lines"),
    follow: bool = typer.Option(False, "-f", "--follow"),
    system: bool = typer.Option(False, "--system"),
) -> None:
    from opencomputer.service.factory import get_backend

    backend = get_backend()
    for line in backend.follow_logs(lines=n, follow=follow):
        typer.echo(line)


# ── diagnose — gateway-vs-CLI intelligence-parity telemetry (M1) ────────────


def _parse_since(spec: str | None) -> float | None:
    """Parse ``7d`` / ``12h`` / ``30m`` / ``90s`` / ``120`` → unix-ts floor.

    A bare number is seconds. Returns ``None`` for an empty spec.
    Raises ``typer.BadParameter`` on a malformed value.
    """
    if not spec:
        return None
    import time as _t

    s = spec.strip().lower()
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    mult = 1
    if s and s[-1] in units:
        mult = units[s[-1]]
        s = s[:-1]
    try:
        n = float(s)
    except ValueError as exc:
        raise typer.BadParameter(
            f"bad --since value {spec!r}; use e.g. 7d, 12h, 90m, 3600"
        ) from exc
    if n < 0:
        raise typer.BadParameter("--since must not be negative")
    return _t.time() - n * mult


@gateway_app.command(
    "diagnose",
    help="Show which CLI-vs-gateway parity mechanisms fired on recent turns.",
)
def cmd_diagnose(
    session: str | None = typer.Option(
        None, "--session", help="Filter to one session id."
    ),
    rollup: bool = typer.Option(
        False, "--rollup", help="Aggregate fire-rate + priority per mechanism."
    ),
    since: str | None = typer.Option(
        None, "--since", help="Time window, e.g. 7d / 12h / 90m / 3600."
    ),
    limit: int = typer.Option(
        20, "--limit", help="Max turns to show in per-turn mode."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of a table."
    ),
) -> None:
    """Diagnose the gateway-vs-CLI intelligence gap from telemetry.

    Every gateway turn records which of the 10 parity-affecting
    mechanisms fired into ``audit.db`` (schema v21). This command reads
    that ``gateway_parity_log`` table two ways:

    * default — a per-turn table of the last N turns;
    * ``--rollup`` — fire-rate + priority per mechanism, the input the
      M2 milestone uses to lock the top-3 mechanisms to fix.

    See ``docs/gateway/intelligence-parity.md``.
    """
    import time as _t

    from opencomputer.gateway.parity_probe import (
        mechanism_label,
        query_parity_log,
        rollup_parity_log,
    )

    audit_db = _profile_home() / "audit.db"
    since_ts = _parse_since(since)

    if rollup:
        data = rollup_parity_log(audit_db, since=since_ts)
        total_turns = max((r["turns"] for r in data), default=0)
        if as_json:
            typer.echo(json.dumps({"rollup": data, "total_turns": total_turns}))
            return
        if total_turns == 0:
            console.print(
                "[yellow]No gateway parity telemetry yet.[/yellow] "
                "The table fills as messages flow through the gateway.\n"
                f"[dim]audit.db: {audit_db}[/dim]"
            )
            return
        table = Table(
            title=f"Gateway parity mechanisms — rollup ({total_turns} turns)"
        )
        table.add_column("Mechanism", style="cyan", no_wrap=False)
        table.add_column("Sev", justify="right")
        table.add_column("Fired", justify="right")
        table.add_column("Fire-rate", justify="right")
        table.add_column("Priority", justify="right", style="bold")
        for i, r in enumerate(data):
            mark = "→ " if i < 3 and r["priority_score"] > 0 else "  "
            table.add_row(
                mark + r["label"],
                str(r["severity"]),
                f"{r['fired_count']}/{r['turns']}",
                f"{r['fire_rate'] * 100:.0f}%",
                f"{r['priority_score']:.2f}",
            )
        console.print(table)
        console.print(
            "[dim]Priority = fire-rate × severity. The top-3 (→) are the "
            "candidates the M2/M3 work fixes first.[/dim]"
        )
        return

    # ── per-turn mode ──
    rows = query_parity_log(
        audit_db, session_id=session, since=since_ts, limit=limit * 10
    )
    if as_json:
        typer.echo(json.dumps({"rows": rows}))
        return
    if not rows:
        console.print(
            "[yellow]No gateway parity telemetry yet.[/yellow] "
            "Turns appear here once the gateway has handled messages.\n"
            f"[dim]audit.db: {audit_db}[/dim]"
        )
        return

    # Group flat rows back into turns, keyed by (session_id, turn_id),
    # newest-first. query_parity_log already returns id-descending, so
    # first-seen order is newest-first.
    turns: dict[tuple[str, int], dict] = {}
    for r in rows:
        key = (r["session_id"], r["turn_id"])
        t = turns.setdefault(
            key,
            {
                "session_id": r["session_id"],
                "turn_id": r["turn_id"],
                "platform": r["platform"],
                "ts": r["ts"],
                "fired": [],
            },
        )
        if r["fired"]:
            t["fired"].append(r["mechanism_id"])
    ordered = list(turns.values())[:limit]

    table = Table(title="Gateway parity diagnostics — recent turns")
    table.add_column("When", style="dim")
    table.add_column("Platform", style="cyan")
    table.add_column("Session", style="dim")
    table.add_column("Turn", justify="right")
    table.add_column("Mechanisms fired")
    for t in ordered:
        when = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(t["ts"]))
        turn_label = str(t["turn_id"])
        if t["fired"]:
            fired = ", ".join(mechanism_label(m) for m in t["fired"])
        else:
            fired = "[green]none — full parity[/green]"
        table.add_row(
            when,
            t["platform"],
            (t["session_id"] or "")[:8],
            turn_label,
            fired,
        )
    console.print(table)
    console.print(
        f"[dim]{len(ordered)} turn(s). Run with --rollup for fire-rate "
        f"aggregates, --session <id> to filter.[/dim]"
    )


# ── /sethome ────────────────────────────────────────────────────────────────


@gateway_app.command("sethome", help="Set or list home channels per platform.")
def cmd_sethome(
    platform: str | None = typer.Argument(None),
    chat_id: str | None = typer.Argument(None),
    thread: str | None = typer.Option(None, "--thread"),
    list_homes: bool = typer.Option(False, "--list"),
    clear: str | None = typer.Option(None, "--clear"),
) -> None:
    home = _profile_home()
    home_path = home / "gateway" / "home_channels.json"
    home_path.parent.mkdir(parents=True, exist_ok=True)

    if home_path.exists():
        try:
            mapping = json.loads(home_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            mapping = {}
    else:
        mapping = {}

    if list_homes:
        if not mapping:
            typer.echo("no home channels set")
            return
        for plat, val in mapping.items():
            typer.echo(f"{plat}: {val}")
        return
    if clear:
        if clear in mapping:
            del mapping[clear]
            home_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
            typer.echo(f"cleared home for {clear}")
        else:
            typer.echo(f"no home set for {clear}")
        return
    if platform is None or chat_id is None:
        typer.echo(
            "usage: oc gateway sethome <platform> <chat_id> [--thread <id>]"
            "       oc gateway sethome --list"
            "       oc gateway sethome --clear <platform>"
        )
        raise typer.Exit(1)
    val = chat_id if not thread else f"{chat_id}:{thread}"
    mapping[platform] = val
    home_path.write_text(json.dumps(mapping, indent=2), encoding="utf-8")
    typer.echo(f"home set: {platform} → {val}")


# ── Pairing subgroup ────────────────────────────────────────────────────────


def _pairing_store():
    from opencomputer.channels.pairing_codes import PairingCodeStore

    return PairingCodeStore(_profile_home())


@pairing_app.command("list", help="List pending + approved pairings.")
def pairing_list(
    all_: bool = typer.Option(False, "--all", help="Include approved users."),
) -> None:
    store = _pairing_store()
    pending = store.list_pending()
    if pending:
        table = Table(title="Pending pairing requests")
        for col in ("Platform", "Code", "User ID", "Name", "Age (min)"):
            table.add_column(col)
        for row in pending:
            table.add_row(
                row["platform"],
                row["code"],
                row["user_id"],
                row.get("user_name", ""),
                str(row["age_minutes"]),
            )
        console.print(table)
    else:
        console.print("[dim]no pending pairing requests[/dim]")
    if all_:
        approved = store.list_approved()
        if approved:
            table = Table(title="Approved users")
            for col in ("Platform", "User ID", "Name"):
                table.add_column(col)
            for row in approved:
                table.add_row(
                    row["platform"], row["user_id"], row.get("user_name", "")
                )
            console.print(table)
        else:
            console.print("[dim]no approved users[/dim]")


@pairing_app.command("approve", help="Approve a pending pairing code.")
def pairing_approve(platform: str, code: str) -> None:
    store = _pairing_store()
    result = store.approve_code(platform, code)
    if result is None:
        console.print(
            f"[red]✗[/red] code {code!r} not pending for {platform!r} "
            f"(may have expired)"
        )
        raise typer.Exit(1)
    user = result["user_name"] or result["user_id"]
    console.print(
        f"[green]✓[/green] approved {user} on {platform} (user_id={result['user_id']})"
    )


@pairing_app.command(
    "approve-deeplink", help="Approve a code parsed from a Telegram deep-link URL."
)
def pairing_approve_deeplink(url: str) -> None:
    """Parse a ``https://t.me/<bot>?start=approve_<code>`` URL and approve."""
    import re

    m = re.search(r"\?start=approve_([A-Z0-9]{8})$", url)
    if not m:
        console.print(f"[red]✗[/red] not a recognised pairing deep-link: {url}")
        raise typer.Exit(1)
    code = m.group(1)
    pairing_approve("telegram", code)


@pairing_app.command("revoke", help="Revoke a previously approved user.")
def pairing_revoke(platform: str, user_id: str) -> None:
    store = _pairing_store()
    if store.revoke(platform, user_id):
        console.print(
            f"[green]✓[/green] revoked {user_id} on {platform}"
        )
    else:
        console.print(f"[yellow]·[/yellow] {user_id} was not approved on {platform}")


@pairing_app.command(
    "regen",
    help="Force-mint a fresh pairing code (bypasses rate limit).",
)
def pairing_regen(platform: str, user_id: str) -> None:
    store = _pairing_store()
    code = store.regenerate_code(platform, user_id)
    if code is None:
        console.print(
            "[red]✗[/red] could not regenerate (locked-out or pending cap reached)"
        )
        raise typer.Exit(1)
    console.print(
        f"[green]✓[/green] new code for {user_id} on {platform}: [bold]{code}[/bold]"
    )


@pairing_app.command(
    "clear-pending", help="Drop all pending requests (for one platform or all)."
)
def pairing_clear(
    platform: str | None = typer.Argument(None),
) -> None:
    store = _pairing_store()
    count = store.clear_pending(platform)
    target = f"on {platform}" if platform else "across all platforms"
    console.print(f"[green]✓[/green] cleared {count} pending {target}")


gateway_app.add_typer(pairing_app, name="pairing")


# ── Hermes-CLI compat: top-level `oc pairing` ──────────────────────────────


top_pairing_app = typer.Typer(
    name="pairing",
    help="Alias of `oc gateway pairing` (Hermes-CLI compat).",
    no_args_is_help=True,
)
# Explicitly re-bind each subcommand under the top-level alias. This
# imports the registered_commands dict and re-registers; cleaner than
# re-decorating each function which would duplicate signatures.


@top_pairing_app.command("list")
def _top_list(all_: bool = typer.Option(False, "--all")) -> None:
    pairing_list(all_=all_)


@top_pairing_app.command("approve")
def _top_approve(platform: str, code: str) -> None:
    pairing_approve(platform, code)


@top_pairing_app.command("approve-deeplink")
def _top_approve_dl(url: str) -> None:
    pairing_approve_deeplink(url)


@top_pairing_app.command("revoke")
def _top_revoke(platform: str, user_id: str) -> None:
    pairing_revoke(platform, user_id)


@top_pairing_app.command("regen")
def _top_regen(platform: str, user_id: str) -> None:
    pairing_regen(platform, user_id)


@top_pairing_app.command("clear-pending")
def _top_clear(platform: str | None = typer.Argument(None)) -> None:
    pairing_clear(platform)


__all__ = [
    "gateway_app",
    "pairing_app",
    "top_pairing_app",
]
