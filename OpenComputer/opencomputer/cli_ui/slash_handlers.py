"""Concrete handlers for the slash commands defined in :mod:`slash`.

Each handler takes a :class:`SlashContext` (the chat loop wires it up
once at session start) and returns a :class:`SlashResult`. Handlers are
intentionally small — anything that needs Rich rendering or filesystem
access uses ``ctx.console``; anything that needs agent state goes
through the callbacks (``on_clear``, ``get_cost_summary``, etc.).

The layer of indirection through callbacks (rather than passing the
agent loop / config directly) keeps this module testable without
booting an agent.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from opencomputer.cli_ui.slash import (
    SLASH_REGISTRY,
    CommandDef,
    SlashResult,
    is_slash_command,
    resolve_command,
)


@dataclass
class SlashContext:
    """Everything a slash handler might need from the chat loop.

    The ``on_*`` callbacks delegate state mutations the chat loop owns
    (e.g. rebinding ``nonlocal session_id`` for ``/clear`` or
    ``/resume``). Default no-op callables keep the dataclass usable in
    test contexts that don't exercise those handlers.
    """

    console: Console
    session_id: str
    config: Any  # Config — typed loosely to avoid import cycle
    on_clear: Callable[[], None]
    get_cost_summary: Callable[[], dict[str, int]]
    get_session_list: Callable[[], list[dict[str, Any]]]
    #: ``/rename <title>`` — returns True on success, False if the title
    #: couldn't be persisted (no current session, DB error).
    on_rename: Callable[[str], bool] = lambda title: False
    #: ``/resume [last|<id-prefix>|pick]`` — returns True if the chat
    #: loop swapped to the target session; False on no-match / ambiguous
    #: prefix / DB error.
    on_resume: Callable[[str], bool] = lambda target: False
    #: ``/queue <prompt>`` — append a prompt to the per-session next-turn
    #: buffer. Returns True on success, False if the queue is full
    #: (default cap = 50). The buffer is FIFO; drained one item per turn
    #: by the chat outer loop ahead of reading from the user.
    on_queue_add: Callable[[str], bool] = lambda text: False
    #: ``/queue list`` — return current pending entries (oldest-first).
    on_queue_list: Callable[[], list[str]] = list
    #: ``/queue clear`` — drop all pending entries; return how many.
    on_queue_clear: Callable[[], int] = lambda: 0
    #: ``/snapshot create [<label>]`` — archive critical state files;
    #: returns the new snapshot id, or ``None`` if no eligible files.
    on_snapshot_create: Callable[[str | None], str | None] = lambda label: None
    #: ``/snapshot list`` — return snapshot manifests, newest first.
    on_snapshot_list: Callable[[], list[dict]] = list
    #: ``/snapshot restore <id> [--only a,b] [--skip x,y]`` — overwrite
    #: current state from snapshot. Optional ``only`` / ``skip`` filters
    #: enable selective restore (v0.5+). Returns count of files restored.
    on_snapshot_restore: Callable[
        [str, list[str] | None, list[str] | None], int,
    ] = lambda sid, only, skip: 0
    #: ``/snapshot list-files <id>`` — return the snapshot's manifest
    #: file list, for selective-restore UX.
    on_snapshot_list_files: Callable[[str], list[str]] = lambda sid: []
    #: ``/snapshot prune`` — drop snapshots beyond the keep cap; returns
    #: count deleted.
    on_snapshot_prune: Callable[[], int] = lambda: 0
    #: ``/reload`` — re-read .env + config.yaml. Returns a small status dict
    #: describing what changed (``{"env_keys_changed": int,
    #: "config_changed": bool, "error": str | None}``).
    on_reload: Callable[[], dict] = dict
    #: ``/reload-mcp`` — disconnect + re-discover MCP servers. Returns
    #: ``{"servers_before": int, "servers_after": int, "tools_after": int,
    #: "error": str | None}``.
    on_reload_mcp: Callable[[], dict] = dict
    #: ``/model <id>`` — swap the active model on the running AgentLoop.
    #: Returns ``(success, message)`` so the slash handler can echo why
    #: the swap failed (unknown alias, invalid model id, provider mismatch).
    on_model_swap: Callable[[str], tuple[bool, str]] = lambda _model: (
        False,
        "model swap callback not wired",
    )
    #: ``/provider <name>`` — swap the active provider plugin instance.
    #: Returns ``(success, message)``. Sub-project D of model-agnosticism.
    on_provider_swap: Callable[[str], tuple[bool, str]] = lambda _name: (
        False,
        "provider swap callback not wired",
    )
    #: ``/compress`` — manually trigger CompactionEngine even when the
    #: input-token threshold hasn't been hit. Returns
    #: ``(ok: bool, before_count: int, after_count: int, reason: str)``.
    #: ``ok=False`` means compaction couldn't run (e.g. bridge unavailable);
    #: ``ok=True with before==after`` means no eligible old block to summarise.
    on_compress: Callable[[], tuple[bool, int, int, str]] = lambda: (
        False, 0, 0, "compress callback not wired",
    )
    #: ``/retry`` — re-queue the last user message. Returns
    #: ``(ok: bool, message_preview: str)``. ``ok=False`` means there's
    #: no prior user message to retry. Hermes-parity Tier B (2026-04-30).
    on_retry: Callable[[], tuple[bool, str]] = lambda: (
        False, "retry callback not wired",
    )
    #: ``/stop`` — kill all background processes spawned this session.
    #: Returns count of processes killed. Hermes-parity Tier B
    #: (2026-04-30). Default: returns 0 (no extension installed).
    on_stop_bg: Callable[[], int] = lambda: 0
    #: ``/image <path>`` — queue a local image for the next user message.
    #: Returns ``(ok: bool, message: str)``. Hermes-parity Tier A
    #: (2026-04-30).
    on_image_attach: Callable[[str], tuple[bool, str]] = lambda _path: (
        False, "image attach callback not wired",
    )
    #: ``/reasoning [args]`` — control reasoning effort + display + show
    #: past turns. Closes over the live ``RuntimeContext`` in the chat
    #: loop so cli_ui doesn't need to import the agent runtime. Returns
    #: the formatted text to print (may include newlines for multi-turn
    #: dumps). Default echoes a "not wired" hint.
    on_reasoning_dispatch: Callable[[str], str] = lambda _args: (
        "/reasoning callback not wired"
    )
    #: ``/sources [args]`` — retroactively expand a turn's web sources.
    #: Same closure pattern as ``on_reasoning_dispatch`` — both share
    #: the per-session ReasoningStore. Empty default echoes a hint.
    on_sources_dispatch: Callable[[str], str] = lambda _args: (
        "/sources callback not wired"
    )


def _split_args(text: str) -> tuple[str, list[str]]:
    """Split ``/cmd arg1 arg2`` into ``("cmd", ["arg1", "arg2"])``."""
    parts = text.lstrip("/").split()
    if not parts:
        return ("", [])
    return (parts[0], parts[1:])


def _handle_exit(ctx: SlashContext, args: list[str]) -> SlashResult:
    return SlashResult(handled=True, exit_loop=True, message="bye.")


def _handle_clear(ctx: SlashContext, args: list[str]) -> SlashResult:
    ctx.on_clear()
    ctx.console.print("[dim]session cleared.[/dim]")
    return SlashResult(handled=True)


def _handle_help(ctx: SlashContext, args: list[str]) -> SlashResult:
    table = Table(title="Slash commands", show_header=True, header_style="bold")
    table.add_column("Command", style="cyan")
    table.add_column("Aliases", style="dim")
    table.add_column("Description")
    for cmd in SLASH_REGISTRY:
        aliases = ", ".join(f"/{a}" for a in cmd.aliases) if cmd.aliases else ""
        ctx_name = f"/{cmd.name}"
        if cmd.args_hint:
            ctx_name = f"{ctx_name} {cmd.args_hint}"
        table.add_row(ctx_name, aliases, cmd.description)
    ctx.console.print(table)
    # 2026-04-29: legend for Shift+Tab cycling, in addition to /mode + aliases.
    ctx.console.print(
        "\n[dim]Modes:[/dim] [bold]Shift+Tab[/bold] cycles "
        "default → accept-edits → auto → plan → default. "
        "Or use [cyan]/mode <name>[/cyan], [cyan]/auto[/cyan], "
        "[cyan]/plan[/cyan], [cyan]/accept-edits[/cyan]."
    )
    return SlashResult(handled=True)


def _handle_screenshot(ctx: SlashContext, args: list[str]) -> SlashResult:
    """Dump the rendered console to a file. Format inferred from extension:
    ``.svg`` → SVG, ``.html`` → HTML, anything else → text."""
    if args:
        path = Path(args[0]).expanduser().resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path.cwd() / f"oc-screenshot-{ts}.txt"
    suffix = path.suffix.lower()
    if suffix == ".svg":
        ctx.console.save_svg(str(path), title="OpenComputer")
    elif suffix in (".html", ".htm"):
        ctx.console.save_html(str(path))
    else:
        ctx.console.save_text(str(path))
    ctx.console.print(f"[green]screenshot →[/green] {path}")
    return SlashResult(handled=True)


def _handle_export(ctx: SlashContext, args: list[str]) -> SlashResult:
    """Same as screenshot but defaults to .md and uses save_text."""
    if args:
        path = Path(args[0]).expanduser().resolve()
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path.cwd() / f"oc-transcript-{ts}.md"
    ctx.console.save_text(str(path))
    ctx.console.print(f"[green]transcript →[/green] {path}")
    return SlashResult(handled=True)


def _handle_cost(ctx: SlashContext, args: list[str]) -> SlashResult:
    summary = ctx.get_cost_summary()
    in_tok = summary.get("in", 0)
    out_tok = summary.get("out", 0)
    ctx.console.print(
        f"[bold]session tokens[/bold]  in={in_tok}  out={out_tok}  total={in_tok + out_tok}"
    )
    return SlashResult(handled=True)


_NATIVE_VENDOR_PREFIXES = {"anthropic", "openai", "bedrock", "openrouter"}


def _handle_provider(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/provider [<name>]`` — show or swap the active provider (Sub-project D)."""
    if not args:
        p = getattr(ctx.config.model, "provider", "?")
        ctx.console.print(f"[bold]active provider[/bold]  {p}")
        return SlashResult(handled=True)
    new_provider = args[0].strip()
    success, message = ctx.on_provider_swap(new_provider)
    if success:
        ctx.console.print(f"[green]provider →[/green] {message}")
    else:
        ctx.console.print(f"[red]swap failed:[/red] {message}")
    return SlashResult(handled=True)


def _handle_model(ctx: SlashContext, args: list[str]) -> SlashResult:
    if not args:
        m = getattr(ctx.config.model, "model", "?")
        p = getattr(ctx.config.model, "provider", "?")
        ctx.console.print(f"[bold]active model[/bold]  {m}  ({p})")
        return SlashResult(handled=True)
    # Mid-session swap (Sub-project C of model-agnosticism plan).
    new_model = args[0].strip()

    # Sub-project D — vendor-prefix triggers cross-provider swap. OpenRouter
    # accepts vendor/model verbatim (routes on its end). Native providers
    # use only the model id after the slash.
    if "/" in new_model:
        vendor, model_only = new_model.split("/", 1)
        if vendor in _NATIVE_VENDOR_PREFIXES:
            prov_ok, prov_msg = ctx.on_provider_swap(vendor)
            if not prov_ok:
                ctx.console.print(
                    f"[red]provider swap failed:[/red] {prov_msg}"
                )
                return SlashResult(handled=True)
            if vendor != "openrouter":
                new_model = model_only

    success, message = ctx.on_model_swap(new_model)
    if success:
        ctx.console.print(f"[green]model →[/green] {message}")
    else:
        ctx.console.print(f"[red]swap failed:[/red] {message}")
    return SlashResult(handled=True)


def _handle_sessions(ctx: SlashContext, args: list[str]) -> SlashResult:
    sessions = ctx.get_session_list()
    if not sessions:
        ctx.console.print("[dim]no prior sessions.[/dim]")
        return SlashResult(handled=True)
    table = Table(title="Recent sessions", show_header=True)
    table.add_column("id", style="cyan")
    table.add_column("started_at")
    for s in sessions[:20]:
        table.add_row(s.get("id", "?"), str(s.get("started_at", "?")))
    ctx.console.print(table)
    return SlashResult(handled=True)


def _handle_rename(ctx: SlashContext, args: list[str]) -> SlashResult:
    title = " ".join(args).strip()
    if not title:
        ctx.console.print(
            "[red]/rename needs a title[/red] — e.g. `/rename my-debug-session`"
        )
        return SlashResult(handled=True)
    ok = ctx.on_rename(title)
    if ok:
        ctx.console.print(f"[green]session renamed →[/green] {title}")
    else:
        ctx.console.print("[red]rename failed[/red] (no current session?)")
    return SlashResult(handled=True)


def _handle_resume(ctx: SlashContext, args: list[str]) -> SlashResult:
    target = (args[0] if args else "pick").strip()
    ok = ctx.on_resume(target)
    if not ok:
        ctx.console.print(
            "[red]resume failed[/red] — target not found, ambiguous prefix, "
            "or no prior sessions"
        )
    return SlashResult(handled=True)


def _handle_queue(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/queue [<prompt>|list|clear]`` — manage the next-turn prompt buffer.

    No args: print current count + usage hint.
    ``list``: list pending entries.
    ``clear``: drop all pending; print drop count.
    Anything else: treat the full ``args`` joined with spaces as the
    prompt to queue.
    """
    if not args:
        pending = ctx.on_queue_list()
        ctx.console.print(
            f"[dim]queue: {len(pending)} pending. "
            f"Use [cyan]/queue <prompt>[/cyan] to add, "
            f"[cyan]/queue list[/cyan] to show, "
            f"[cyan]/queue clear[/cyan] to drop all.[/dim]"
        )
        return SlashResult(handled=True)
    sub = args[0].lower()
    if sub == "list":
        pending = ctx.on_queue_list()
        if not pending:
            ctx.console.print("[dim]queue is empty.[/dim]")
            return SlashResult(handled=True)
        ctx.console.print(f"[bold]queue ({len(pending)} pending):[/bold]")
        for i, p in enumerate(pending, start=1):
            preview = p if len(p) <= 80 else p[:77] + "..."
            ctx.console.print(f"  [dim]{i}.[/dim] {preview}")
        return SlashResult(handled=True)
    if sub == "clear":
        n = ctx.on_queue_clear()
        ctx.console.print(f"[green]queue cleared[/green] — {n} dropped.")
        return SlashResult(handled=True)
    text = " ".join(args).strip()
    if not text:
        ctx.console.print("[red]queue: empty prompt[/red]")
        return SlashResult(handled=True)
    ok = ctx.on_queue_add(text)
    if ok:
        preview = text if len(text) <= 80 else text[:77] + "..."
        ctx.console.print(
            f"[green]queued[/green] — will fire on next turn: [dim]{preview}[/dim]"
        )
    else:
        ctx.console.print(
            "[red]queue full[/red] — drain with [cyan]/queue clear[/cyan] first."
        )
    return SlashResult(handled=True)


def _handle_footer(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/footer`` — Wave 5 T4 — Hermes-port (e123f4ecf).

    Status-only in this revision: print whether the runtime metadata
    footer is currently enabled. Toggling on/off lives in
    ``~/.opencomputer/<profile>/config.yaml`` under
    ``display.runtime_footer.enabled``.
    """
    try:
        import yaml

        from opencomputer.agent.config import _home
        from opencomputer.gateway.runtime_footer import resolve_footer_config

        _cfg_path = _home() / "config.yaml"
        if _cfg_path.exists():
            try:
                cfg_dict = yaml.safe_load(_cfg_path.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001 — partial yaml is fine; degrade
                cfg_dict = {}
        else:
            cfg_dict = {}
        fc = resolve_footer_config(cfg_dict)
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]/footer status read failed: {e}[/yellow]")
        return SlashResult(handled=True)
    state = "[green]on[/green]" if fc.enabled else "[dim]off[/dim]"
    ctx.console.print(
        f"[bold]runtime footer:[/bold] {state}\n"
        f"  [dim]edit display.runtime_footer.enabled in config.yaml to toggle.[/dim]"
    )
    return SlashResult(handled=True)


def _handle_steer(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/steer <text>`` — Wave 5 T3 — Hermes-port (e27b0b765).

    In the CLI the chat loop is never mid-turn when the slash dispatcher
    runs (the prompt is awaiting input), so this is a queue-at-head
    convenience alias for ``/queue <text>``. In ACP/IDE clients the
    same command actually interrupts an in-flight turn — see
    ``opencomputer/acp/server.py::_handle_steer``.
    """
    text = " ".join(args).strip()
    if not text:
        ctx.console.print(
            "[red]/steer needs text[/red] — e.g. `/steer change direction please`"
        )
        return SlashResult(handled=True)
    ok = ctx.on_queue_add(text)
    if ok:
        preview = text if len(text) <= 80 else text[:77] + "..."
        ctx.console.print(
            f"[green]steered[/green] — next turn will use: [dim]{preview}[/dim]"
        )
    else:
        ctx.console.print(
            "[red]queue full[/red] — drain with [cyan]/queue clear[/cyan] first."
        )
    return SlashResult(handled=True)


def _handle_goal(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/goal [<text>|status|pause|resume|clear]`` — manage persistent goal.

    No args / ``status``: show current goal.
    ``pause``: stop continuation loop.
    ``resume``: resume + reset turn counter.
    ``clear``: drop the goal.
    Anything else: set ``args`` joined with spaces as the new goal text.

    Persists in the ``sessions`` table (schema v11+ ``goal_*`` columns).
    Direct DB access bypasses callback wiring — db_path comes from
    ``ctx.config.session.db_path`` so this handler is self-contained.
    """
    from opencomputer.agent.state import SessionDB

    db = SessionDB(ctx.config.session.db_path)
    sub = (args[0].lower() if args else "status")

    if sub == "status" or not args:
        g = db.get_session_goal(ctx.session_id)
        if g is None:
            ctx.console.print(
                "[dim]no goal set. "
                "Use [cyan]/goal <text>[/cyan] to set one.[/dim]"
            )
            return SlashResult(handled=True)
        state = "[green]active[/green]" if g.active else "[yellow]paused[/yellow]"
        ctx.console.print(
            f"[bold]goal:[/bold] {g.text}\n"
            f"  status: {state}, turn {g.turns_used}/{g.budget}"
        )
        return SlashResult(handled=True)

    if sub == "pause":
        if db.get_session_goal(ctx.session_id) is None:
            ctx.console.print("[red]no goal set.[/red]")
        else:
            db.update_session_goal(ctx.session_id, active=False)
            ctx.console.print("[yellow]goal paused.[/yellow]")
        return SlashResult(handled=True)

    if sub == "resume":
        if db.get_session_goal(ctx.session_id) is None:
            ctx.console.print("[red]no goal set.[/red]")
        else:
            db.update_session_goal(ctx.session_id, active=True, turns_used=0)
            ctx.console.print("[green]goal resumed.[/green] (turn counter reset)")
        return SlashResult(handled=True)

    if sub == "clear":
        if db.get_session_goal(ctx.session_id) is None:
            ctx.console.print("[dim]no goal to clear.[/dim]")
        else:
            db.clear_session_goal(ctx.session_id)
            ctx.console.print("[green]goal cleared.[/green]")
        return SlashResult(handled=True)

    # Otherwise, treat the full args as the new goal text
    text = " ".join(args).strip()
    if not text:
        ctx.console.print("[red]/goal: empty text[/red]")
        return SlashResult(handled=True)
    db.set_session_goal(ctx.session_id, text=text)
    preview = text if len(text) <= 80 else text[:77] + "..."
    ctx.console.print(
        f"[green]goal set:[/green] {preview}\n"
        f"  [dim]budget=20 continuations · use /goal status to check progress[/dim]"
    )
    return SlashResult(handled=True)


def _handle_snapshot(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/snapshot [create [<label>]|list|restore <id>|prune]``.

    Default subcommand (no args) is ``list`` — show recent snapshots.
    """
    sub = (args[0].lower() if args else "list").strip()
    if sub == "create":
        label = " ".join(args[1:]).strip() or None
        sid = ctx.on_snapshot_create(label)
        if sid:
            ctx.console.print(f"[green]snapshot created:[/green] {sid}")
        else:
            ctx.console.print(
                "[yellow]snapshot empty[/yellow] — no eligible state files found "
                "(profile_home may be uninitialized)."
            )
        return SlashResult(handled=True)

    if sub == "list":
        items = ctx.on_snapshot_list()
        if not items:
            ctx.console.print("[dim]no snapshots.[/dim]")
            return SlashResult(handled=True)
        ctx.console.print(f"[bold]snapshots ({len(items)}):[/bold]")
        for i, m in enumerate(items, start=1):
            sid = m.get("id", "?")
            n = m.get("file_count", 0)
            sz = m.get("total_size", 0)
            label = m.get("label") or ""
            label_part = f"  [cyan]{label}[/cyan]" if label else ""
            ctx.console.print(
                f"  [dim]{i}.[/dim] {sid}{label_part}  "
                f"[dim]({n} files, {sz} bytes)[/dim]"
            )
        return SlashResult(handled=True)

    if sub == "restore":
        if len(args) < 2:
            ctx.console.print(
                "[red]usage:[/red] /snapshot restore <id> "
                "[--only a,b] [--skip x,y]  "
                "[dim](try /snapshot list-files <id>)[/dim]"
            )
            return SlashResult(handled=True)
        sid = args[1].strip()
        # Parse optional --only / --skip filters (v0.5+)
        only_list: list[str] | None = None
        skip_list: list[str] | None = None
        i = 2
        while i < len(args):
            tok = args[i]
            if tok == "--only" and i + 1 < len(args):
                only_list = [s.strip() for s in args[i + 1].split(",") if s.strip()]
                i += 2
            elif tok == "--skip" and i + 1 < len(args):
                skip_list = [s.strip() for s in args[i + 1].split(",") if s.strip()]
                i += 2
            else:
                i += 1
        n = ctx.on_snapshot_restore(sid, only_list, skip_list)
        if n > 0:
            sel = ""
            if only_list:
                sel = f" (only: {', '.join(only_list)})"
            elif skip_list:
                sel = f" (skipped: {', '.join(skip_list)})"
            ctx.console.print(
                f"[green]restored {n} files[/green]{sel} from snapshot {sid}.\n"
                "[yellow]restart recommended[/yellow] for state.db / config "
                "changes to take effect."
            )
        else:
            ctx.console.print(
                f"[red]restore failed[/red] — snapshot {sid!r} not found, "
                "no manifest, or filter excluded all files."
            )
        return SlashResult(handled=True)

    if sub == "list-files":
        if len(args) < 2:
            ctx.console.print(
                "[red]usage:[/red] /snapshot list-files <id>"
            )
            return SlashResult(handled=True)
        sid = args[1].strip()
        files = ctx.on_snapshot_list_files(sid)
        if not files:
            ctx.console.print(
                f"[yellow]no manifest[/yellow] for snapshot {sid!r}"
            )
            return SlashResult(handled=True)
        ctx.console.print(f"[bold]Files in snapshot {sid}:[/bold]")
        for f in files:
            ctx.console.print(f"  • {f}")
        ctx.console.print(
            "\n[dim]Use --only / --skip with /snapshot restore to filter.[/dim]"
        )
        return SlashResult(handled=True)

    if sub == "prune":
        n = ctx.on_snapshot_prune()
        ctx.console.print(f"[green]pruned[/green] — {n} snapshot(s) deleted.")
        return SlashResult(handled=True)

    if sub == "export":
        if len(args) < 2:
            ctx.console.print(
                "[red]usage:[/red] /snapshot export <id> [path]  "
                "[dim](default: ~/oc-snapshot-<id>-<ts>.tar.gz)[/dim]"
            )
            return SlashResult(handled=True)
        from opencomputer.agent.config import default_config
        from opencomputer.snapshot.quick import export_snapshot

        sid = args[1].strip()
        dest = Path(" ".join(args[2:])).expanduser() if len(args) >= 3 else None
        try:
            cfg = default_config()
            profile_home = cfg.session.db_path.parent
            out = export_snapshot(profile_home, sid, dest_path=dest)
            ctx.console.print(f"[green]exported →[/green] {out}")
        except (ValueError, OSError) as exc:
            ctx.console.print(f"[red]export failed:[/red] {exc}")
        return SlashResult(handled=True)

    if sub == "import":
        if len(args) < 2:
            ctx.console.print(
                "[red]usage:[/red] /snapshot import <archive-path> [label]"
            )
            return SlashResult(handled=True)
        import tarfile

        from opencomputer.agent.config import default_config
        from opencomputer.snapshot.quick import import_snapshot

        archive = Path(args[1]).expanduser()
        label = " ".join(args[2:]).strip() or None
        try:
            cfg = default_config()
            profile_home = cfg.session.db_path.parent
            new_id = import_snapshot(
                profile_home, archive_path=archive, label=label
            )
            ctx.console.print(f"[green]imported as snapshot[/green] {new_id}")
        except (ValueError, OSError, tarfile.TarError) as exc:
            ctx.console.print(f"[red]import failed:[/red] {exc}")
        return SlashResult(handled=True)

    ctx.console.print(
        f"[red]unknown subcommand:[/red] /snapshot {sub}  "
        "[dim](try create | list | restore <id> | prune | export <id> | import <path>)[/dim]"
    )
    return SlashResult(handled=True)


def _handle_reload(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/reload`` — re-read .env + config.yaml."""
    res = ctx.on_reload()
    if not res:
        ctx.console.print(
            "[red]reload not wired[/red] — chat loop didn't provide a callback."
        )
        return SlashResult(handled=True)
    if res.get("error"):
        ctx.console.print(f"[red]reload failed:[/red] {res['error']}")
        return SlashResult(handled=True)
    env_n = res.get("env_keys_changed", 0)
    cfg_changed = res.get("config_changed", False)
    parts: list[str] = []
    if env_n:
        parts.append(f"{env_n} env var(s) updated")
    if cfg_changed:
        parts.append("config.yaml reloaded")
    if not parts:
        parts.append("no changes detected")
    ctx.console.print("[green]reload:[/green] " + ", ".join(parts) + ".")
    return SlashResult(handled=True)


def _handle_reload_mcp(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/reload-mcp`` — disconnect + re-discover MCP servers."""
    res = ctx.on_reload_mcp()
    if not res:
        ctx.console.print(
            "[red]reload-mcp not wired[/red] — chat loop didn't provide a callback."
        )
        return SlashResult(handled=True)
    if res.get("error"):
        ctx.console.print(f"[red]reload-mcp failed:[/red] {res['error']}")
        return SlashResult(handled=True)
    before = res.get("servers_before", 0)
    after = res.get("servers_after", 0)
    tools = res.get("tools_after", 0)
    ctx.console.print(
        f"[green]reload-mcp:[/green] {before} → {after} servers, "
        f"{tools} tool(s) registered."
    )
    return SlashResult(handled=True)


def _handle_debug(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/debug`` — print a sanitized diagnostic dump to console."""
    from opencomputer.cli_ui.debug_dump import build_debug_dump

    ctx.console.print(build_debug_dump())
    return SlashResult(handled=True)


def _handle_compress(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/compress`` — manually compact older turns via CompactionEngine.

    Hermes-parity Tier S (2026-04-30). Skips the auto-trigger threshold
    so users can force a summary even when context isn't yet "full".
    """
    ok, before, after, reason = ctx.on_compress()
    if not ok:
        ctx.console.print(f"[yellow]{reason}[/yellow]")
        return SlashResult(handled=True)
    if before == after:
        ctx.console.print(
            "[dim]No compression — context not large enough or no eligible "
            "messages to summarise yet.[/dim]"
        )
        return SlashResult(handled=True)
    ctx.console.print(
        f"[green]✓[/green] Compressed: {before} → {after} messages "
        f"({before - after} folded into summary)."
    )
    return SlashResult(handled=True)


# ─── Hermes-parity Tier A (2026-04-30): in-session info wrappers ─────


def _handle_config(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/config`` — show key fields of the active Config."""
    cfg = ctx.config
    lines = ["## Active config\n"]
    try:
        lines.append(f"  model:       {cfg.model.provider} / {cfg.model.model}")
        lines.append(f"  cheap model: {cfg.model.cheap_model or '(disabled)'}")
        lines.append(f"  max tokens:  {cfg.model.max_tokens}")
        lines.append(f"  temperature: {cfg.model.temperature}")
        lines.append(f"  db path:     {cfg.session.db_path}")
        lines.append(f"  memory:      {cfg.memory.declarative_path}")
    except AttributeError as e:
        lines.append(f"  [yellow]config layout differs — {e}[/yellow]")
    ctx.console.print("\n".join(lines))
    return SlashResult(handled=True)


def _handle_insights(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/insights`` — quick session-history snapshot."""
    sessions = ctx.get_session_list()
    cost = ctx.get_cost_summary()
    lines = ["## Session insights\n"]
    lines.append(f"  recent sessions: {len(sessions)}")
    if cost:
        for k, v in cost.items():
            lines.append(f"  {k}: {v}")
    if sessions:
        lines.append("\n  most recent (5):")
        for s in sessions[:5]:
            title = s.get("title") or s.get("id", "")[:8]
            lines.append(f"    • {title}")
    ctx.console.print("\n".join(lines))
    return SlashResult(handled=True)


def _handle_skills_inline(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/skills`` — list installed skills."""
    try:
        from opencomputer.agent.memory import MemoryManager
        cfg = ctx.config
        mem = MemoryManager(cfg.memory.declarative_path, cfg.memory.skills_path)
        skills = mem.list_skills()
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Skills unavailable: {e}[/yellow]")
        return SlashResult(handled=True)
    if not skills:
        ctx.console.print("[dim]No skills installed.[/dim]")
        return SlashResult(handled=True)
    lines = [f"## Skills ({len(skills)})\n"]
    for s in skills:
        name = getattr(s, "name", None) or getattr(s, "id", "?")
        desc = getattr(s, "description", "") or ""
        lines.append(f"  /{name}  [dim]{desc[:80]}[/dim]")
    ctx.console.print("\n".join(lines))
    return SlashResult(handled=True)


def _handle_cron_inline(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/cron`` — list active cron jobs (best-effort)."""
    try:
        from opencomputer.agent.config import _home
        from opencomputer.cron.store import CronStore
        store = CronStore(_home() / "cron" / "cron.json")
        jobs = store.list()
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Cron unavailable: {e}[/yellow]")
        return SlashResult(handled=True)
    if not jobs:
        ctx.console.print("[dim]No cron jobs configured.[/dim]")
        return SlashResult(handled=True)
    lines = [f"## Cron jobs ({len(jobs)})\n"]
    for j in jobs:
        name = getattr(j, "name", None) or getattr(j, "id", "?")
        sched = getattr(j, "schedule", "?")
        lines.append(f"  {name} — {sched}")
    ctx.console.print("\n".join(lines))
    return SlashResult(handled=True)


def _handle_plugins_inline(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/plugins`` — list installed plugins."""
    try:
        from opencomputer.plugins.registry import registry as _reg
        plugins = list(getattr(_reg, "plugins", {}).items())
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Plugins unavailable: {e}[/yellow]")
        return SlashResult(handled=True)
    if not plugins:
        ctx.console.print("[dim]No plugins installed.[/dim]")
        return SlashResult(handled=True)
    lines = [f"## Plugins ({len(plugins)})\n"]
    for name, _val in plugins:
        lines.append(f"  • {name}")
    ctx.console.print("\n".join(lines))
    return SlashResult(handled=True)


def _handle_profile_inline(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/profile`` — show active profile name + home dir."""
    try:
        from opencomputer.agent.config import _home
        from opencomputer.profiles import read_active_profile
        active = read_active_profile() or "default"
        home = _home()
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Profile lookup failed: {e}[/yellow]")
        return SlashResult(handled=True)
    ctx.console.print(
        f"## Active profile\n\n  name: [cyan]{active}[/cyan]\n  home: {home}"
    )
    return SlashResult(handled=True)


def _handle_tools_inline(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/tools`` — read-only inventory of registered tools."""
    try:
        from opencomputer.tools.registry import registry as _treg
        names = sorted(_treg.list_names())
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Tool registry unavailable: {e}[/yellow]")
        return SlashResult(handled=True)
    if not names:
        ctx.console.print("[dim]No tools registered.[/dim]")
        return SlashResult(handled=True)
    lines = [f"## Registered tools ({len(names)})\n"]
    # Render in 3 columns so long lists stay compact.
    col_count = 3
    for i in range(0, len(names), col_count):
        row = names[i:i + col_count]
        lines.append("  " + "    ".join(f"{n:<22}" for n in row))
    ctx.console.print("\n".join(lines))
    return SlashResult(handled=True)


def _handle_image(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/image <path>`` — queue an image for the next user message."""
    if not args:
        ctx.console.print(
            "[yellow]Usage: /image <path>[/yellow]"
        )
        return SlashResult(handled=True)
    path = " ".join(args).strip()
    ok, msg = ctx.on_image_attach(path)
    if ok:
        ctx.console.print(f"[green]✓[/green] {msg}")
    else:
        ctx.console.print(f"[yellow]{msg}[/yellow]")
    return SlashResult(handled=True)


# ─── Hermes-parity Tier B (2026-04-30): /retry + /stop ───────────────


def _handle_retry(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/retry`` — re-queue the last user message via the queue shim."""
    ok, preview = ctx.on_retry()
    if not ok:
        ctx.console.print(f"[yellow]{preview}[/yellow]")
        return SlashResult(handled=True)
    short = preview if len(preview) <= 80 else preview[:77] + "..."
    ctx.console.print(
        f"[green]↻[/green] Queued for retry: [dim]{short}[/dim]"
    )
    return SlashResult(handled=True)


def _handle_stop_bg(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/stop`` — kill all background processes for this session."""
    try:
        killed = ctx.on_stop_bg()
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Stop failed: {e}[/yellow]")
        return SlashResult(handled=True)
    if killed == 0:
        ctx.console.print("[dim]No background processes running.[/dim]")
    else:
        ctx.console.print(
            f"[green]✓[/green] Killed {killed} background process(es)."
        )
    return SlashResult(handled=True)


def _handle_reasoning(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/reasoning [args]`` — delegate to the agent ReasoningCommand.

    The chat loop binds ``on_reasoning_dispatch`` to a closure over the
    live RuntimeContext (which holds the reasoning store + effort flags).
    We just join the args back into a single string and print whatever
    the callback returns.
    """
    try:
        output = ctx.on_reasoning_dispatch(" ".join(args))
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]/reasoning failed: {e}[/yellow]")
        return SlashResult(handled=True)
    if output:
        ctx.console.print(output)
    return SlashResult(handled=True)


def _handle_sources(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/sources [args]`` — delegate to the agent SourcesCommand.

    Same bridge pattern as ``/reasoning`` — closes over the live
    RuntimeContext so cli_ui doesn't import the agent runtime
    directly. Re-renders any past turn's web sources in their
    expanded form, since the trigger printed at finalize is a
    static glyph (Rich scrollback is immutable post-Live.stop).
    """
    try:
        output = ctx.on_sources_dispatch(" ".join(args))
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]/sources failed: {e}[/yellow]")
        return SlashResult(handled=True)
    if output:
        ctx.console.print(output)
    return SlashResult(handled=True)


_HANDLERS: dict[str, Callable[[SlashContext, list[str]], SlashResult]] = {
    "exit": _handle_exit,
    "clear": _handle_clear,
    "help": _handle_help,
    "screenshot": _handle_screenshot,
    "export": _handle_export,
    "cost": _handle_cost,
    "model": _handle_model,
    "provider": _handle_provider,
    "sessions": _handle_sessions,
    "rename": _handle_rename,
    "resume": _handle_resume,
    "queue": _handle_queue,
    "steer": _handle_steer,
    "footer": _handle_footer,
    "goal": _handle_goal,
    "snapshot": _handle_snapshot,
    "reload": _handle_reload,
    "reload-mcp": _handle_reload_mcp,
    "debug": _handle_debug,
    "compress": _handle_compress,
    "config":   _handle_config,
    "insights": _handle_insights,
    "skills":   _handle_skills_inline,
    "cron":     _handle_cron_inline,
    "plugins":  _handle_plugins_inline,
    "profile":  _handle_profile_inline,
    "image":    _handle_image,
    "tools":    _handle_tools_inline,
    "retry":    _handle_retry,
    "stop":     _handle_stop_bg,
    "reasoning": _handle_reasoning,
    "sources": _handle_sources,
}


def dispatch_slash(text: str, ctx: SlashContext) -> SlashResult:
    """Dispatch a slash-command string to its handler.

    Returns ``SlashResult(handled=False)`` for non-slash text so the
    caller can fall back to "treat as normal message". Unknown slash
    commands are consumed (handled=True) with an error message — we
    don't want them to leak to the LLM.
    """
    if not is_slash_command(text):
        return SlashResult(handled=False)
    name, args = _split_args(text)
    cmd: CommandDef | None = resolve_command(name)
    if cmd is None:
        ctx.console.print(f"[red]unknown command:[/red] /{name}  (try /help)")
        return SlashResult(handled=True)
    handler = _HANDLERS[cmd.name]
    return handler(ctx, args)
