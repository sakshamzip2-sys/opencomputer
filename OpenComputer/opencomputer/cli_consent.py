"""F1: `opencomputer consent` CLI subcommand group.

Subcommands:
    opencomputer consent list                              — show active grants
    opencomputer consent grant <cap> [opts]                — create a grant
    opencomputer consent revoke <cap> [--scope X]          — remove a grant
    opencomputer consent history [<cap>]                   — dump audit log
    opencomputer consent verify-chain                      — HMAC chain integrity check
    opencomputer consent export-chain-head --out <file>    — backup head HMAC
    opencomputer consent import-chain-head --from <file>   — verify backup still matches
    opencomputer consent bypass [--status]                 — show emergency-bypass state
    opencomputer consent suggest-promotions [--auto-accept] — list promotion candidates

Grants, counters and audit log all live in the ACTIVE PROFILE's
SQLite DB at `_home() / "sessions.db"`.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.config import _home
from opencomputer.agent.consent import (
    AuditEvent,
    AuditLogger,
    BypassManager,
    ConsentStore,
    KeyringAdapter,
)
from opencomputer.agent.state import apply_migrations
from plugin_sdk import ConsentGrant, ConsentTier

consent_app = typer.Typer(
    name="consent",
    help="Manage consent grants and the immutable audit log.",
    no_args_is_help=True,
)


# ─── Helpers ──────────────────────────────────────────────────────────


def _open_consent_db() -> tuple[sqlite3.Connection, ConsentStore, AuditLogger]:
    """Open the active profile's session DB + return (conn, store, logger).

    Creates the DB (with migrations) if missing, and provisions the HMAC
    chain key via keyring (with file fallback) on first use.
    """
    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "sessions.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    apply_migrations(conn)

    kr = KeyringAdapter(service="opencomputer-consent", fallback_dir=home)
    key_hex = kr.get("hmac-chain")
    if key_hex is None:
        key_bytes = os.urandom(32)
        kr.set("hmac-chain", key_bytes.hex())
    else:
        key_bytes = bytes.fromhex(key_hex)

    store = ConsentStore(conn)
    logger = AuditLogger(conn, hmac_key=key_bytes)
    return conn, store, logger


def _parse_expiry(spec: str | None, default_days: int = 30) -> float | None:
    """Parse `--expires` value.

    None          → default 30d from now
    'never'       → None (never expires)
    'session'     → 1h from now (session-only placeholder)
    '<N>d'        → N days from now
    '<N>h'        → N hours from now
    """
    now = time.time()
    if spec is None:
        return now + default_days * 86400
    s = spec.strip().lower()
    if s == "never":
        return None
    if s == "session":
        return now + 3600
    if s.endswith("d"):
        return now + int(s[:-1]) * 86400
    if s.endswith("h"):
        return now + int(s[:-1]) * 3600
    raise typer.BadParameter(f"cannot parse --expires {spec!r}")


def _fmt_tier(t: int) -> str:
    try:
        return ConsentTier(t).name
    except ValueError:
        return f"<tier={t}>"


# ─── Commands ─────────────────────────────────────────────────────────


@consent_app.command("list")
def consent_list() -> None:
    """List active (non-expired) consent grants."""
    _, store, _ = _open_consent_db()
    grants = store.list_active()
    if not grants:
        typer.echo("No active grants.")
        return
    for g in grants:
        if g.expires_at is None:
            exp = "never"
        else:
            exp = (
                "expires "
                + time.strftime("%Y-%m-%d %H:%M", time.localtime(g.expires_at))
            )
        scope = g.scope_filter or "*"
        typer.echo(f"  [{g.tier.name}] {g.capability_id} scope={scope} ({exp})")


@consent_app.command("grant")
def consent_grant(
    capability_id: str,
    scope: Annotated[
        str | None, typer.Option("--scope", help="Scope path or prefix.")
    ] = None,
    tier: Annotated[
        int, typer.Option("--tier", help="Consent tier 0-3.")
    ] = 1,
    expires: Annotated[
        str | None,
        typer.Option("--expires", help="never / session / 7d / 12h / 30d (default: 30d)"),
    ] = None,
) -> None:
    """Grant a capability. Default tier = 1 (EXPLICIT), default expiry 30d."""
    _, store, logger = _open_consent_db()
    expires_at = _parse_expiry(expires)
    g = ConsentGrant(
        capability_id=capability_id, tier=ConsentTier(tier),
        scope_filter=scope, granted_at=time.time(),
        expires_at=expires_at, granted_by="user",
    )
    store.upsert(g)
    logger.append(AuditEvent(
        session_id=None, actor="user", action="grant",
        capability_id=capability_id, tier=tier, scope=scope,
        decision="allow", reason="CLI grant",
    ))
    typer.echo(
        f"Granted {capability_id} tier={ConsentTier(tier).name} "
        f"scope={scope or '*'}"
    )


@consent_app.command("revoke")
def consent_revoke(
    capability_id: str,
    scope: Annotated[
        str | None, typer.Option("--scope", help="Scope path or prefix.")
    ] = None,
) -> None:
    """Revoke a previously granted capability."""
    _, store, logger = _open_consent_db()
    store.revoke(capability_id, scope)
    logger.append(AuditEvent(
        session_id=None, actor="user", action="revoke",
        capability_id=capability_id, tier=0, scope=scope,
        decision="n/a", reason="CLI revoke",
    ))
    typer.echo(f"Revoked {capability_id} scope={scope or '*'}")


@consent_app.command("history")
def consent_history(
    capability_id: Annotated[
        str | None,
        typer.Argument(help="Filter to this capability. Omit for full log."),
    ] = None,
) -> None:
    """Show audit log entries."""
    conn, _, _ = _open_consent_db()
    if capability_id:
        rows = conn.execute(
            "SELECT timestamp, actor, action, decision, reason "
            "FROM audit_log WHERE capability_id=? ORDER BY id",
            (capability_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp, actor, action, decision, reason "
            "FROM audit_log ORDER BY id"
        ).fetchall()
    if not rows:
        typer.echo("(no audit entries)")
        return
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0]))
        typer.echo(f"{when}  {r[1]:<8}  {r[2]:<12}  {r[3]:<6}  {r[4]}")


@consent_app.command("verify-chain")
def consent_verify_chain() -> None:
    """Verify HMAC chain integrity over the audit log."""
    _, _, logger = _open_consent_db()
    ok = logger.verify_chain()
    if ok:
        typer.echo("Chain verified: ok")
    else:
        typer.echo("CHAIN BROKEN — audit log has been tampered with.", err=True)
        raise typer.Exit(1)


@consent_app.command("export-chain-head")
def consent_export_chain_head(
    out: Annotated[str, typer.Option("--out", help="Path to write backup.")],
) -> None:
    """Export current HMAC chain head to a backup file (for post-keyring-wipe recovery)."""
    _, _, logger = _open_consent_db()
    logger.export_chain_head(Path(out))
    typer.echo(f"Exported chain head to {out}")


@consent_app.command("import-chain-head")
def consent_import_chain_head(
    from_: Annotated[
        str,
        typer.Option("--from", help="Path to previously-exported backup."),
    ],
) -> None:
    """Verify a backed-up chain head still matches the current DB."""
    _, _, logger = _open_consent_db()
    try:
        logger.import_chain_head(Path(from_))
        typer.echo("Chain head verified against DB.")
    except ValueError as e:
        typer.echo(f"Mismatch: {e}", err=True)
        raise typer.Exit(1) from e


@consent_app.command("bypass")
def consent_bypass(
    status: Annotated[
        bool, typer.Option("--status", help="Show current bypass state.")
    ] = False,
) -> None:
    """Show or explain the emergency consent-bypass flag."""
    if status:
        active = BypassManager.is_active()
        state = "active" if active else "inactive"
        typer.echo(f"Consent bypass is {state}.")
        if active:
            typer.echo(BypassManager.banner())
        return
    typer.echo(
        "To enable: `export OPENCOMPUTER_CONSENT_BYPASS=1` in your shell.\n"
        "This should only be used to unbrick a broken gate. Every action "
        "is heavily audit-logged while bypass is active."
    )


# ─── 2.B.1 — Progressive-tier auto-promotion CLI ─────────────────────


@consent_app.command("suggest-promotions")
def consent_suggest_promotions(
    auto_accept: Annotated[
        bool,
        typer.Option(
            "--auto-accept",
            help=(
                "Apply each suggested promotion immediately instead of "
                "printing instructions."
            ),
        ),
    ] = False,
) -> None:
    """List Tier-2 grants eligible for promotion to Tier-1 (>=10 clean runs).

    Reads the per-(capability, scope) clean_run_count from
    ``consent_counters`` (maintained by ProgressivePromoter) and shows
    only those whose currently-stored grant is still EXPLICIT (Tier 2):
    a counter without a matching EXPLICIT grant has nothing to promote.

    With ``--auto-accept`` each candidate is upgraded in place to
    IMPLICIT (Tier 1) and an audit row is appended with
    ``actor=progressive_auto_promoter``, ``action=promote``,
    ``reason=clean_run_count>=10``.
    """
    conn, store, logger = _open_consent_db()
    threshold = 10
    rows = conn.execute(
        "SELECT capability_id, scope_filter, clean_run_count "
        "FROM consent_counters WHERE clean_run_count >= ? "
        "ORDER BY capability_id, scope_filter",
        (threshold,),
    ).fetchall()

    eligible: list[tuple[str, str | None, int, ConsentGrant]] = []
    for cap_id, scope, count in rows:
        grant = store.get(cap_id, scope)
        if grant is None:
            # No active grant (revoked or expired) — nothing to promote.
            continue
        if grant.tier != ConsentTier.EXPLICIT:
            # Already IMPLICIT or higher-trust — skip.
            continue
        eligible.append((cap_id, scope, int(count), grant))

    if not eligible:
        typer.echo("No promotion candidates.")
        return

    if auto_accept:
        for cap_id, scope, count, grant in eligible:
            promoted = ConsentGrant(
                capability_id=cap_id,
                tier=ConsentTier.IMPLICIT,
                scope_filter=scope,
                granted_at=time.time(),
                expires_at=grant.expires_at,
                granted_by="promoted",
            )
            store.upsert(promoted)
            logger.append(AuditEvent(
                session_id=None,
                actor="progressive_auto_promoter",
                action="promote",
                capability_id=cap_id,
                tier=int(ConsentTier.IMPLICIT),
                scope=scope,
                decision="allow",
                reason="clean_run_count>=10",
            ))
            typer.echo(
                f"Promoted {cap_id} scope={scope or '*'} "
                f"(clean_run_count={count}) → IMPLICIT"
            )
        return

    console = Console()
    table = Table(title="Tier-2 → Tier-1 promotion candidates")
    table.add_column("capability_id", style="cyan")
    table.add_column("scope", style="green")
    table.add_column("clean_run_count", justify="right")
    table.add_column("current tier")
    table.add_column("suggested tier")
    for cap_id, scope, count, _grant in eligible:
        table.add_row(
            cap_id,
            scope if scope else "(global)",
            str(count),
            ConsentTier.EXPLICIT.name,
            ConsentTier.IMPLICIT.name,
        )
    console.print(table)
    typer.echo(
        "To accept a suggestion, run: "
        "opencomputer consent grant <cap> --tier 1 [--scope <path>]"
    )
