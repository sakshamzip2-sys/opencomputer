"""``oc secrets`` — audit, resolve, list, and migrate secret references.

Subcommands:

* ``oc secrets audit [PATH...]`` — walk one or more config files (or
  the active profile by default) and report plaintext-shaped
  credentials and ``$secret_ref`` usages.
* ``oc secrets resolve REF_ID`` — resolve a single ref against the
  active registry. Echoes the *length* of the resolved value, never
  the value itself, so a screen-recording or shoulder-surfing
  scenario can't leak the secret.
* ``oc secrets list`` — list configured spec ids without values.
* ``oc secrets configure`` — interactive (or ``--yes``) migration:
  scans ``os.environ`` for known credential names and writes them as
  ``source: env`` specs into ``<profile>/secrets.json``, ready for
  later swap to ``source: exec`` against 1Password / Vault / sops.
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


@secrets_app.command("configure")
def cmd_configure(
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Non-interactive: auto-accept every detected credential.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Print the planned secrets.json without writing it.",
    ),
) -> None:
    """Scan ``os.environ`` for known credential names and migrate them
    into ``<profile>/secrets.json`` as ``source: env`` specs.

    Detected env vars are matched against a curated list of known
    credential prefixes/exact names. Each match becomes a spec like::

        {"id": "anthropic", "source": "env",
         "lookup": "ANTHROPIC_API_KEY",
         "export_as": "ANTHROPIC_API_KEY"}

    ``export_as`` mirrors the env-var name so the loader rewrites the
    same key back on startup — making the migration a no-op for
    consumers that already read ``ANTHROPIC_API_KEY``. The operator can
    later swap a spec's ``source`` to ``exec`` or ``file`` and the rest
    of the system keeps working.

    Use ``--yes`` for non-interactive migration; ``--dry-run`` to
    preview without writing.
    """
    profile = _active_profile_dir()
    existing_specs = _load_specs_from_profile()
    existing_ids = {s.id for s in existing_specs}
    existing_lookups = {s.lookup for s in existing_specs if s.source == "env"}
    detected = _detect_credentials_in_environ()
    if not detected:
        _console.print(
            "[yellow]No known credential env vars detected.[/yellow] "
            "Nothing to migrate."
        )
        return

    new_specs: list[dict[str, str]] = []
    skipped: list[str] = []
    for env_name, spec_id in detected:
        if env_name in existing_lookups:
            skipped.append(f"{env_name} (already migrated)")
            continue
        if spec_id in existing_ids:
            spec_id = f"{spec_id}-{env_name.lower()}"
        if yes:
            confirm = True
        else:
            confirm = typer.confirm(
                f"Migrate {env_name!r} → spec id {spec_id!r}?",
                default=True,
            )
        if not confirm:
            skipped.append(f"{env_name} (skipped by user)")
            continue
        new_specs.append({
            "id": spec_id,
            "source": "env",
            "lookup": env_name,
            "export_as": env_name,
        })

    if not new_specs:
        _console.print("[yellow]Nothing migrated.[/yellow]")
        if skipped:
            _console.print("Skipped: " + ", ".join(skipped))
        return

    secrets_path = profile / "secrets.json"
    if secrets_path.is_file():
        try:
            existing_doc = json.loads(secrets_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing_doc = {}
    else:
        existing_doc = {}
    if not isinstance(existing_doc, dict):
        existing_doc = {}
    existing_doc.setdefault("secrets", [])
    if not isinstance(existing_doc["secrets"], list):
        existing_doc["secrets"] = []
    existing_doc["secrets"].extend(new_specs)

    rendered = json.dumps(existing_doc, indent=2, sort_keys=False)
    if dry_run:
        _console.print(rendered)
        _console.print(
            f"[yellow]dry-run[/yellow] — would write {secrets_path} "
            f"(+{len(new_specs)} specs)"
        )
        return

    profile.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text(rendered + "\n", encoding="utf-8")
    try:
        secrets_path.chmod(0o600)
    except OSError as e:
        _console.print(
            f"[yellow]warning:[/yellow] chmod 600 on {secrets_path} failed: {e}"
        )
    _console.print(
        f"[green]wrote[/green] {secrets_path} (+{len(new_specs)} specs)"
    )
    if skipped:
        _console.print("Skipped: " + ", ".join(skipped))
    _console.print(
        "Restart oc to apply. Resolved values are exported into "
        "os.environ so existing code that reads e.g. ANTHROPIC_API_KEY "
        "keeps working."
    )


# Curated set of known credential env-var names. Format:
# ``(env_var_name, suggested_spec_id)``. Spec ids are kept short so
# users can later reference them in plugin-specific config without
# typing the full env var name.
_KNOWN_CREDENTIAL_NAMES: tuple[tuple[str, str], ...] = (
    ("ANTHROPIC_API_KEY", "anthropic"),
    ("CLAUDE_CODE_OAUTH_TOKEN", "claude-code-oauth"),
    ("OPENAI_API_KEY", "openai"),
    ("OPENROUTER_API_KEY", "openrouter"),
    ("GROQ_API_KEY", "groq"),
    ("MISTRAL_API_KEY", "mistral"),
    ("GEMINI_API_KEY", "gemini"),
    ("DEEPSEEK_API_KEY", "deepseek"),
    ("LMSTUDIO_API_KEY", "lmstudio"),
    ("OLLAMA_API_KEY", "ollama"),
    ("TELEGRAM_BOT_TOKEN", "telegram"),
    ("DISCORD_BOT_TOKEN", "discord"),
    ("SLACK_BOT_TOKEN", "slack"),
    ("MATTERMOST_BOT_TOKEN", "mattermost"),
    ("MATRIX_ACCESS_TOKEN", "matrix"),
    ("WHATSAPP_API_TOKEN", "whatsapp"),
    ("SIGNAL_BOT_TOKEN", "signal"),
    ("GITHUB_TOKEN", "github"),
    ("GH_TOKEN", "github-gh"),
    ("AWS_ACCESS_KEY_ID", "aws-access-key"),
    ("AWS_SECRET_ACCESS_KEY", "aws-secret"),
    ("BROWSER_USE_API_KEY", "browser-use"),
    ("BROWSERBASE_API_KEY", "browserbase"),
    ("FIRECRAWL_API_KEY", "firecrawl"),
    ("HUGGINGFACE_TOKEN", "huggingface"),
    ("LINEAR_API_KEY", "linear"),
    ("NOTION_API_KEY", "notion"),
    ("PINECONE_API_KEY", "pinecone"),
    ("POSTMAN_API_KEY", "postman"),
    ("SOURCEGRAPH_TOKEN", "sourcegraph"),
)


def _detect_credentials_in_environ() -> list[tuple[str, str]]:
    """Return the subset of :data:`_KNOWN_CREDENTIAL_NAMES` that have a
    non-empty value in ``os.environ`` right now."""
    return [
        (env_name, spec_id)
        for env_name, spec_id in _KNOWN_CREDENTIAL_NAMES
        if os.environ.get(env_name)
    ]


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
