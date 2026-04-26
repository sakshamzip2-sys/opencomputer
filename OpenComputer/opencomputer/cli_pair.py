"""``opencomputer pair <platform>`` — guided channel pairing CLI.

Phase 1.3 of the catch-up plan. See ``opencomputer/channels/pairing.py``
for the registry of supported pairers + write_secret helper.

Subcommand shape:

    opencomputer pair --list             # list supported platforms
    opencomputer pair telegram           # interactive (prompts for token)
    opencomputer pair telegram --token X # non-interactive (CI / script)
    opencomputer pair telegram --skip-live-check
"""

from __future__ import annotations

import sqlite3
import time
from typing import Annotated

import typer

from opencomputer.channels.pairing import PAIRERS, Pairer, write_secret
from plugin_sdk import ConsentGrant, ConsentTier

pair_app = typer.Typer(
    name="pair",
    help="Pair OpenComputer with a chat channel (writes secret + grants consent).",
    no_args_is_help=False,
    invoke_without_command=True,
)


@pair_app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    list_: Annotated[
        bool, typer.Option("--list", help="List supported platforms.")
    ] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if list_:
        typer.echo("Supported platforms (run `opencomputer pair <platform>`):")
        for name, p in PAIRERS.items():
            typer.echo(f"  - {name}  ({p.env_var_name})")
        raise typer.Exit(0)
    typer.echo("Usage: opencomputer pair --list  OR  opencomputer pair <platform>")
    raise typer.Exit(0)


def _do_pair(pairer: Pairer, token: str, skip_live_check: bool) -> None:
    # 1. Format validation (cheap, no IO)
    try:
        pairer.validate_format(token)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    # 2. Optional live check
    live_ok: bool | None = None
    if pairer.live_check and not skip_live_check:
        typer.echo(f"Verifying {pairer.platform} credentials...")
        live_ok = pairer.live_check(token)
        if live_ok:
            typer.echo("✓ live check passed")
        else:
            typer.echo(
                "⚠ live check failed (format is valid; "
                "network/proxy issue or bad token). Continuing anyway."
            )

    # 3. Persist secret (chmod 0600)
    from opencomputer.agent.config import _home

    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    secret_path = write_secret(home, pairer, token)
    typer.echo(f"✓ secret written to {secret_path}")

    # 4. Auto-grant consent capability (EXPLICIT tier, 365d default)
    db_path = home / "sessions.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        from opencomputer.agent.consent.store import ConsentStore
        from opencomputer.agent.state import apply_migrations

        apply_migrations(conn)
        store = ConsentStore(conn)
        grant = ConsentGrant(
            capability_id=pairer.capability_id,
            scope_filter=None,
            tier=ConsentTier.EXPLICIT,
            granted_at=time.time(),
            expires_at=time.time() + 365 * 86400,
            granted_by=f"opencomputer pair {pairer.platform}",
        )
        store.upsert(grant)
    finally:
        conn.close()
    typer.echo(
        f"✓ consent granted: {pairer.capability_id} "
        f"(tier=EXPLICIT, expires in 365d)"
    )

    # 5. User-facing next-step (until Phase 14.F .env loader lands)
    typer.echo("")
    typer.echo("Next step — export the env var so the channel adapter can read it:")
    typer.echo(f"  export {pairer.env_var_name}=$(cat {secret_path})")
    typer.echo("")
    typer.echo("Or add to your shell rc (~/.zshrc, ~/.bashrc):")
    typer.echo(f"  export {pairer.env_var_name}=\"$(cat {secret_path})\"")


def _make_pair_command(platform: str) -> None:
    """Register `opencomputer pair <platform>` for one platform.

    NOTE: we look up the Pairer fresh from PAIRERS[platform] at *call*
    time (not capture-time), so tests that swap the registry entry
    actually take effect.
    """
    initial = PAIRERS[platform]
    help_text = f"Pair OpenComputer with {platform}. {initial.instructions}"

    def _cmd(
        token: Annotated[
            str | None,
            typer.Option(
                "--token", help="Provide token non-interactively (CI / scripts)."
            ),
        ] = None,
        skip_live_check: Annotated[
            bool,
            typer.Option(
                "--skip-live-check",
                help="Skip the API probe (use when offline or behind a proxy).",
            ),
        ] = False,
    ) -> None:
        pairer = PAIRERS[platform]  # late lookup
        if token is None:
            typer.echo(pairer.instructions)
            token = typer.prompt(
                f"Paste {platform} token", hide_input=True, confirmation_prompt=False
            )
        _do_pair(pairer, token, skip_live_check)

    _cmd.__name__ = platform
    _cmd.__doc__ = help_text
    pair_app.command(name=platform, help=help_text)(_cmd)


for _platform in PAIRERS:
    _make_pair_command(_platform)
