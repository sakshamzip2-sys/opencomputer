"""F1 2.B.4: `opencomputer audit` CLI subcommand group.

Subcommands:
    opencomputer audit show [--tool X] [--since 7d] [--decision allow]
                            [--session ID] [--limit N] [--json]
    opencomputer audit verify

The audit log is a tamper-evident, HMAC-chained append-only table. This
CLI is a thin user-facing viewer plus a wrapper around
:meth:`AuditLogger.verify_chain` that surfaces row-count detail.

The store opened here is the same DB that backs ``opencomputer consent``
(``_home() / "sessions.db"``), so grants/audit rows are always consistent
between the two CLIs.
"""
from __future__ import annotations

import json as _json
import re
import time
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.cli_consent import _open_consent_db

audit_app = typer.Typer(
    name="audit",
    help="Inspect the immutable, HMAC-chained consent audit log.",
    no_args_is_help=True,
)


def _parse_since(spec: str) -> float:
    """Parse ``--since`` as ISO-8601 OR relative (``7d``, ``24h``, ``30m``).

    Returns an epoch-seconds float. Relative values count back from now.
    Raises ``typer.BadParameter`` on unparseable input.
    """
    s = spec.strip()
    if not s:
        raise typer.BadParameter("--since cannot be empty")
    # Relative form: <N>(d|h|m)
    m = re.fullmatch(r"(\d+)([dhm])", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        seconds = {"d": 86400, "h": 3600, "m": 60}[unit] * n
        return time.time() - seconds
    # ISO-8601 form. Accept both naive ("2026-04-24T12:00:00") and offset
    # ("2026-04-24T12:00:00+00:00"); also accept the Z shorthand.
    try:
        from datetime import datetime

        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            # Treat naive timestamps as local time so users can paste
            # "2026-04-24" without thinking about TZ. Matches what
            # ``time.strftime`` shows back at them.
            dt = dt.astimezone()
        return dt.timestamp()
    except ValueError as e:
        raise typer.BadParameter(
            f"--since {spec!r}: expected '<N>d|h|m' or ISO-8601 timestamp"
        ) from e


@audit_app.command("show")
def audit_show(
    tool: Annotated[
        str | None,
        typer.Option("--tool", help="Filter by capability_id (regex)."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="Filter to entries newer than this — e.g. 24h, 7d, 30m, or ISO-8601.",
        ),
    ] = None,
    decision: Annotated[
        str | None,
        typer.Option("--decision", help="Filter by decision: allow|deny|n/a."),
    ] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", help="Filter by session_id."),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", help="Max rows to show (default 50)."),
    ] = 50,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable JSON instead of a Rich table.",
        ),
    ] = False,
) -> None:
    """List audit_log entries (newest first) with optional filters."""
    _, _, logger = _open_consent_db()
    since_ts = _parse_since(since) if since else None
    rows = logger.query(
        capability_pattern=tool,
        since=since_ts,
        decision=decision,
        session_id=session,
        limit=limit,
    )

    if as_json:
        typer.echo(_json.dumps(rows, default=str))
        return

    if not rows:
        typer.echo("(no audit entries match)")
        return

    # Wide console so the test runner's narrow virtual terminal doesn't
    # truncate ``capability_id`` columns. Real users in a normal terminal
    # see Rich's auto-fit behavior since this writes via a fresh Console.
    console = Console(width=200, soft_wrap=False, force_terminal=False)
    table = Table(title="Audit log")
    table.add_column("id", justify="right")
    table.add_column("when")
    table.add_column("session")
    table.add_column("actor")
    table.add_column("action")
    table.add_column("capability_id", style="cyan", no_wrap=False)
    table.add_column("tier", justify="right")
    table.add_column("scope", no_wrap=False)
    table.add_column("decision")
    table.add_column("reason", no_wrap=False)
    for r in rows:
        when = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(float(r["timestamp"])),
        )
        table.add_row(
            str(r["id"]),
            when,
            (r["session_id"] or "")[:8],
            r["actor"],
            r["action"],
            r["capability_id"],
            str(r["tier"]),
            r["scope"] or "",
            r["decision"],
            r["reason"] or "",
        )
    console.print(table)


@audit_app.command("verify")
def audit_verify() -> None:
    """Verify HMAC chain integrity over the audit log.

    Thin wrapper around :meth:`AuditLogger.verify_chain` that prints a
    row-count diagnostic on success and exits non-zero on failure. Same
    underlying check as ``opencomputer consent verify-chain``; lives
    under ``audit`` because that's where users intuit it belongs.
    """
    _, _, logger = _open_consent_db()
    ok, n = logger.verify_chain_detailed()
    if ok:
        typer.echo(f"Chain intact ({n} rows verified)")
        return
    typer.echo(f"Chain broken at row {n}", err=True)
    raise typer.Exit(1)
