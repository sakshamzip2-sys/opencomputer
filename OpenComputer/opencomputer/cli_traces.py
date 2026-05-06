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
    for sub in (
        "state",
        "identity",
        "config",
        "session_state",
        "tag_extractor",
        "novelty_judge",
        "distiller",
        "prefetch",
        "subscriber",
    ):
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
            parent.client = client_pkg
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
    """Show aggregate-only state + wiring diagnostics.

    Privacy contract: this output never includes specific session ids,
    intents, distilled insights, or anything that could leak user
    content. Only aggregate state appears.

    The "wired" line answers "would `opencomputer chat` actually emit
    traces right now?" — if false, enabling the feature was incomplete
    and you'll get pre-task lookup but no post-task submissions.
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

    # Phase 5: in-flight tracked sessions (pre→post-task bridge).
    # Aggregate count only — the per-session ids would correlate
    # with active conversations, which is privacy-sensitive.
    try:
        from extensions.social_traces.session_state import tracked_session_count

        typer.echo(f"tracked sessions: {tracked_session_count()}")
    except Exception:  # noqa: BLE001 — diagnostic only, never fail the status
        pass

    # Subscriber-wired diagnostic: tells the operator whether the
    # current process actually has the LLM pipeline plumbed. CLI
    # status calls run in their own ephemeral process, so a `not
    # wired` here means "no recent gateway/chat process was
    # running" — NOT a configuration bug. Documented expectation.
    try:
        from extensions.social_traces.plugin import get_active_subscriber

        sub = get_active_subscriber()
        if sub is None:
            typer.echo(
                "subscriber: not wired in this process "
                "(post-task emit only fires while `opencomputer chat` or "
                "`opencomputer gateway` is running)"
            )
        else:
            provider_name = type(sub._provider).__name__ if sub._provider else "<none>"
            typer.echo(f"subscriber: wired (provider={provider_name})")
    except Exception:  # noqa: BLE001 — diagnostic only
        pass

    # Configured-provider check: catches "I enabled traces but my
    # config has no provider section" before the user wonders why
    # nothing is being emitted.
    try:
        from opencomputer.agent.config_store import load_config

        cfg = load_config(profile_home / "config.yaml")
        configured = cfg.model.provider if cfg.model else None
        typer.echo(
            f"configured provider: {configured or '<unset — emit pipeline will degrade>'}"
        )
    except Exception:  # noqa: BLE001
        pass


# ── ``traces rotate-id`` ──────────────────────────────────────────────────


@app.command("rotate-id")
def rotate_id(
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Force-regenerate this profile's submitter_hash.

    Future submissions will appear to come from a new agent — past
    submissions remain attributed to the old id (the network has no
    way to retroactively re-key). Use this if you want to start fresh
    on the network without bringing along whatever reputation
    (positive or negative) the old id has accumulated.

    Equivalent to ``rm <profile_home>/traces/agent_id`` followed by
    the next pipeline call regenerating it, but ergonomic and
    confirmable.
    """
    _ensure_alias()
    from extensions.social_traces.identity import agent_id_path, rotate_agent_id

    profile_home = _profile_home()
    path = agent_id_path(profile_home)
    if path.exists() and not yes:
        typer.echo(
            f"This will replace the agent_id at {path} with a new random "
            "value. The network will treat future submissions as a new "
            "agent."
        )
        if not typer.confirm("Rotate?", default=False):
            typer.echo("aborted.")
            raise typer.Exit(code=1)

    old_id, new_id = rotate_agent_id(profile_home)
    if old_id:
        typer.echo(f"rotated. old: {old_id[:8]}…  new: {new_id[:8]}…")
    else:
        typer.echo(f"generated new agent_id: {new_id[:8]}…")


# ── ``traces dry-run`` ────────────────────────────────────────────────────


@app.command("dry-run")
def dry_run(
    session_id: str = typer.Argument(
        ..., help="Session id to distill (see `opencomputer sessions list`)."
    ),
    no_llm: bool = typer.Option(
        False,
        "--no-llm",
        help="Skip provider calls — only show pipeline structure (gates, "
        "transcript length, redaction sweep). No cost incurred.",
    ),
) -> None:
    """Run the distill pipeline for one session and print the result
    without submitting to the network.

    Useful before enabling traces for the first time: pick a known
    session, see what the redactor + distiller would produce, and
    decide whether you trust the redaction layer with your real data.

    With ``--no-llm`` only the pre-LLM stages (transcript fetch +
    redaction sweep) run, and we print before/after sample. With LLM
    you'll incur ~3-4 Haiku calls (~$0.005-0.02 typical) but get the
    actual TraceCard JSON the subscriber would have submitted.

    Never writes to the outbox. Never calls ``client.submit()``.
    """
    import asyncio
    import json as _json

    _ensure_alias()

    profile_home = _profile_home()

    if no_llm:
        _dry_run_no_llm(profile_home, session_id)
        return

    # Real LLM path. Resolve provider + cost_guard the same way
    # ``opencomputer chat`` does so we exercise the production wiring.
    try:
        from opencomputer.agent.config_store import load_config
        from opencomputer.cost_guard import get_default_guard
        from opencomputer.plugins.registry import registry as _reg
    except ImportError as exc:  # noqa: BLE001
        typer.echo(f"error: cannot import OC core ({exc})", err=True)
        raise typer.Exit(code=1) from None

    cfg = load_config(profile_home / "config.yaml")
    if not cfg.model or not cfg.model.provider:
        typer.echo(
            "error: no provider configured in config.yaml — set model.provider "
            "or run with --no-llm to skip the LLM path.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        provider_factory = _reg.get_provider(cfg.model.provider)
    except Exception as exc:  # noqa: BLE001
        typer.echo(
            f"error: provider {cfg.model.provider!r} not registered ({exc})",
            err=True,
        )
        raise typer.Exit(code=1) from None
    provider = provider_factory(cfg.model)

    from extensions.social_traces import distiller as st_distiller
    from extensions.social_traces.config import from_config_dict
    from extensions.social_traces.identity import get_or_create_agent_id

    raw = {}
    try:
        import yaml as _yaml

        cfg_path = profile_home / "config.yaml"
        if cfg_path.exists():
            raw = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        pass
    st_cfg = from_config_dict(raw.get("social_traces", {}))

    submitter_hash = get_or_create_agent_id(profile_home)
    cost_guard = get_default_guard()

    typer.echo(f"dry-run: session={session_id}")
    typer.echo(f"  provider: {type(provider).__name__}")
    typer.echo(
        f"  redact_paths={st_cfg.privacy.redact_paths} "
        f"redact_hostnames={st_cfg.privacy.redact_hostnames}"
    )
    typer.echo("  running distiller (this incurs Haiku cost)…")

    try:
        proposal = asyncio.run(
            st_distiller.distill_session(
                session_id=session_id,
                profile_home=profile_home,
                submitter_hash=submitter_hash,
                provider=provider,
                cost_guard=cost_guard,
                redact_paths_layer=st_cfg.privacy.redact_paths,
                redact_hostnames_layer=st_cfg.privacy.redact_hostnames,
                sensitive_filter=None,
                harness_version="dry-run",
                outcome="success",
            )
        )
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"distiller raised: {exc!r}", err=True)
        raise typer.Exit(code=1) from None

    if proposal is None:
        typer.echo(
            "result: distiller returned None (session filtered out, no user "
            "message, validation failed, or sensitive_filter collapsed it)"
        )
        return

    from extensions.social_traces.client.local_file import trace_card_to_dict

    typer.echo("---")
    typer.echo(_json.dumps(trace_card_to_dict(proposal), indent=2))
    typer.echo("---")
    typer.echo("(not submitted — dry-run)")


def _dry_run_no_llm(profile_home: Path, session_id: str) -> None:
    """Structure-only dry run: fetch transcript, run redactor, print
    summary. No provider calls, no cost."""
    try:
        from opencomputer.agent.state import SessionDB
    except ImportError as exc:  # noqa: BLE001
        typer.echo(f"error: cannot import SessionDB ({exc})", err=True)
        raise typer.Exit(code=1) from None

    db = SessionDB(profile_home / "sessions.db")
    try:
        messages = db.get_messages(session_id)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: cannot read session {session_id!r}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    if not messages:
        typer.echo(f"session {session_id} has no messages.")
        return

    from extensions.social_traces.config import from_config_dict
    from extensions.social_traces.redactor import is_useful_body, redact

    raw = {}
    try:
        import yaml as _yaml

        cfg_path = profile_home / "config.yaml"
        if cfg_path.exists():
            raw = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        pass
    st_cfg = from_config_dict(raw.get("social_traces", {}))

    n_redacted = 0
    n_collapsed = 0
    sample_before = ""
    sample_after = ""
    for m in messages:
        text = ""
        if isinstance(m.content, str):
            text = m.content
        if not text:
            continue
        after = redact(
            text,
            redact_paths_layer=st_cfg.privacy.redact_paths,
            redact_hostnames_layer=st_cfg.privacy.redact_hostnames,
        )
        if after != text:
            n_redacted += 1
            if not sample_before:
                sample_before = text[:200]
                sample_after = after[:200]
        if not is_useful_body(after):
            n_collapsed += 1

    typer.echo(f"dry-run (no-llm): session={session_id}")
    typer.echo(f"  messages: {len(messages)}")
    typer.echo(f"  messages with redactions: {n_redacted}")
    typer.echo(f"  messages collapsed-as-useless after redact: {n_collapsed}")
    typer.echo(
        f"  redact_paths={st_cfg.privacy.redact_paths} "
        f"redact_hostnames={st_cfg.privacy.redact_hostnames}"
    )
    if sample_before:
        typer.echo("  sample (first redacted message):")
        typer.echo(f"    before: {sample_before!r}")
        typer.echo(f"    after:  {sample_after!r}")


# ── ``traces audit-redactor`` ─────────────────────────────────────────────


@app.command("audit-redactor")
def audit_redactor(
    limit: int = typer.Option(
        50,
        "--limit",
        "-n",
        help="Number of recent sessions to scan.",
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help=(
            "Write the audit to this file. Default: "
            "<profile_home>/traces/audit-<timestamp>.txt. The file may "
            "contain raw user content — do NOT paste it into a chat or "
            "share without re-reading."
        ),
    ),
) -> None:
    """Sweep recent SessionDB messages through the redactor and write
    a before/after diff report for human review.

    Use this BEFORE enabling traces for real to verify the redactor
    catches your codenames, internal hostnames, project paths, etc.
    The output file may contain raw (unredacted) user content — it's
    written under ``<profile_home>/traces/`` and not shipped anywhere.
    Read it once and delete.
    """
    import datetime as _dt

    _ensure_alias()

    profile_home = _profile_home()

    try:
        from opencomputer.agent.state import SessionDB
    except ImportError as exc:  # noqa: BLE001
        typer.echo(f"error: cannot import SessionDB ({exc})", err=True)
        raise typer.Exit(code=1) from None

    db_path = profile_home / "sessions.db"
    if not db_path.exists():
        typer.echo(f"no SessionDB at {db_path} — nothing to audit.")
        return

    db = SessionDB(db_path)

    # SessionDB.list_sessions returns dicts with `id` as the session
    # primary key (sessions table) and started_at-DESC ordering.
    sessions: list[str] = []
    try:
        for s in db.list_sessions(limit=limit):
            sid = s.get("id") if isinstance(s, dict) else None
            if sid:
                sessions.append(sid)
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"error: cannot enumerate sessions ({exc})", err=True)
        raise typer.Exit(code=1) from None

    if not sessions:
        typer.echo("no sessions found.")
        return

    from extensions.social_traces.config import from_config_dict
    from extensions.social_traces.redactor import redact

    raw = {}
    try:
        import yaml as _yaml

        cfg_path = profile_home / "config.yaml"
        if cfg_path.exists():
            raw = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        pass
    st_cfg = from_config_dict(raw.get("social_traces", {}))

    if output is None:
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        output = profile_home / "traces" / f"audit-{ts}.txt"
    output.parent.mkdir(parents=True, exist_ok=True)

    n_msgs = 0
    n_changed = 0
    with output.open("w", encoding="utf-8") as fh:
        fh.write(f"# social-traces redactor audit — {_dt.datetime.now().isoformat()}\n")
        fh.write(f"# profile_home: {profile_home}\n")
        fh.write(f"# sessions scanned: {len(sessions)}\n")
        fh.write(
            f"# redact_paths={st_cfg.privacy.redact_paths} "
            f"redact_hostnames={st_cfg.privacy.redact_hostnames}\n"
        )
        fh.write("# Each block shows BEFORE/AFTER for messages whose redacted text differs.\n")
        fh.write("#" + "─" * 78 + "\n\n")
        for sid in sessions:
            try:
                messages = db.get_messages(sid)
            except Exception:  # noqa: BLE001
                continue
            for m in messages:
                if not isinstance(m.content, str) or not m.content:
                    continue
                n_msgs += 1
                after = redact(
                    m.content,
                    redact_paths_layer=st_cfg.privacy.redact_paths,
                    redact_hostnames_layer=st_cfg.privacy.redact_hostnames,
                )
                if after == m.content:
                    continue
                n_changed += 1
                fh.write(f"## session={sid} role={m.role}\n")
                fh.write("BEFORE:\n")
                fh.write(m.content[:2000])
                fh.write("\n\nAFTER:\n")
                fh.write(after[:2000])
                fh.write("\n\n" + "─" * 80 + "\n\n")

    typer.echo(f"audit written: {output}")
    typer.echo(f"  messages scanned: {n_msgs}")
    typer.echo(f"  messages with redactions: {n_changed}")
    typer.echo(
        "  review the file — anything you DON'T want emitted to the "
        "network needs a sensitive_filter or extra regex."
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
