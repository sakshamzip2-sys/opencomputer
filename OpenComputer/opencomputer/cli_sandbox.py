"""``opencomputer sandbox`` Typer subapp.

Subcommands::

    opencomputer sandbox status             # which strategies are available
    opencomputer sandbox enable [--scope]   # turn sandboxing on, set the scope
    opencomputer sandbox disable            # turn sandboxing off
    opencomputer sandbox set [opts]         # set backend / scope / fallback
    opencomputer sandbox explain            # print the effective policy
    opencomputer sandbox explain -- <argv>  # print the wrapped command (dry-run)
    opencomputer sandbox run -- <argv>      # run argv under the active policy

``status`` / ``run`` / ``explain -- <argv>`` are the original Phase-3.E
commands. ``enable`` / ``disable`` and the bare ``explain`` policy
inspector are the Milestone-1 scope-policy surface. ``set`` is the
Milestone-2 backend-routing surface (Hermes + OpenClaw parity plan,
2026-05-16, task T2.7): it persists the ``backend`` / ``scope`` /
``fallback`` keys of the active profile's ``config.yaml`` ``sandbox:``
block, and the bare ``explain`` is extended to show what a tool call
resolves to under that config.

Argv is passed AFTER ``--`` so flags meant for the sandboxed command
aren't parsed by Typer. ``argv`` is a list-of-strings — every token
becomes a single argv element (no shell parsing).
"""

from __future__ import annotations

import asyncio
import dataclasses
import platform
import typing
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
from opencomputer.sandbox.policy import (
    SANDBOX_FALLBACK_ERROR,
    SANDBOX_FALLBACK_LOCAL,
    SandboxScope,
)
from opencomputer.sandbox.runner import _named_strategy, run_sandboxed
from plugin_sdk.sandbox import SandboxConfig, SandboxStrategyName, SandboxUnavailable

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


#: Concrete sandbox-backend names ``oc sandbox set --backend`` accepts —
#: the :data:`~plugin_sdk.sandbox.SandboxStrategyName` Literal members
#: minus ``"auto"``. ``"auto"`` is *not* a persistable ``sandbox.backend``
#: value: the M2 resolver treats ``sandbox.backend`` as a single concrete
#: strategy and ``runner._named_strategy`` rejects ``"auto"``. Derived
#: from the Literal so a new backend (a future SDK addition) is accepted
#: by the CLI the moment it joins the contract — no second list to drift.
_VALID_BACKENDS: tuple[str, ...] = tuple(
    name
    for name in typing.get_args(SandboxStrategyName)
    if name != "auto"
)


def _backends_for_help() -> str:
    """``|``-joined concrete backend names, for ``--backend`` help text."""
    return "|".join(_VALID_BACKENDS)


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


@sandbox_app.command("set")
def sandbox_set(
    backend: Annotated[
        str | None,
        typer.Option(
            "--backend",
            help=(
                f"Default sandbox backend a tool call routes to: "
                f"{_backends_for_help()}. Persisted as sandbox.backend."
            ),
        ),
    ] = None,
    scope: Annotated[
        str | None,
        typer.Option(
            "--scope",
            help=(
                "Container scope: none / tool / session / agent / shared. "
                "Persisted as sandbox.scope."
            ),
        ),
    ] = None,
    fallback: Annotated[
        str | None,
        typer.Option(
            "--fallback",
            help=(
                "What happens when the backend is unreachable: error "
                "(fail loud — default) or local (run on the host with a "
                "WARNING). Persisted as sandbox.fallback."
            ),
        ),
    ] = None,
) -> None:
    """Set sandbox ``backend`` / ``scope`` / ``fallback`` in ``config.yaml``.

    M2 task T2.7 — the backend-routing config surface. Each flag is
    optional; only the keys you pass are changed, the rest of the
    ``sandbox:`` block (tool allow/deny lists, the keys you omit) is
    preserved. At least one flag must be given. Every value is validated
    at this trust boundary — an unknown ``--backend`` / ``--scope`` /
    ``--fallback`` is rejected with the accepted set, before anything is
    written.

    The active :class:`~opencomputer.sandbox.policy.SandboxPolicy` is
    loaded, the given keys applied, and the result written back via
    ``SandboxPolicy.to_mapping()`` + the profile config's atomic writer
    (``save_config``).
    """
    if backend is None and scope is None and fallback is None:
        console.print(
            "[bold red]error:[/bold red] nothing to set — pass at least "
            "one of [cyan]--backend[/cyan] / [cyan]--scope[/cyan] / "
            "[cyan]--fallback[/cyan]"
        )
        raise typer.Exit(2)

    # --- Validate every value at the trust boundary BEFORE any write. ----
    new_backend: str | None = None
    if backend is not None:
        candidate = backend.strip()
        if candidate not in _VALID_BACKENDS:
            console.print(
                f"[bold red]error:[/bold red] unknown backend "
                f"{backend!r}; valid: {_backends_for_help()}"
            )
            raise typer.Exit(2)
        new_backend = candidate

    new_scope: SandboxScope | None = None
    if scope is not None:
        try:
            new_scope = SandboxScope(scope.strip())
        except ValueError:
            valid = "|".join(s.value for s in SandboxScope)
            console.print(
                f"[bold red]error:[/bold red] unknown scope {scope!r}; "
                f"valid: {valid}"
            )
            raise typer.Exit(2) from None

    new_fallback: str | None = None
    if fallback is not None:
        candidate = fallback.strip().lower()
        if candidate not in (SANDBOX_FALLBACK_ERROR, SANDBOX_FALLBACK_LOCAL):
            console.print(
                f"[bold red]error:[/bold red] unknown fallback "
                f"{fallback!r}; valid: {SANDBOX_FALLBACK_ERROR}|"
                f"{SANDBOX_FALLBACK_LOCAL}"
            )
            raise typer.Exit(2)
        new_fallback = candidate

    # --- Apply only the keys that were passed; preserve the rest. --------
    cfg = load_config()
    pol = cfg.sandbox
    changes: dict[str, object] = {}
    if new_backend is not None:
        changes["backend"] = new_backend
    if new_scope is not None:
        changes["scope"] = new_scope
    if new_fallback is not None:
        changes["fallback"] = new_fallback
    updated = dataclasses.replace(pol, **changes)
    # ``to_mapping`` round-trips ``from_mapping``; ``save_config`` writes
    # the whole config.yaml atomically (its writer is the canonical
    # profile-config atomic writer — no bespoke write here).
    save_config(dataclasses.replace(cfg, sandbox=updated))

    console.print("[green]sandbox config updated[/green]")
    if new_backend is not None:
        console.print(f"  backend  → [cyan]{new_backend}[/cyan]")
    if new_scope is not None:
        console.print(
            f"  scope    → [cyan]{new_scope.value}[/cyan] "
            f"[dim]({_SCOPE_DESC[new_scope]})[/dim]"
        )
    if new_fallback is not None:
        console.print(f"  fallback → [cyan]{new_fallback}[/cyan]")
    console.print(f"[dim]written to {config_file_path()}[/dim]")
    console.print("[dim]inspect with: oc sandbox explain[/dim]")


def _backend_is_available(name: str) -> bool:
    """Return whether the named sandbox backend can run on this host.

    :func:`_named_strategy` raises :class:`~plugin_sdk.SandboxUnavailable`
    both for an unknown name and a known-but-unavailable backend, and only
    *returns* a strategy when that strategy's
    :meth:`~plugin_sdk.SandboxBackend.is_available` is already ``True``.
    So a clean return means "available", and the exception means "cannot
    run here" — the two cases the caller needs.
    """
    try:
        _named_strategy(name)
    except SandboxUnavailable:
        return False
    return True


def _backend_availability_line(name: str) -> str:
    """Render ``<name>  (available|unavailable on this host)`` for ``explain``."""
    if _backend_is_available(name):
        return f"[cyan]{name}[/cyan]  [green](available)[/green]"
    return f"[cyan]{name}[/cyan]  [yellow](unavailable on this host)[/yellow]"


def _explain_policy() -> None:
    """Print the effective sandbox policy + what a tool call resolves to.

    M1 shipped the scope inspector (scope / enabled / tool allow-deny /
    host backend). M2 task T2.7 extends it: the configured ``backend``,
    whether sandboxing is *opted into*, whether that backend is available
    on this host, the ``fallback`` policy, and a one-line summary of what
    an ordinary tool call resolves to under this config.
    """
    pol = load_config().sandbox
    table = Table(title="Sandbox policy", show_header=False)
    table.add_column("field", style="cyan", no_wrap=True)
    table.add_column("value")
    table.add_row("scope", f"{pol.scope.value}  [dim]({_SCOPE_DESC[pol.scope]})[/dim]")
    table.add_row("enabled", "[green]yes[/green]" if pol.enabled else "[dim]no[/dim]")
    # M2 — the configured default backend a tool call routes to.
    if pol.backend is None:
        table.add_row(
            "backend",
            "[dim](unset — sandboxing not opted into; tools run on the host)[/dim]",
        )
    else:
        table.add_row("backend", _backend_availability_line(pol.backend))
    # M2 — the fallback policy when the configured backend is unreachable.
    if pol.fallback == SANDBOX_FALLBACK_LOCAL:
        fallback_desc = "run on the host with a logged WARNING (no containment)"
    else:
        fallback_desc = "fail the call loud — never silently downgrade containment"
    table.add_row("fallback", f"{pol.fallback}  [dim]({fallback_desc})[/dim]")
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
    table.add_row("host backend", f"{picked}  [dim](what `auto` would pick)[/dim]")
    console.print(table)

    # M2 — one-line "what a tool call resolves to" summary. This is the
    # human reading of the per-tool-call resolver's decision for an
    # ordinary tool (one that sets no sandbox preference).
    console.print(_resolution_summary(pol))
    console.print(
        "[dim]config keys:[/dim] sandbox.backend · sandbox.scope · "
        "sandbox.fallback · sandbox.tools.allow · sandbox.tools.deny"
    )
    console.print(f"[dim]policy file:[/dim] {config_file_path()}")
    console.print(
        f"[dim]change with:[/dim] oc sandbox set --backend <{_backends_for_help()}> "
        f"--scope <none|{_enableable_scopes()}> "
        f"--fallback <{SANDBOX_FALLBACK_ERROR}|{SANDBOX_FALLBACK_LOCAL}>"
    )


def _resolution_summary(pol: object) -> str:
    """Return the one-line "what an ordinary tool call resolves to" summary.

    Mirrors the per-tool-call resolver's branch order
    (:func:`opencomputer.sandbox.resolver.resolve_backend`) for an
    ordinary tool: no configured ``backend`` → no sandbox; a configured
    backend that is available → routed through it; configured but
    unreachable → the ``fallback`` policy decides.
    """
    backend = getattr(pol, "backend", None)
    fallback = getattr(pol, "fallback", SANDBOX_FALLBACK_ERROR)
    if backend is None:
        return (
            "[bold]resolves to:[/bold] [dim]no sandbox[/dim] — no "
            "sandbox.backend configured, so an ordinary tool call runs on "
            "the host exactly as it would with sandboxing off."
        )
    if _backend_is_available(backend):
        return (
            f"[bold]resolves to:[/bold] the [cyan]{backend}[/cyan] sandbox "
            "— an ordinary tool call (the Bash tool) routes its command "
            "through it."
        )
    if fallback == SANDBOX_FALLBACK_LOCAL:
        return (
            f"[bold]resolves to:[/bold] [yellow]{backend} is unreachable "
            "here[/yellow] — sandbox.fallback=local, so the call runs on "
            "the HOST with a logged WARNING (no containment)."
        )
    return (
        f"[bold]resolves to:[/bold] [yellow]{backend} is unreachable "
        "here[/yellow] — sandbox.fallback=error, so a tool that requires a "
        "sandbox fails loud; an ordinary tool runs un-sandboxed."
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
