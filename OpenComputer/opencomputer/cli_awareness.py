"""V2.C — ``opencomputer awareness {patterns,personas} {list,mute,unmute}``.

Two subgroups under one top-level group:

* ``awareness patterns`` — life-event pattern controls (T1/T2 registry).
  Mute state is persisted at ``$OPENCOMPUTER_HOME/awareness/muted_patterns.json``
  (atomic truncate-then-write; single-user). On agent start the registry
  reads this file once and applies the muted set.

* ``awareness personas`` — plural-persona controls. Registry is implemented
  in T4; until then ``personas list`` prints a stub line so users running
  ``--help`` against a partially-built tree don't hit ``ImportError``.

Capability claims for these flows live in ``F1_CAPABILITIES`` under
``awareness.life_event.*`` and ``awareness.persona.*`` (all IMPLICIT — see
the taxonomy comment for the rationale).
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

awareness_app = typer.Typer(help="Layered Awareness controls (patterns + personas)")
patterns_app = typer.Typer(help="Life-event pattern controls")
personas_app = typer.Typer(help="Plural-persona controls")
awareness_app.add_typer(patterns_app, name="patterns")
awareness_app.add_typer(personas_app, name="personas")


def _muted_state_path() -> Path:
    """Return path to the persisted muted-patterns JSON list.

    Resolved every call (not cached) so tests that monkey-patch
    ``OPENCOMPUTER_HOME`` per-test pick up the right tmp path.
    """
    from opencomputer.agent.config import _home

    return _home() / "awareness" / "muted_patterns.json"


def _load_muted() -> list[str]:
    """Load persisted muted pattern IDs. Tolerates missing/corrupt file."""
    path = _muted_state_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data]


def _save_muted(muted: list[str]) -> None:
    """Persist muted pattern IDs (truncate-then-write)."""
    path = _muted_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(muted))


def _registry_pattern_ids() -> set[str]:
    """Return the set of known pattern IDs from the default registry."""
    from opencomputer.awareness.life_events.registry import LifeEventRegistry

    reg = LifeEventRegistry()
    return {pid for pid, _surf, _muted in reg.list_patterns()}


@patterns_app.command("list")
def patterns_list() -> None:
    """List all registered life-event patterns + their muted state."""
    from opencomputer.awareness.life_events.registry import LifeEventRegistry

    reg = LifeEventRegistry()
    persisted_muted = set(_load_muted())
    typer.echo(f"{'pattern_id':30s} {'surfacing':10s} {'muted':6s}")
    for pattern_id, surfacing, in_memory_muted in reg.list_patterns():
        muted = in_memory_muted or (pattern_id in persisted_muted)
        typer.echo(f"{pattern_id:30s} {surfacing:10s} {'yes' if muted else 'no':6s}")


@patterns_app.command("mute")
def patterns_mute(
    pattern_id: str = typer.Argument(..., help="Pattern ID to mute (see `awareness patterns list`)."),
) -> None:
    """Mute a life-event pattern (silent for the rest of this session AND saved)."""
    valid_ids = _registry_pattern_ids()
    if pattern_id not in valid_ids:
        typer.echo(f"Unknown pattern: {pattern_id}", err=True)
        typer.echo(f"Known patterns: {', '.join(sorted(valid_ids))}", err=True)
        raise typer.Exit(1)

    muted = _load_muted()
    if pattern_id not in muted:
        muted.append(pattern_id)
    _save_muted(muted)
    typer.echo(f"Muted: {pattern_id}")


@patterns_app.command("unmute")
def patterns_unmute(
    pattern_id: str = typer.Argument(..., help="Pattern ID to unmute."),
) -> None:
    """Unmute a previously-muted life-event pattern."""
    state_path = _muted_state_path()
    if not state_path.exists():
        typer.echo("Nothing muted (no state file).")
        return
    muted = _load_muted()
    if pattern_id in muted:
        muted.remove(pattern_id)
    _save_muted(muted)
    typer.echo(f"Unmuted: {pattern_id}")


@personas_app.command("list")
def personas_list() -> None:
    """List all registered personas.

    The persona registry is implemented in V2.C-T4. Until that lands this
    command prints a stub instead of raising ``ImportError`` so the CLI
    surface stays usable.
    """
    try:
        from opencomputer.awareness.personas.registry import (  # type: ignore[import-not-found]
            list_personas,
        )
    except ImportError:
        typer.echo("No personas registered yet (V2.C-T4 pending)")
        return

    personas = list_personas()
    if not personas:
        typer.echo("No personas registered.")
        return
    typer.echo(f"{'persona_id':20s} {'description':50s}")
    for p in personas:
        # Tolerate either {"id": ..., "description": ...} dicts or objects
        # exposing those attributes; pick whichever shape T4 ships.
        if isinstance(p, dict):
            pid = str(p.get("id", ""))
            desc = str(p.get("description", ""))
        else:
            pid = str(getattr(p, "id", ""))
            desc = str(getattr(p, "description", ""))
        typer.echo(f"{pid:20s} {desc:50s}")
