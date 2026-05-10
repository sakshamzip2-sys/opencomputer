"""``oc secrets`` — audit and resolve secret references.

Subcommands:

* ``oc secrets audit [PATH...]`` — walk one or more config files (or
  the active profile by default) and report plaintext-shaped
  credentials and ``$secret_ref`` usages.
* ``oc secrets resolve REF_ID`` — resolve a single ref against the
  active registry. Echoes the *length* of the resolved value, never
  the value itself, so a screen-recording or shoulder-surfing
  scenario can't leak the secret.
* ``oc secrets list`` — list configured spec ids without values.

This is the operator surface that makes :mod:`opencomputer.security.secrets`
useful from the command line. It does NOT mutate config files — that's
a separate ``oc secrets configure`` flow which is intentionally out of
scope for this PR (see OC-FROM-OPENCLAW.md item 3 follow-up).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.security.secrets import (
    AuditFinding,
    SecretProviderError,
    SecretRegistry,
    SecretSpec,
    audit_paths,
)

secrets_app = typer.Typer(help="Audit and resolve secret references.")
_console = Console()


def _active_profile_dir() -> Path:
    raw = os.environ.get("OC_PROFILE_DIR") or str(
        Path.home() / ".opencomputer" / "default"
    )
    return Path(raw).expanduser()


def _default_audit_paths() -> list[Path]:
    """Best-effort list of files we want to scan when the user gives no args.

    Conservative — includes config files we know about; absent files
    are silently skipped by :func:`audit_paths`.
    """
    profile = _active_profile_dir()
    candidates = [
        profile / "config.yaml",
        profile / "config.json",
        profile / "bindings.yaml",
        Path.home() / ".opencomputer" / "config.yaml",
    ]
    # Walk profile/plugins/*/config.yaml — plugin-local settings often
    # carry credentials.
    plugins_dir = profile / "plugins"
    if plugins_dir.is_dir():
        candidates.extend(plugins_dir.glob("*/config.yaml"))
        candidates.extend(plugins_dir.glob("*/config.json"))
    return candidates


def _load_specs_from_profile() -> list[SecretSpec]:
    """Read declared secret specs from ``<profile>/secrets.json`` if present.

    Format::

        {
          "secrets": [
            {"id": "anthropic", "source": "env", "lookup": "ANTHROPIC_API_KEY"},
            {"id": "vault-key", "source": "exec",
             "lookup": "secret/openclaw#OPENAI_API_KEY",
             "provider_name": "vault"}
          ]
        }

    Missing file → empty list (a fresh install with no specs configured).
    """
    path = _active_profile_dir() / "secrets.json"
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise typer.BadParameter(
            f"could not parse {path}: {e}"
        ) from e
    out: list[SecretSpec] = []
    for entry in raw.get("secrets", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            out.append(
                SecretSpec(
                    id=str(entry["id"]),
                    source=entry["source"],
                    lookup=str(entry["lookup"]),
                    provider_name=str(entry.get("provider_name", "default")),
                )
            )
        except (KeyError, TypeError):
            continue
    return out


@secrets_app.command("audit")
def cmd_audit(
    paths: list[str] = typer.Argument(
        None,
        help="Files to scan. Defaults to the active profile's config files.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Scan config files for plaintext credentials and SecretRef usage."""
    targets = (
        [Path(p).expanduser() for p in paths] if paths else _default_audit_paths()
    )
    findings: list[AuditFinding] = audit_paths(targets)
    if json_out:
        out = [
            {
                "kind": f.kind,
                "path": str(f.path),
                "detail": f.detail,
                "line": f.line,
            }
            for f in findings
        ]
        _console.print_json(data=out)
        # Non-zero exit if ANY plaintext_secret findings exist — useful
        # for CI gates ("fail the build if any plaintext credentials
        # land in committed configs").
        if any(f.kind == "plaintext_secret" for f in findings):
            raise typer.Exit(code=1)
        return

    if not findings:
        _console.print("[green]No findings — nothing to audit.[/green]")
        return

    table = Table(title="Secrets audit findings")
    table.add_column("Kind", style="bold")
    table.add_column("Path")
    table.add_column("Detail")
    plaintext_count = 0
    for f in findings:
        kind_style = {
            "plaintext_secret": "[red]plaintext[/red]",
            "secret_ref_present": "[green]secret_ref[/green]",
            "unresolved_ref": "[yellow]unresolved[/yellow]",
        }.get(f.kind, f.kind)
        table.add_row(kind_style, str(f.path), f.detail)
        if f.kind == "plaintext_secret":
            plaintext_count += 1
    _console.print(table)
    if plaintext_count:
        _console.print(
            f"[red]{plaintext_count} plaintext finding(s).[/red] "
            f"Move credentials to env vars or a secret manager and "
            f"reference them with $secret_ref. See "
            f"docs/OC-FROM-OPENCLAW.md item 3 for the full pattern."
        )
        raise typer.Exit(code=1)


@secrets_app.command("resolve")
def cmd_resolve(
    ref_id: str = typer.Argument(..., help="The id from secrets.json."),
    show: bool = typer.Option(
        False,
        "--show",
        help="Print the resolved value. Off by default — only the length is shown.",
    ),
) -> None:
    """Resolve REF_ID against the active registry. Diagnostic only.

    By default echoes only the length; pass ``--show`` to print the
    actual value (use only over a private terminal — opt-in to avoid
    accidental shoulder-surfing).
    """
    specs = _load_specs_from_profile()
    if not specs:
        _console.print(
            "[red]No specs configured.[/red] Create "
            f"{_active_profile_dir()/'secrets.json'} with a 'secrets' list."
        )
        raise typer.Exit(code=2)
    reg = SecretRegistry()
    try:
        reg.load(specs)
    except SecretProviderError as e:
        _console.print(f"[red]Registry load failed:[/red] {e}")
        raise typer.Exit(code=2) from e
    value = reg.get(ref_id)
    if value is None:
        _console.print(f"[red]No spec with id {ref_id!r}.[/red]")
        raise typer.Exit(code=1)
    if show:
        _console.print(value)
    else:
        _console.print(
            f"[green]{ref_id}[/green]: resolved (length={len(value)}). "
            f"Pass --show to print the value."
        )


@secrets_app.command("list")
def cmd_list(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """List declared spec ids in the active profile (no values)."""
    specs = _load_specs_from_profile()
    if json_out:
        _console.print_json(
            data=[
                {
                    "id": s.id,
                    "source": s.source,
                    "lookup": s.lookup,
                    "provider_name": s.provider_name,
                }
                for s in specs
            ]
        )
        return
    if not specs:
        _console.print(
            f"[yellow]No specs configured.[/yellow] Create "
            f"{_active_profile_dir()/'secrets.json'} to declare some."
        )
        return
    table = Table(title="Configured secret specs")
    table.add_column("ID", style="bold")
    table.add_column("Source")
    table.add_column("Lookup")
    table.add_column("Provider")
    for spec in specs:
        table.add_row(spec.id, spec.source, spec.lookup, spec.provider_name)
    _console.print(table)
