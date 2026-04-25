"""`opencomputer models` CLI subcommand — add + list curated model entries.

Round 2A P-11 (Tier 4). Surfaces the existing
:mod:`opencomputer.agent.model_metadata` registry to the user via two
subcommands:

* ``models add PROVIDER MODEL [--alias X] [--context N] [--cost-input N]
  [--cost-output N]`` — register (or update) a (provider, model) entry
  and persist it to ``<profile_home>/model_overrides.yaml`` so the
  next process start re-applies the same delta.
* ``models list [--provider X]`` — pretty-print every model currently
  in the in-memory registry, optionally filtered by provider id.

Persistence is file-backed (YAML), atomic-write, mode 0600. The
overrides file is read on CLI startup (see ``opencomputer/cli.py``)
so the merge is replayed against a fresh interpreter without forcing
the user to re-run ``models add`` after every restart.

Non-destructive: ``models add`` only ADDs or MUTATES; existing
entries (curated G.32 defaults + plugin-shipped catalogs) survive.
Idempotent: re-running ``models add`` with no flag changes is a
no-op (logs ``already registered``).
"""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from opencomputer.agent.model_metadata import (
    ADD_STATUS_ADDED,
    ADD_STATUS_UPDATED,
    list_models,
    register_user_model,
    upsert_override_file,
)

models_app = typer.Typer(
    name="models",
    help="Curate the model-metadata registry (context length + cost).",
)
console = Console()


@models_app.command("add")
def models_add(
    provider: str = typer.Argument(
        ...,
        help="Provider id this model belongs to (e.g. 'anthropic', 'openai', 'bedrock').",
    ),
    model: str = typer.Argument(
        ...,
        help="Model id (e.g. 'claude-opus-4-7', 'gpt-5.4').",
    ),
    alias: str = typer.Option(
        "", "--alias",
        help="Optional alias — second id resolving to the same metadata.",
    ),
    context: int = typer.Option(
        0, "--context",
        help="Max context length in tokens. Omit to leave existing value untouched.",
        min=0,
    ),
    cost_input: float = typer.Option(
        -1.0, "--cost-input",
        help="USD per 1M input tokens. Omit to leave existing value untouched.",
    ),
    cost_output: float = typer.Option(
        -1.0, "--cost-output",
        help="USD per 1M output tokens. Omit to leave existing value untouched.",
    ),
) -> None:
    """Register (or update) a (provider, model) entry and persist it.

    Idempotent: re-running with no flag changes is a no-op. Mutating:
    pass ``--context`` / ``--cost-input`` / ``--cost-output`` to update
    the corresponding field; unspecified flags preserve the stored
    value. Persists to ``<profile_home>/model_overrides.yaml`` and
    live-mutates the in-memory registry — no restart required.
    """
    if not provider.strip():
        console.print("[bold red]error:[/bold red] provider must not be empty.")
        raise typer.Exit(2)
    if not model.strip():
        console.print("[bold red]error:[/bold red] model must not be empty.")
        raise typer.Exit(2)

    # typer can't represent "unset numeric flag" cleanly without dropping
    # to a plain Optional[int] — use sentinel values and translate here.
    context_arg: int | None = context if context > 0 else None
    cost_input_arg: float | None = cost_input if cost_input >= 0 else None
    cost_output_arg: float | None = cost_output if cost_output >= 0 else None
    alias_arg: str | None = alias.strip() or None

    status, meta = register_user_model(
        provider_id=provider,
        model_id=model,
        alias=alias_arg,
        context_length=context_arg,
        input_usd_per_million=cost_input_arg,
        output_usd_per_million=cost_output_arg,
    )

    # Persist regardless of status so the file always reflects the most
    # recent (provider, model) row the user touched. For NOOP this is a
    # cheap rewrite of the same content — atomic swap so it's safe.
    path = upsert_override_file(
        provider_id=provider,
        model_id=model,
        alias=alias_arg,
        context_length=context_arg,
        input_usd_per_million=cost_input_arg,
        output_usd_per_million=cost_output_arg,
    )

    if status == ADD_STATUS_ADDED:
        console.print(f"[green]added[/green] {provider}/{model}")
    elif status == ADD_STATUS_UPDATED:
        console.print(f"[yellow]updated[/yellow] {provider}/{model}")
    else:
        console.print(f"[dim]already registered[/dim] {provider}/{model} (no changes)")

    detail_bits: list[str] = []
    if meta.context_length is not None:
        detail_bits.append(f"context={meta.context_length:,}")
    if meta.input_usd_per_million is not None:
        detail_bits.append(f"input=${meta.input_usd_per_million}/M")
    if meta.output_usd_per_million is not None:
        detail_bits.append(f"output=${meta.output_usd_per_million}/M")
    if alias_arg:
        detail_bits.append(f"alias={alias_arg}")
    if detail_bits:
        console.print(f"  [dim]{', '.join(detail_bits)}[/dim]")
    console.print(f"  [dim]persisted to {path}[/dim]")


@models_app.command("list")
def models_list(
    provider: str = typer.Option(
        "", "--provider", "-p",
        help="Filter to entries whose provider_id matches this value.",
    ),
) -> None:
    """List every model currently in the in-memory registry.

    The registry is the union of the curated G.32 defaults, any
    plugin-shipped contributions, and the user's
    ``model_overrides.yaml``. Use ``--provider`` to filter to one
    provider id.
    """
    rows = list_models()
    if provider:
        rows = [m for m in rows if m.provider_id == provider]
    if not rows:
        if provider:
            console.print(f"[dim]no models registered for provider {provider!r}.[/dim]")
        else:
            console.print("[dim]no models registered.[/dim]")
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("provider", style="magenta")
    table.add_column("model_id", style="cyan", overflow="fold")
    table.add_column("context", justify="right")
    table.add_column("$/M input", justify="right")
    table.add_column("$/M output", justify="right")
    for m in rows:
        table.add_row(
            m.provider_id or "[dim]-[/dim]",
            m.model_id,
            f"{m.context_length:,}" if m.context_length else "[dim]-[/dim]",
            f"{m.input_usd_per_million}" if m.input_usd_per_million is not None else "[dim]-[/dim]",
            f"{m.output_usd_per_million}" if m.output_usd_per_million is not None else "[dim]-[/dim]",
        )
    console.print(table)


__all__ = ["models_app"]
