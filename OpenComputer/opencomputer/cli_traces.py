"""``opencomputer traces {enable,disable,status}`` CLI for the
social-traces plugin.

Mirrors the ``skills evolution`` CLI shape (see ``cli_skills.py``) but
lives at the top level because the trace network is a peer feature, not
a skills-catalog sub-feature.

Privacy contract for ``status``:
    The output MUST NEVER include specific session ids, intent strings,
    distilled insights, or anything that could leak user content. Only
    the aggregate state (enabled flag, heartbeat timestamp, outbox
    depth, agent_id presence) appears. Tests in
    ``tests/test_social_traces_cli.py`` enforce this.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
import types
from pathlib import Path

import typer

# Top-level CLI app — mounted into the main ``opencomputer`` Typer in
# ``opencomputer/cli.py``.
app = typer.Typer(
    name="traces",
    help=(
        "Trace network controls — enable/disable the social-traces plugin and "
        "inspect its state."
    ),
    no_args_is_help=True,
)

# Inbox sub-app — `oc traces inbox {add,list,show,remove}`. Phase 3
# helpers for the local-file backend dev stub. When the http backend
# lands these verbs become "no-ops with a hint" — inbox lives on the
# server side once OpenHub is real.
inbox_app = typer.Typer(
    name="inbox",
    help="Manage the local-file inbox (Phase 3 dev stub).",
    no_args_is_help=True,
)
app.add_typer(inbox_app, name="inbox")

# Outbox sub-app — `oc traces outbox {list,show}`. Read-only inspection
# of pending submissions queued by the local-file backend.
outbox_app = typer.Typer(
    name="outbox",
    help="Inspect the local-file outbox (Phase 3 dev stub).",
    no_args_is_help=True,
)
app.add_typer(outbox_app, name="outbox")


# ── extensions.social_traces alias bootstrap ────────────────────────────────
#
# The plugin lives at ``extensions/social-traces/`` (hyphenated). Python
# module names use underscores, so we register a synthetic namespace package
# pointing at the hyphenated dir on first import — same pattern as
# ``cli_skills._ensure_skill_evolution_alias``.

def _ensure_alias() -> None:
    if "extensions.social_traces.state" in sys.modules:
        return
    project_root = Path(__file__).resolve().parent.parent
    ext_dir = project_root / "extensions"
    st_dir = ext_dir / "social-traces"
    if not st_dir.exists():
        return
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(ext_dir)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg
    if "extensions.social_traces" not in sys.modules:
        mod = types.ModuleType("extensions.social_traces")
        mod.__path__ = [str(st_dir)]
        mod.__package__ = "extensions.social_traces"
        sys.modules["extensions.social_traces"] = mod
        sys.modules["extensions"].social_traces = mod  # type: ignore[attr-defined]
    parent = sys.modules["extensions.social_traces"]
    for sub in ("state", "identity", "config", "prefetch", "subscriber"):
        full_name = f"extensions.social_traces.{sub}"
        if full_name in sys.modules:
            setattr(parent, sub, sys.modules[full_name])
            continue
        init = st_dir / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.social_traces"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)
        setattr(parent, sub, sub_mod)

    # client/ subpackage — Phase 3 local_file backend.
    client_dir = st_dir / "client"
    if client_dir.exists():
        client_init = client_dir / "__init__.py"
        if (
            "extensions.social_traces.client" not in sys.modules
            and client_init.exists()
        ):
            client_pkg = types.ModuleType("extensions.social_traces.client")
            client_pkg.__path__ = [str(client_dir)]
            client_pkg.__package__ = "extensions.social_traces.client"
            # Load __init__.py manually so the factory + re-exports are
            # populated; this matches what a real ``import`` would do.
            spec = importlib.util.spec_from_file_location(
                "extensions.social_traces.client",
                str(client_init),
                submodule_search_locations=[str(client_dir)],
            )
            assert spec is not None and spec.loader is not None
            client_pkg = importlib.util.module_from_spec(spec)
            sys.modules["extensions.social_traces.client"] = client_pkg
            client_pkg.__package__ = "extensions.social_traces.client"
            spec.loader.exec_module(client_pkg)
            setattr(parent, "client", client_pkg)
        for sub in ("local_file",):
            full_name = f"extensions.social_traces.client.{sub}"
            if full_name in sys.modules:
                continue
            init = client_dir / f"{sub}.py"
            if not init.exists():
                continue
            spec = importlib.util.spec_from_file_location(full_name, str(init))
            if spec is None or spec.loader is None:
                continue
            sub_mod = importlib.util.module_from_spec(spec)
            sub_mod.__package__ = "extensions.social_traces.client"
            sys.modules[full_name] = sub_mod
            spec.loader.exec_module(sub_mod)


def _profile_home() -> Path:
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME")
    if env:
        return Path(env)
    from opencomputer.agent.config import _home  # lazy: avoid import cycles

    return _home()


def _format_relative(ts: float, now: float | None = None) -> str:
    if not ts:
        return "never"
    delta = (now if now is not None else time.time()) - ts
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{delta:.0f}s ago"
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


def _outbox_depth(profile_home: Path) -> int:
    """Count pending submissions in the local outbox.

    Phase 9 wires the real outbox writer; this just counts files so the
    status output stays useful from day one.
    """
    outbox = profile_home / "traces" / "outbox"
    if not outbox.exists():
        return 0
    try:
        return sum(1 for _ in outbox.iterdir() if _.is_file())
    except OSError:
        return 0


# ── ``traces enable`` ──────────────────────────────────────────────────────


@app.command("enable")
def enable() -> None:
    """Turn the social-traces feature on for the active profile.

    Writes ``<profile_home>/traces/state.json`` with ``{"enabled": true}``.
    The plugin's hooks and subscriber poll this file at event-arrival
    time — no daemon to restart.

    Note: enabling here is one of TWO required opt-ins. The plugin
    itself must also be loaded via ``opencomputer plugin enable
    social-traces`` (default-disabled in plugin.json).
    """
    _ensure_alias()
    from extensions.social_traces.state import set_enabled

    set_enabled(_profile_home(), True)
    typer.echo("social-traces: enabled.")
    typer.echo(
        "  Pre-task hook will query the network; novel sessions may emit "
        "TraceCards."
    )
    typer.echo(
        "  Make sure the plugin itself is loaded: "
        "`opencomputer plugin enable social-traces`."
    )


# ── ``traces disable`` ─────────────────────────────────────────────────────


@app.command("disable")
def disable() -> None:
    """Turn the social-traces feature off without unloading the plugin.

    Hooks remain registered but become no-ops; subscriber stays attached
    to the bus but ignores events. Cheap to flip on and off.
    """
    _ensure_alias()
    from extensions.social_traces.state import set_enabled

    set_enabled(_profile_home(), False)
    typer.echo("social-traces: disabled.")


# ── ``traces status`` ──────────────────────────────────────────────────────


@app.command("status")
def status() -> None:
    """Show aggregate-only state.

    Privacy contract: this output never includes specific session ids,
    intents, distilled insights, or anything that could leak user
    content. Only aggregate state appears.
    """
    _ensure_alias()
    from extensions.social_traces.identity import agent_id_path
    from extensions.social_traces.state import (
        is_enabled,
        read_heartbeat,
    )

    profile_home = _profile_home()
    enabled = is_enabled(profile_home)
    if not enabled:
        typer.echo("social-traces: disabled (feature is opt-in).")
        typer.echo("  (run `oc traces enable` to turn it on.)")
        return

    typer.echo("social-traces: enabled")

    hb_ts = read_heartbeat(profile_home)
    typer.echo(f"last heartbeat: {_format_relative(hb_ts)}")

    typer.echo(f"outbox depth: {_outbox_depth(profile_home)}")

    aid_path = agent_id_path(profile_home)
    typer.echo(
        f"agent_id: {'present' if aid_path.exists() else 'not yet generated'}"
    )


# ── ``traces inbox …`` ────────────────────────────────────────────────────


def _make_local_client(profile_home: Path):
    """Construct the local-file client. Helper kept here so the CLI
    doesn't have to know the concrete class name (Phase 9 will add an
    http path that goes through the same factory)."""
    _ensure_alias()
    from extensions.social_traces.client import make_client

    return make_client(backend="local", profile_home=profile_home)


@inbox_app.command("list")
def inbox_list() -> None:
    """List traces currently in the local inbox.

    Output is aggregate-friendly: trace id, intent (truncated to 60
    chars), and tags. Distilled insight is NOT shown — use ``show`` for
    one trace at a time when you need the full body.
    """
    client = _make_local_client(_profile_home())
    items = client.list_inbox()
    if not items:
        typer.echo("inbox: empty")
        return
    typer.echo(f"inbox: {len(items)} trace(s)")
    for stem, card in items:
        intent_short = card.intent if len(card.intent) <= 60 else card.intent[:57] + "..."
        tags = ", ".join(card.meta.tags) or "(no tags)"
        typer.echo(f"  - {stem}: {intent_short}  [{tags}]")


@inbox_app.command("show")
def inbox_show(
    ident: str = typer.Argument(..., help="Trace id or filename stem to show."),
) -> None:
    """Print the full TraceCard JSON for one inbox entry."""
    import json

    client = _make_local_client(_profile_home())
    card = client.show_inbox(ident)
    if card is None:
        typer.echo(f"error: no inbox trace matching {ident!r}", err=True)
        raise typer.Exit(code=1)

    from extensions.social_traces.client.local_file import trace_card_to_dict

    typer.echo(json.dumps(trace_card_to_dict(card), indent=2))


@inbox_app.command("add")
def inbox_add(
    source: Path = typer.Argument(
        ...,
        help="Path to a TraceCard JSON file to import into the inbox.",
        exists=True,
        readable=True,
    ),
) -> None:
    """Import a TraceCard JSON file into the local inbox.

    Validates the JSON parses as a TraceCard before copying — a
    malformed file fails fast here rather than later at query time.

    Used during dev to seed the inbox so Phase 4's pre-task lookup can
    return matching traces. Once OpenHub is real, this verb becomes a
    no-op with a hint pointing at the network's submission endpoint.
    """
    client = _make_local_client(_profile_home())
    try:
        dest = client.add_to_inbox(source)
    except (ValueError, TypeError, KeyError) as exc:
        typer.echo(
            f"error: {source} is not a valid TraceCard JSON ({exc})", err=True
        )
        raise typer.Exit(code=1) from None
    except OSError as exc:
        typer.echo(f"error: failed to write inbox file: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"added: {dest}")


@inbox_app.command("remove")
def inbox_remove(
    ident: str = typer.Argument(
        ..., help="Trace id or filename stem to remove."
    ),
) -> None:
    """Delete a trace from the local inbox."""
    client = _make_local_client(_profile_home())
    if not client.remove_from_inbox(ident):
        typer.echo(f"error: no inbox trace matching {ident!r}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"removed: {ident}")


# ── ``traces outbox …`` ───────────────────────────────────────────────────


@outbox_app.command("list")
def outbox_list() -> None:
    """List pending submissions in the local outbox."""
    client = _make_local_client(_profile_home())
    items = client.list_outbox()
    if not items:
        typer.echo("outbox: empty")
        return
    typer.echo(f"outbox: {len(items)} pending submission(s)")
    for stem, card in items:
        intent_short = card.intent if len(card.intent) <= 60 else card.intent[:57] + "..."
        tags = ", ".join(card.meta.tags) or "(no tags)"
        typer.echo(f"  - {stem}: {intent_short}  [{tags}]")


@outbox_app.command("show")
def outbox_show(
    ident: str = typer.Argument(..., help="Submission id or filename stem."),
) -> None:
    """Print the full TraceCard JSON for one queued submission."""
    import json

    profile_home = _profile_home()
    outbox = profile_home / "traces" / "outbox"
    direct = outbox / f"{ident}.json"
    if direct.exists():
        typer.echo(direct.read_text(encoding="utf-8"))
        return

    # Fallback: scan + match by id field.
    client = _make_local_client(profile_home)
    for stem, card in client.list_outbox():
        if card.id == ident or stem == ident:
            from extensions.social_traces.client.local_file import (
                trace_card_to_dict,
            )

            typer.echo(json.dumps(trace_card_to_dict(card), indent=2))
            return

    typer.echo(f"error: no outbox submission matching {ident!r}", err=True)
    raise typer.Exit(code=1)


__all__ = ["app"]
