"""Typer app for ``opencomputer evolution`` subcommands.

Session A wires this into the main CLI by adding (in one line in cli.py):

    from opencomputer.evolution.entrypoint import evolution_app
    app.add_typer(evolution_app, name="evolution")

Until that wiring lands, end users can invoke the subapp directly via
``python -m opencomputer.evolution.entrypoint <subcommand>`` for development /
dogfood.
"""

from __future__ import annotations

import typer

evolution_app = typer.Typer(
    name="evolution",
    help="Self-improvement: trajectory collection, reflection, skill synthesis. Opt-in.",
    no_args_is_help=True,
)

# Importing cli registers commands on evolution_app via @evolution_app.command(...)
from opencomputer.evolution import cli as _cli  # noqa: F401, E402


def wire_into(parent_app: typer.Typer) -> None:
    """Convenience: wire the evolution subapp into a parent Typer app.

    Equivalent to ``parent_app.add_typer(evolution_app, name='evolution')``.
    Provided so Session A doesn't need to know our internal module layout.
    """
    parent_app.add_typer(evolution_app, name="evolution")


if __name__ == "__main__":
    evolution_app()
