"""``oc bindings`` Typer subgroup — manage ~/.opencomputer/bindings.yaml.

Subcommands: list / show / add / remove / set-default / test.

All writes are flock'd via ``filelock`` so concurrent CLI invocations
don't lose updates. Closes the latent profile.yaml flock tech debt
at the same time — pattern reused.

Path resolution
---------------
Uses :func:`opencomputer.agent.config._home` so ``OPENCOMPUTER_HOME``
env var is honoured. ``bindings.yaml`` lives at the OpenComputer
root (it is the routing entry point — chooses the per-message
profile — so it itself sits ABOVE per-profile state).
"""

from __future__ import annotations

from pathlib import Path

import typer
import yaml
from filelock import FileLock
from rich.console import Console
from rich.table import Table

from opencomputer.agent.bindings_config import (
    Binding,
    BindingMatch,
    BindingsConfig,
    load_bindings,
)
from opencomputer.agent.config import _home

app = typer.Typer(help="Manage gateway routing rules (bindings.yaml).")
console = Console()


def _bindings_path() -> Path:
    """Resolve the bindings.yaml path.

    Uses ``_home()`` so ``OPENCOMPUTER_HOME`` env var (and tests'
    monkeypatch of it) work without further plumbing.
    """
    return _home() / "bindings.yaml"


def _save(cfg: BindingsConfig) -> None:
    """Write ``cfg`` to bindings.yaml under a filelock.

    The lock is named ``<path>.lock``; concurrent CLI invocations
    serialize their read-modify-write pass through this lock.
    """
    path = _bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock", timeout=10)
    with lock:
        # Re-read inside the lock so concurrent adds compose rather
        # than overwrite. Callers that already hold a snapshot pass
        # ``cfg`` containing their additions; we rebase those onto
        # the on-disk state by detecting the diff.
        # Simpler & correct: callers pass the FULL config they want
        # written; the lock guarantees one writer at a time.
        data = {
            "default_profile": cfg.default_profile,
            "bindings": [
                {
                    "match": {
                        k: v
                        for k, v in {
                            "platform": b.match.platform,
                            "chat_id": b.match.chat_id,
                            "group_id": b.match.group_id,
                            "peer_id": b.match.peer_id,
                            "account_id": b.match.account_id,
                        }.items()
                        if v is not None
                    },
                    "profile": b.profile,
                    "priority": b.priority,
                }
                for b in cfg.bindings
            ],
        }
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _mutate(fn) -> None:
    """Run ``fn(current_cfg) -> new_cfg`` while holding the file lock.

    Atomic read-modify-write so ``oc bindings add`` racing with
    another ``oc bindings add`` doesn't lose either append.
    """
    path = _bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(path) + ".lock", timeout=10)
    with lock:
        cfg = load_bindings(path)
        new_cfg = fn(cfg)
        data = {
            "default_profile": new_cfg.default_profile,
            "bindings": [
                {
                    "match": {
                        k: v
                        for k, v in {
                            "platform": b.match.platform,
                            "chat_id": b.match.chat_id,
                            "group_id": b.match.group_id,
                            "peer_id": b.match.peer_id,
                            "account_id": b.match.account_id,
                        }.items()
                        if v is not None
                    },
                    "profile": b.profile,
                    "priority": b.priority,
                }
                for b in new_cfg.bindings
            ],
        }
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


@app.command("list")
def list_cmd() -> None:
    """List all bindings."""
    cfg = load_bindings(_bindings_path())
    if not cfg.bindings:
        console.print("[dim]no bindings configured (default-only routing)[/dim]")
        console.print(f"[dim]default profile: {cfg.default_profile}[/dim]")
        return
    table = Table(title=f"bindings (default → {cfg.default_profile})")
    table.add_column("#")
    table.add_column("match")
    table.add_column("profile")
    table.add_column("priority")
    for i, b in enumerate(cfg.bindings):
        match_str = ", ".join(
            f"{k}={v}"
            for k, v in {
                "platform": b.match.platform,
                "chat_id": b.match.chat_id,
                "group_id": b.match.group_id,
                "peer_id": b.match.peer_id,
                "account_id": b.match.account_id,
            }.items()
            if v is not None
        )
        table.add_row(str(i), match_str or "<catch-all>", b.profile, str(b.priority))
    console.print(table)


@app.command("show")
def show_cmd() -> None:
    """Show the parsed bindings.yaml content + path + binding count."""
    cfg = load_bindings(_bindings_path())
    console.print(f"[bold]default profile:[/bold] {cfg.default_profile}")
    console.print(f"[bold]bindings file:[/bold] {_bindings_path()}")
    console.print(f"[bold]binding count:[/bold] {len(cfg.bindings)}")


@app.command("add")
def add_cmd(
    profile: str = typer.Argument(..., help="profile_id to route matched events to"),
    platform: str | None = typer.Option(
        None,
        "--platform",
        help="match platform (telegram, discord, slack, ...)",
    ),
    chat_id: str | None = typer.Option(None, "--chat-id"),
    group_id: str | None = typer.Option(None, "--group-id"),
    peer_id: str | None = typer.Option(None, "--peer-id"),
    account_id: str | None = typer.Option(None, "--account-id"),
    priority: int = typer.Option(0, "--priority"),
) -> None:
    """Add a binding."""
    new = Binding(
        match=BindingMatch(
            platform=platform,
            chat_id=chat_id,
            group_id=group_id,
            peer_id=peer_id,
            account_id=account_id,
        ),
        profile=profile,
        priority=priority,
    )
    _mutate(
        lambda cfg: BindingsConfig(
            default_profile=cfg.default_profile,
            bindings=cfg.bindings + (new,),
        )
    )
    console.print(f"[green]added[/green]: {profile} (priority={priority})")


@app.command("remove")
def remove_cmd(
    index: int = typer.Argument(..., help="0-based binding index from `oc bindings list`"),
) -> None:
    """Remove a binding by its index."""
    cfg = load_bindings(_bindings_path())
    if not (0 <= index < len(cfg.bindings)):
        console.print(f"[red]invalid index {index}; have {len(cfg.bindings)} binding(s)[/red]")
        raise typer.Exit(1)
    _mutate(
        lambda cfg: BindingsConfig(
            default_profile=cfg.default_profile,
            bindings=tuple(b for i, b in enumerate(cfg.bindings) if i != index),
        )
    )
    console.print(f"[green]removed[/green]: binding #{index}")


@app.command("set-default")
def set_default_cmd(
    profile: str = typer.Argument(..., help="profile_id catching unmatched events"),
) -> None:
    """Change the ``default_profile`` (caught-by when no binding matches)."""
    _mutate(lambda cfg: BindingsConfig(default_profile=profile, bindings=cfg.bindings))
    console.print(f"[green]default profile set to[/green]: {profile}")


@app.command("test")
def test_cmd(
    platform: str = typer.Option(..., "--platform"),
    chat_id: str | None = typer.Option(None, "--chat-id"),
    peer_id: str | None = typer.Option(None, "--peer-id"),
    group_id: str | None = typer.Option(None, "--group-id"),
    account_id: str | None = typer.Option(None, "--account-id"),
) -> None:
    """Show which profile WOULD catch a hypothetical event.

    Pass-2 F12 — useful for checking routing rules without sending
    real messages. Constructs a synthetic ``MessageEvent`` matching
    the supplied flags and runs it through the actual
    ``BindingResolver`` so what you see is what gateway would do.
    """
    from opencomputer.gateway.binding_resolver import BindingResolver
    from plugin_sdk.core import MessageEvent, Platform

    cfg = load_bindings(_bindings_path())
    resolver = BindingResolver(cfg)
    metadata = {
        k: v
        for k, v in {
            "peer_id": peer_id,
            "group_id": group_id,
            "account_id": account_id,
        }.items()
        if v is not None
    }
    # MessageEvent requires user_id/timestamp on this build; use
    # placeholder values — they don't influence resolver decisions.
    base = dict(
        platform=Platform(platform),
        chat_id=chat_id or "",
        text="",
        attachments=[],
        metadata=metadata,
    )
    try:
        fake_event = MessageEvent(**base)
    except TypeError:
        base["user_id"] = "u0"
        base["timestamp"] = 0.0
        fake_event = MessageEvent(**base)
    resolved = resolver.resolve(fake_event)
    console.print(f"resolved profile: [bold]{resolved}[/bold]")


__all__ = ["app"]
