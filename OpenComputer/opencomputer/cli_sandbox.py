"""``opencomputer sandbox`` Typer subapp.

Subcommands::

    opencomputer sandbox status             # which strategies are available
    opencomputer sandbox enable [--scope]   # turn sandboxing on, set the scope
    opencomputer sandbox disable            # turn sandboxing off
    opencomputer sandbox explain            # print the effective scope policy
    opencomputer sandbox explain -- <argv>  # print the wrapped command (dry-run)
    opencomputer sandbox run -- <argv>      # run argv under the active policy

``status`` / ``run`` / ``explain -- <argv>`` are the original Phase-3.E
commands. ``enable`` / ``disable`` and the bare ``explain`` policy
inspector are the Milestone-1 scope-policy surface (Hermes + OpenClaw
parity plan, 2026-05-16): they read and write the ``sandbox:`` block of
the active profile's ``config.yaml``.

Argv is passed AFTER ``--`` so flags meant for the sandboxed command
aren't parsed by Typer. ``argv`` is a list-of-strings — every token
becomes a single argv element (no shell parsing).
"""

from __future__ import annotations

import asyncio
import dataclasses
import platform
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config_store import config_file_path, load_config, save_config
from opencomputer.sandbox.auto import auto_strategy
from opencomputer.sandbox.docker import DockerStrategy
from opencomputer.sandbox.linux import LinuxBwrapStrategy
from opencomputer.sandbox.macos import MacOSSandboxExecStrategy
from opencomputer.sandbox.none_strategy import NoneSandboxStrategy
from opencomputer.sandbox.policy import SandboxScope
from opencomputer.sandbox.runner import run_sandboxed
from plugin_sdk.sandbox import SandboxConfig, SandboxUnavailable

sandbox_app = typer.Typer(
    name="sandbox",
    help="Sandbox strategies + scope policy (Phase 3.E + M1 parity).",
    no_args_is_help=True,
)
console = Console()

#: One-line human description per scope — shown by ``explain`` and the
#: ``enable`` confirmation. Keyed on every :class:`SandboxScope` member.
_SCOPE_DESC: dict[SandboxScope, str] = {
    SandboxScope.NONE: "sandboxing off — tool commands run on the host",
    SandboxScope.TOOL: "one fresh, transient container per tool call",
    SandboxScope.SESSION: "one container per session",
    SandboxScope.AGENT: "one container per agent",
    SandboxScope.SHARED: "one container shared by every sandboxed call",
}


def _all_strategies() -> list[tuple[str, object]]:
    """Return ``(name, instance)`` for every concrete strategy.

    Used by the ``status`` command to render a table of availabilities.
    Ordered to match the auto-selection preference (host-native first,
    then Docker, then ``none``).
    """
    return [
        ("macos_sandbox_exec", MacOSSandboxExecStrategy()),
        ("linux_bwrap", LinuxBwrapStrategy()),
        ("docker", DockerStrategy()),
        ("none", NoneSandboxStrategy()),
    ]


def _enableable_scopes() -> str:
    """``|``-joined scope names ``enable`` accepts (every scope but ``none``)."""
    return "|".join(s.value for s in SandboxScope if s is not SandboxScope.NONE)


@sandbox_app.command("status")
def sandbox_status() -> None:
    """Show which strategies are available and which one ``auto`` picks."""
    table = Table(title=f"Sandbox strategies ({platform.system()})")
    table.add_column("Strategy", style="cyan")
    table.add_column("Available", justify="center")
    for name, s in _all_strategies():
        marker = "[green]yes[/green]" if s.is_available() else "[dim]no[/dim]"
        table.add_row(name, marker)
    console.print(table)

    try:
        picked = auto_strategy()
        console.print(f"[bold]auto[/bold] would pick: [cyan]{picked.name}[/cyan]")
    except SandboxUnavailable as e:
        console.print(f"[yellow]auto unavailable:[/yellow] {e}")


@sandbox_app.command("enable")
def sandbox_enable(
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Container scope: session / agent / shared / tool.",
        ),
    ] = SandboxScope.SESSION.value,
) -> None:
    """Turn sandboxing on and persist the container scope.

    Writes the ``sandbox.scope`` key of the active profile's
    ``config.yaml``. Existing tool allow/deny lists are preserved.
    """
    try:
        new_scope = SandboxScope(scope)
    except ValueError:
        console.print(
            f"[bold red]error:[/bold red] unknown scope {scope!r}; "
            f"valid: {_enableable_scopes()}"
        )
        raise typer.Exit(2) from None
    if new_scope is SandboxScope.NONE:
        console.print(
            "[bold red]error:[/bold red] `enable` cannot set scope `none` — "
            "use [cyan]oc sandbox disable[/cyan] to turn sandboxing off"
        )
        raise typer.Exit(2)

    cfg = load_config()
    updated = dataclasses.replace(cfg.sandbox, scope=new_scope)
    save_config(dataclasses.replace(cfg, sandbox=updated))
    console.print(
        f"[green]sandbox enabled[/green] — scope [cyan]{new_scope.value}[/cyan] "
        f"([dim]{_SCOPE_DESC[new_scope]}[/dim])"
    )
    console.print(f"[dim]written to {config_file_path()}[/dim]")
    console.print("[dim]inspect with: oc sandbox explain[/dim]")


@sandbox_app.command("disable")
def sandbox_disable() -> None:
    """Turn sandboxing off (set scope to ``none``).

    Tool allow/deny lists are preserved, so a later ``enable`` restores them.
    """
    cfg = load_config()
    if not cfg.sandbox.enabled:
        console.print("[dim]sandbox already disabled (scope=none)[/dim]")
        return
    updated = dataclasses.replace(cfg.sandbox, scope=SandboxScope.NONE)
    save_config(dataclasses.replace(cfg, sandbox=updated))
    console.print("[green]sandbox disabled[/green] — tool commands run on the host")
    console.print(f"[dim]written to {config_file_path()}[/dim]")


def _explain_policy() -> None:
    """Print the effective sandbox scope policy (OpenClaw-style inspector)."""
    pol = load_config().sandbox
    table = Table(title="Sandbox policy", show_header=False)
    table.add_column("field", style="cyan", no_wrap=True)
    table.add_column("value")
    table.add_row("scope", f"{pol.scope.value}  [dim]({_SCOPE_DESC[pol.scope]})[/dim]")
    table.add_row("enabled", "[green]yes[/green]" if pol.enabled else "[dim]no[/dim]")
    table.add_row(
        "tools allow",
        ", ".join(pol.tools_allow) if pol.tools_allow else "[dim](all tools)[/dim]",
    )
    table.add_row(
        "tools deny",
        ", ".join(pol.tools_deny) if pol.tools_deny else "[dim](none)[/dim]",
    )
    try:
        picked = auto_strategy().name
    except SandboxUnavailable:
        picked = "[yellow](none available on this host)[/yellow]"
    table.add_row("host backend", picked)
    console.print(table)
    console.print(
        "[dim]config keys:[/dim] sandbox.scope · sandbox.tools.allow · sandbox.tools.deny"
    )
    console.print(f"[dim]policy file:[/dim] {config_file_path()}")
    console.print(
        f"[dim]change with:[/dim] oc sandbox enable --scope <{_enableable_scopes()}>"
        "  ·  oc sandbox disable"
    )


@sandbox_app.command("explain")
def sandbox_explain(
    argv: Annotated[
        list[str] | None,
        typer.Argument(help="Command + args to wrap; omit to show the scope policy."),
    ] = None,
) -> None:
    """Explain the sandbox.

    With no argument, print the effective scope policy (scope, enabled,
    tool allow/deny, host backend, fix-it config keys). With ``-- <argv>``,
    print the wrapped command for that argv without running it.
    """
    if not argv:
        _explain_policy()
        return
    try:
        strategy = auto_strategy()
    except SandboxUnavailable as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        raise typer.Exit(2) from None
    wrapped = strategy.explain(argv, config=SandboxConfig())
    console.print(f"[dim]strategy:[/dim] [cyan]{strategy.name}[/cyan]")
    # One token per line so the output is easy to eyeball + diff.
    for tok in wrapped:
        console.out(tok)


@sandbox_app.command("run")
def sandbox_run(
    argv: Annotated[
        list[str],
        typer.Argument(help="Command + args to run inside the sandbox."),
    ],
) -> None:
    """Run ``argv`` through the auto strategy under the active scope policy.

    Exits with the wrapped command's exit code. The active
    :class:`~opencomputer.sandbox.policy.SandboxPolicy` (from
    ``config.yaml``) selects how the container is keyed.
    """
    if not argv:
        console.print("[bold red]error:[/bold red] no command supplied")
        raise typer.Exit(2)
    policy = load_config().sandbox
    try:
        result = asyncio.run(run_sandboxed(argv, config=SandboxConfig(), policy=policy))
    except SandboxUnavailable as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        raise typer.Exit(2) from None
    if result.stdout:
        # Print raw stdout so binary-y content survives; rely on Rich's
        # default UTF-8 handling for safety.
        console.out(result.stdout, end="")
    if result.stderr:
        console.print(result.stderr, end="", style="red")
    console.print(
        f"\n[dim]strategy={result.strategy_name} scope={policy.scope.value} "
        f"exit={result.exit_code} duration={result.duration_seconds:.2f}s[/dim]"
    )
    raise typer.Exit(result.exit_code if result.exit_code >= 0 else 1)
