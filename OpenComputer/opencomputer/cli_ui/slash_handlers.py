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
    register_extra_commands,
    resolve_command,
)
from plugin_sdk.runtime_context import RuntimeContext


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
    #: ``/mcp`` (no args) — live status snapshot. Returns
    #: ``{"servers": list[dict], "connecting": list[str]}`` where each
    #: server dict mirrors :meth:`MCPManager.status_snapshot` (name /
    #: connection_state / tool_count / version / last_error).
    on_mcp_status: Callable[[], dict] = dict
    #: ``/mcp connect <name>`` — connect a server by config name.
    #: Returns ``(ok, message)``.
    on_mcp_connect: Callable[[str], tuple[bool, str]] = lambda _name: (
        False, "/mcp connect callback not wired"
    )
    #: ``/mcp disconnect <name>`` — disconnect by name.
    #: Returns ``(ok, message)``.
    on_mcp_disconnect: Callable[[str], tuple[bool, str]] = lambda _name: (
        False, "/mcp disconnect callback not wired"
    )
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
    #: ``/undo`` — remove the last user/assistant exchange. Bridges to the
    #: agent ``UndoCommand``; the chat loop binds this to a closure over
    #: the live SessionDB + session id. Returns the status text to print.
    on_undo: Callable[[], str] = lambda: "/undo callback not wired"
    #: ``/goal <text>`` mid-run race-guard — returns True iff the AgentLoop
    #: is currently streaming a turn for this session. The slash handler
    #: refuses the SET form when this is True (status / pause / resume /
    #: clear are still allowed). CLI input loop attaches a closure over
    #: its in-flight flag; gateway path inserts its own check at dispatch
    #: time. Default ``lambda: False`` keeps callers that don't care
    #: about race-guarding agnostic.
    is_running_agent: Callable[[], bool] = lambda: False
    #: ``(model, provider)`` getter for the live AgentLoop's active model.
    #: The no-args branches of ``/model`` and ``/provider`` use this so
    #: post-swap reads are FRESH — reading ``ctx.config.model`` directly
    #: returns the frozen session-start snapshot, which drifts every
    #: time the user runs ``/model <id>``. Production wiring in
    #: ``cli.py::_run_chat_session`` closes over the live ``loop``:
    #: ``lambda: (loop.config.model.model, loop.config.model.provider)``.
    #: Default returns ``("", "")`` as a sentinel; the consuming
    #: handler falls back to ``ctx.config`` in that case so test
    #: fixtures that don't wire the getter still render a meaningful
    #: value rather than literal empty strings.
    get_active_model_info: Callable[[], tuple[str, str]] = lambda: ("", "")
    #: Bridge to the System-A built-in command registry (best-of-three
    #: Recipe 2). Called when System B has no native handler for a
    #: command — runs the matching ``agent/slash_commands_impl`` Command
    #: with the live ``RuntimeContext`` and returns ``(found, output)``.
    #: ``found=False`` means no System-A command matched either, so the
    #: dispatcher reports "unknown command". cli.py wires a closure over
    #: the live AgentLoop; the default keeps test contexts agnostic.
    on_builtin_dispatch: Callable[[str, str], tuple[bool, str]] = (
        lambda _name, _args: (False, "")
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


def _read_active_model_info(ctx: SlashContext) -> tuple[str, str]:
    """Resolve the live ``(model, provider)`` for read-only displays.

    Priority order:

    1. ``ctx.get_active_model_info()`` — production wiring closes this
       over the running AgentLoop, so every read reflects the latest
       ``/model`` / ``/provider`` swap.
    2. ``ctx.config.model`` — frozen session-start snapshot. Used only
       when (1) is unwired (test fixtures, ACP one-shot contexts).
       Returns ``"?"`` if even the config is missing.

    Never raises. Adversarial getter implementations (returning
    non-tuple, non-string, or wrong-arity values) degrade to the
    config fallback rather than crashing the slash render.
    """
    try:
        info = ctx.get_active_model_info()
    except Exception as e:  # noqa: BLE001 — never wedge a /model render
        import logging as _logging
        _logging.getLogger(__name__).debug(
            "get_active_model_info raised; falling back to ctx.config: %r", e
        )
        info = None
    if (
        isinstance(info, tuple)
        and len(info) >= 2
        and isinstance(info[0], str)
        and isinstance(info[1], str)
        and info[0]
    ):
        return (info[0], info[1])
    # Fallback to the (potentially stale) session-start snapshot.
    cfg = getattr(ctx, "config", None)
    model_cfg = getattr(cfg, "model", None)
    m_raw = getattr(model_cfg, "model", "?") if model_cfg is not None else "?"
    p_raw = getattr(model_cfg, "provider", "?") if model_cfg is not None else "?"
    m = m_raw if isinstance(m_raw, str) and m_raw else "?"
    p = p_raw if isinstance(p_raw, str) and p_raw else "?"
    return (m, p)


def _handle_provider(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/provider [<name>]`` — show or swap the active provider (Sub-project D)."""
    if not args:
        _, p = _read_active_model_info(ctx)
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
        # Pull FRESH (model, provider) from the live AgentLoop via the
        # getter so post-swap reads reflect the current state. The
        # legacy ``ctx.config.model.model`` read is captured at session
        # start and goes stale the moment the user runs ``/model <id>``
        # — that drift is what made ``/model`` (no args) report the OLD
        # id after a successful swap, fueling the "swap silently fails"
        # bug Saksham reported on 2026-05-11.
        m, p = _read_active_model_info(ctx)
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
    table.add_column("title")
    table.add_column("msgs", justify="right")
    table.add_column("started")
    for s in sessions[:20]:
        sid = str(s.get("id") or "?")
        title = str(s.get("title") or "[untitled]")
        message_count = str(s.get("message_count") or 0)
        table.add_row(
            sid[:8],
            title,
            message_count,
            _format_session_started_at(s.get("started_at")),
        )
    ctx.console.print(table)
    return SlashResult(handled=True)


def _format_session_started_at(value: object) -> str:
    if value in (None, ""):
        return "unknown"
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    text = str(value)
    try:
        return datetime.fromtimestamp(float(text)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text.replace("T", " ")[:16]


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
    """``/footer [on|off|status]`` — Wave 5 T4 — Hermes-port (e123f4ecf).

    Toggle or display the runtime metadata footer. Persists to
    ``~/.opencomputer/<profile>/config.yaml`` under
    ``display.runtime_footer.enabled``. Empty args / ``status`` shows
    current state without writing.

    Wave 5 deferral closure (Approach B-minimal): direct yaml read/write
    instead of a SlashContext.persist_config ABC method, since only this
    one slash command needs persistence today.
    """
    import yaml

    from opencomputer.agent.config import _home
    from opencomputer.gateway.runtime_footer import resolve_footer_config

    cfg_path = _home() / "config.yaml"
    sub = (args[0].lower() if args else "status")

    def _read_cfg() -> dict:
        if not cfg_path.exists():
            return {}
        try:
            return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    def _write_enabled(enabled: bool) -> bool:
        cfg = _read_cfg()
        display = cfg.setdefault("display", {})
        rf = display.setdefault("runtime_footer", {})
        rf["enabled"] = enabled
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(
                yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            ctx.console.print(f"[red]/footer write failed: {exc}[/red]")
            return False
        return True

    if sub in ("on", "off"):
        target = sub == "on"
        if _write_enabled(target):
            # Reflect immediately in runtime context so the next turn sees it.
            try:
                runtime = getattr(ctx, "runtime", None)
                if runtime is not None:
                    runtime.custom = {**(runtime.custom or {}), "show_footer": target}
            except Exception:  # noqa: BLE001 — runtime hint is best-effort
                pass
            ctx.console.print(
                f"[green]runtime footer:[/green] "
                f"{'[green]on[/green]' if target else '[dim]off[/dim]'} "
                f"[dim]({cfg_path})[/dim]"
            )
        return SlashResult(handled=True)

    # status / unknown subcommand
    try:
        fc = resolve_footer_config(_read_cfg())
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]/footer status read failed: {e}[/yellow]")
        return SlashResult(handled=True)
    state = "[green]on[/green]" if fc.enabled else "[dim]off[/dim]"
    ctx.console.print(
        f"[bold]runtime footer:[/bold] {state}\n"
        f"  [dim]/footer on|off to toggle and persist to config.yaml.[/dim]"
    )
    return SlashResult(handled=True)


def _handle_steer(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/steer <text>`` — Wave 5 T3 — Hermes-port (e27b0b765) + PR-A.

    Two integration paths:

    1. **Queue-at-head** (legacy, always fires): ``ctx.on_queue_add``
       drains as the next turn's user message via the existing wave-5
       T3 mechanism. Keeps the long-standing CLI behaviour where /steer
       on an idle prompt simply schedules the next message.
    2. **PR-A cancel-event** (2026-05-07): also calls
       ``SteerRegistry.submit`` so if a tool is mid-flight (e.g. a
       long Bash) the agent loop's cancel-aware dispatcher reacts and
       interrupts. The ack distinguishes "interrupted" (a dispatch was
       in flight and got cancelled) vs "steered" (queue-only path).

    Cross-references:
        ``opencomputer/acp/server.py::_handle_steer`` — same contract
        for IDE clients (also calls SteerRegistry.submit via
        ``ACPSession.steer``).
    """
    text = " ".join(args).strip()
    if not text:
        ctx.console.print(
            "[red]/steer needs text[/red] — e.g. `/steer change direction please`"
        )
        return SlashResult(handled=True)

    # PR-A — figure out whether a dispatch was mid-flight BEFORE submit.
    sid = getattr(ctx, "session_id", "") or ""
    was_mid_dispatch = False
    try:
        from opencomputer.agent.steer import default_registry as _steer_reg
        if sid and _steer_reg.has_cancel_listener(sid):
            ev = _steer_reg.cancel_event(sid)
            # If the event is unset and the listener exists, the agent
            # loop allocated it for an active dispatch (the loop clears
            # stale events at the top of each dispatch, so an unset
            # event means "actively listening").
            was_mid_dispatch = not ev.is_set()
        # Always submit so the cancel signal reaches the dispatcher.
        if sid:
            _steer_reg.submit(sid, text)
    except Exception:  # noqa: BLE001 — never break the slash path
        pass

    # Queue-add for legacy compat (drains as next-turn user message).
    ok = ctx.on_queue_add(text)

    if ok:
        preview = text if len(text) <= 80 else text[:77] + "..."
        status = "interrupted" if was_mid_dispatch else "steered"
        ctx.console.print(
            f"[green]{status}[/green] — next turn will use: [dim]{preview}[/dim]"
        )
    else:
        ctx.console.print(
            "[red]queue full[/red] — drain with [cyan]/queue clear[/cyan] first."
        )
    return SlashResult(handled=True)


_GOAL_SAFE_SUBCOMMANDS: frozenset[str] = frozenset(
    {"status", "pause", "resume", "clear"}
)


def _handle_goal(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/goal [<text>|status|pause|resume|clear]`` — manage persistent goal.

    v2 surface (Kanban-Goals v2, 2026-05-08):

    - Rich UX with icons: ``⊙`` set, ``↻`` resume/continue, ``✓`` achieved,
      ``⏸`` paused, ``✗`` cleared.
    - ``status`` surfaces ``GoalState.last_judge_reason`` (the most recent
      judge rationale) when present.
    - SET form (``/goal <new text>``) is refused while the AgentLoop is
      streaming a turn — set form races with the in-flight continuation
      prompt. Status / pause / resume / clear remain unrestricted because
      they only touch the control plane.

    Persists in the ``sessions`` table (schema v14 ``goal_*`` columns).
    Direct DB access — db_path comes from ``ctx.config.session.db_path``.
    """
    from opencomputer.agent.state import SessionDB

    db = SessionDB(ctx.config.session.db_path)
    sub = (args[0].lower() if args else "status")
    is_set_form = bool(args) and sub not in _GOAL_SAFE_SUBCOMMANDS

    if is_set_form and ctx.is_running_agent():
        ctx.console.print(
            "[yellow]/goal: agent is currently running — "
            "use [cyan]/stop[/cyan] first, then set the new goal.[/yellow]"
        )
        return SlashResult(handled=True)

    if sub == "status" or not args:
        g = db.get_session_goal(ctx.session_id)
        if g is None:
            ctx.console.print(
                "[dim]no goal set. "
                "Use [cyan]/goal <text>[/cyan] to set one.[/dim]"
            )
            return SlashResult(handled=True)
        if g.budget_exhausted():
            ctx.console.print(
                f"[yellow]⏸ goal paused — {g.turns_used}/{g.budget} "
                f"turns used. Use [cyan]/goal resume[/cyan] to keep going, "
                f"or [cyan]/goal clear[/cyan] to stop.[/yellow]\n"
                f"  [bold]goal:[/bold] {g.text}"
            )
            if g.last_judge_reason:
                ctx.console.print(
                    f"  last judge: [dim]{g.last_judge_reason}[/dim]"
                )
            return SlashResult(handled=True)
        state = "[green]active[/green]" if g.active else "[yellow]paused[/yellow]"
        body = (
            f"[bold]goal:[/bold] {g.text}\n"
            f"  status: {state} · turns {g.turns_used}/{g.budget}"
        )
        if g.last_judge_reason:
            body += f"\n  last judge: [dim]{g.last_judge_reason}[/dim]"
        ctx.console.print(body)
        return SlashResult(handled=True)

    if sub == "pause":
        if db.get_session_goal(ctx.session_id) is None:
            ctx.console.print("[red]no goal set.[/red]")
        else:
            db.update_session_goal(ctx.session_id, active=False)
            ctx.console.print("[yellow]⏸ goal paused.[/yellow]")
        return SlashResult(handled=True)

    if sub == "resume":
        if db.get_session_goal(ctx.session_id) is None:
            ctx.console.print("[red]no goal set.[/red]")
        else:
            db.update_session_goal(
                ctx.session_id,
                active=True,
                turns_used=0,
                clear_last_judge_reason=True,
            )
            ctx.console.print(
                "[green]↻ goal resumed[/green] (turn counter reset to 0)."
            )
        return SlashResult(handled=True)

    if sub == "clear":
        if db.get_session_goal(ctx.session_id) is None:
            ctx.console.print("[dim]no goal to clear.[/dim]")
        else:
            db.clear_session_goal(ctx.session_id)
            ctx.console.print("[green]✗ goal cleared.[/green]")
        return SlashResult(handled=True)

    # SET form — args are the goal text.
    text = " ".join(args).strip()
    if not text:
        ctx.console.print("[red]/goal: empty text[/red]")
        return SlashResult(handled=True)
    db.set_session_goal(ctx.session_id, text=text)
    g = db.get_session_goal(ctx.session_id)
    budget = g.budget if g else 20
    preview = text if len(text) <= 80 else text[:77] + "..."
    ctx.console.print(
        f"[green]⊙ Goal set[/green] ({budget}-turn budget): {preview}\n"
        f"  [dim]check progress with [cyan]/goal status[/cyan][/dim]"
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


def _handle_mcp(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/mcp`` — Claude-Code-parity MCP server panel.

    No args (or ``status``) prints the live status table.
    ``connect <name>`` / ``disconnect <name>`` manage individual servers.
    ``reload`` is an alias for ``/reload-mcp`` (full re-discover).
    """
    sub = args[0].lower() if args else "status"

    if sub in {"status", "list", "ls"}:
        res = ctx.on_mcp_status()
        if not res:
            ctx.console.print(
                "[red]/mcp not wired[/red] — chat loop didn't provide a status callback."
            )
            return SlashResult(handled=True)
        servers = res.get("servers", [])
        connecting = res.get("connecting", [])
        if not servers and not connecting:
            ctx.console.print("[dim]No MCP servers configured.[/dim]")
            return SlashResult(handled=True)
        table = Table(
            title="MCP Servers", show_header=True, header_style="bold"
        )
        table.add_column("Name", style="cyan")
        table.add_column("Status")
        table.add_column("Tools", justify="right")
        table.add_column("Version", style="dim")
        for s in servers:
            state = s.get("connection_state", "?")
            color = {
                "connected": "green",
                "error": "red",
                "disconnected": "dim",
            }.get(state, "")
            tools_count = str(s.get("tool_count", 0))
            ver = s.get("version") or ""
            badge = f"[{color}]{state}[/{color}]" if color else state
            table.add_row(s.get("name", "?"), badge, tools_count, ver)
            err = s.get("last_error")
            if err:
                table.add_row(
                    "", f"  [dim red]{err}[/dim red]", "", ""
                )
        for name in connecting:
            table.add_row(
                name, "[yellow]connecting…[/yellow]", "-", ""
            )
        ctx.console.print(table)
        if connecting:
            ctx.console.print(
                f"[dim]{len(connecting)} server(s) still connecting — "
                f"re-run [bold]/mcp[/bold] to refresh.[/dim]"
            )
        return SlashResult(handled=True)

    if sub == "connect":
        if len(args) < 2:
            ctx.console.print("[red]usage:[/red] /mcp connect <name>")
            return SlashResult(handled=True)
        name = args[1]
        ok, msg = ctx.on_mcp_connect(name)
        color = "green" if ok else "red"
        ctx.console.print(f"[{color}]mcp connect:[/{color}] {msg}")
        return SlashResult(handled=True)

    if sub == "disconnect":
        if len(args) < 2:
            ctx.console.print("[red]usage:[/red] /mcp disconnect <name>")
            return SlashResult(handled=True)
        name = args[1]
        ok, msg = ctx.on_mcp_disconnect(name)
        color = "green" if ok else "red"
        ctx.console.print(f"[{color}]mcp disconnect:[/{color}] {msg}")
        return SlashResult(handled=True)

    if sub == "reload":
        # Full re-discover — same path as /reload-mcp.
        return _handle_reload_mcp(ctx, [])

    ctx.console.print(
        f"[red]unknown subcommand:[/red] /mcp {sub}  "
        f"(try: status / connect <name> / disconnect <name> / reload)"
    )
    return SlashResult(handled=True)


def _handle_debug(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/debug`` — print a sanitized diagnostic dump to console.

    Pulls the LIVE (model, provider) via the standard getter so post-
    ``/model`` swap dumps show the actual running state rather than the
    on-disk YAML default. Crucial for bug reports — without this a user
    debugging a swap issue would see the OLD model in /debug and
    misattribute the bug to the swap silently failing.
    """
    from opencomputer.cli_ui.debug_dump import build_debug_dump

    live_info = _read_active_model_info(ctx)
    # Only forward when the getter actually returned something useful.
    # ``_read_active_model_info`` may return ``("?", "?")`` via the
    # fallback path when both getter AND ctx.config are unavailable;
    # in that case the dump should use its own load_config() rather
    # than display literal "?" rows.
    if live_info[0] and live_info[0] != "?":
        ctx.console.print(build_debug_dump(live_active_model_info=live_info))
    else:
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
        # The /compress handler is "queued" semantics — compaction runs
        # on the next user turn, so before==after is the canonical "no
        # work done yet" reply. Surface the reason verbatim so the user
        # sees ``queued — compaction will run on next user turn``.
        ctx.console.print(f"[dim]{reason}[/dim]")
        return SlashResult(handled=True)
    # 2026-05-11 — PI-style summary card. before/after are MESSAGE
    # counts; the /compress callback doesn't surface token data
    # (compaction runs asynchronously on the next turn), so pass
    # None for the tokens kwargs to omit the row rather than show a
    # misleading "0 → 0". Plain truth: messages compacted, tokens
    # row will appear in the auto-emit card once it ships.
    from opencomputer.cli_ui.summary_cards import render_compaction_card

    card = render_compaction_card(
        messages_before=before,
        messages_after=after,
        tokens_before=None,
        tokens_after=None,
        reason="manual",
    )
    ctx.console.print(card)
    return SlashResult(handled=True)


# ─── Hermes-parity Tier A (2026-04-30): in-session info wrappers ─────


def _handle_config(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/config`` — show key fields of the active Config.

    The ``model`` and ``provider`` rows are read via the live getter
    (``ctx.get_active_model_info``) so they reflect any mid-session
    ``/model`` / ``/provider`` swaps. The other rows (cheap_model,
    max_tokens, temperature, paths) are not yet swappable mid-session
    and stay sourced from ``ctx.config``. If you add a mid-session
    swap for any of those, plumb it through the getter pattern too
    or this row will drift the same way ``model`` did pre-fix
    (2026-05-11).
    """
    cfg = ctx.config
    lines = ["## Active config\n"]
    # Read the live, post-swap (model, provider) tuple. Falls back to
    # the frozen ctx.config snapshot when the getter isn't wired
    # (test fixtures / ACP), per `_read_active_model_info` contract.
    live_model, live_provider = _read_active_model_info(ctx)
    try:
        lines.append(f"  model:       {live_provider} / {live_model}")
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
    """``/cron [list|add|pause|resume|run|remove|help] [args...]`` — Hermes parity.

    Bare ``/cron`` lists jobs (back-compat with the prior read-only
    handler; the prior code referenced a nonexistent
    ``opencomputer.cron.store.CronStore`` and silently failed — fixed
    here). Subcommands wrap :mod:`opencomputer.cron.jobs` directly.
    """
    sub = args[0].lower() if args else "list"
    rest = args[1:]

    try:
        from opencomputer.cron.jobs import (
            create_job,
            get_job,
            list_jobs,
            pause_job,
            remove_job,
            resume_job,
            trigger_job,
        )
        from opencomputer.cron.threats import CronThreatBlocked
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Cron unavailable: {e}[/yellow]")
        return SlashResult(handled=True)

    if sub == "list":
        jobs = list_jobs(include_disabled=("all" in rest or "-a" in rest))
        if not jobs:
            ctx.console.print(
                "[dim]No cron jobs configured. Use `/cron add <schedule> <prompt>`.[/dim]"
            )
            return SlashResult(handled=True)
        lines = [f"## Cron jobs ({len(jobs)})\n"]
        for j in jobs:
            target = (
                j.get("skill")
                or (",".join(j["skills"]) if j.get("skills") else None)
                or (j.get("prompt") or "")[:40]
            )
            lines.append(
                f"  {j['id'][:8]} {j['name'][:30]:<30} "
                f"{j.get('schedule_display', ''):<18} {target}"
            )
        ctx.console.print("\n".join(lines))
        return SlashResult(handled=True)

    if sub == "add":
        if not rest:
            ctx.console.print(
                "[yellow]Usage: /cron add <schedule> [prompt] [--skill name]\n"
                "Examples:\n"
                "  /cron add every 1h --skill blogwatcher\n"
                "  /cron add 'every 1h' Check on the server\n"
                "  /cron add '0 9 * * *' --skill morning-briefing[/yellow]"
            )
            return SlashResult(handled=True)

        # The slash dispatcher whitespace-splits, so multi-token schedules
        # like ``every 1h`` arrive as two tokens. Detect schedule by
        # consuming tokens until the first ``--flag`` or until the prefix
        # parses cleanly as a schedule. This makes ``/cron add every 1h
        # --skill X`` and ``/cron add 0 9 * * * --skill X`` both work.
        from opencomputer.cron.jobs import parse_schedule

        sched_tokens: list[str] = []
        consumed = 0
        for tok in rest:
            if tok.startswith("--"):
                break
            sched_tokens.append(tok)
            consumed += 1
            try:
                parse_schedule(" ".join(sched_tokens))
                break  # prefix parses → that's our schedule
            except ValueError:
                continue  # need more tokens
        if not sched_tokens:
            ctx.console.print("[yellow]Missing schedule[/yellow]")
            return SlashResult(handled=True)
        sched = " ".join(sched_tokens)
        post_sched = rest[consumed:]

        skills: list[str] = []
        prompt_parts: list[str] = []
        i = 0
        while i < len(post_sched):
            tok = post_sched[i]
            if tok == "--skill" and i + 1 < len(post_sched):
                skills.append(post_sched[i + 1])
                i += 2
            else:
                prompt_parts.append(tok)
                i += 1
        prompt_text = " ".join(prompt_parts).strip() or None
        if not prompt_text and not skills:
            ctx.console.print("[yellow]Need either a prompt or --skill name[/yellow]")
            return SlashResult(handled=True)
        try:
            kwargs: dict = {"schedule": sched}
            if skills and len(skills) == 1:
                kwargs["skill"] = skills[0]
            elif skills:
                kwargs["skills"] = skills
            else:
                kwargs["prompt"] = prompt_text
            job = create_job(**kwargs)
        except CronThreatBlocked as e:
            ctx.console.print(f"[red]Blocked: {e}[/red]")
            return SlashResult(handled=True)
        except ValueError as e:
            ctx.console.print(f"[red]Error: {e}[/red]")
            return SlashResult(handled=True)
        ctx.console.print(
            f"[green]✓[/green] Created cron {job['id']} ({job['schedule_display']})"
        )
        return SlashResult(handled=True)

    if sub in ("pause", "resume", "run", "remove"):
        if not rest:
            ctx.console.print(f"[yellow]Usage: /cron {sub} <job_id>[/yellow]")
            return SlashResult(handled=True)
        job_id = rest[0]
        if sub == "pause":
            result = pause_job(job_id)
        elif sub == "resume":
            result = resume_job(job_id)
        elif sub == "run":
            result = trigger_job(job_id)
        else:  # remove
            result = remove_job(job_id)
        if not result:
            ctx.console.print(f"[red]Cron job {job_id!r} not found[/red]")
            return SlashResult(handled=True)
        ctx.console.print(f"[green]✓[/green] /cron {sub} {job_id}")
        return SlashResult(handled=True)

    if sub == "edit":
        # /cron edit <job_id> [--schedule "every 4h"] [--prompt "..."]
        # [--skill X] [--add-skill Y] [--remove-skill Z] [--clear-skills]
        # [--notify telegram:123]
        if not rest:
            ctx.console.print(
                "[yellow]Usage: /cron edit <job_id> [--schedule X] "
                "[--prompt X] [--skill X] [--add-skill X] [--remove-skill X] "
                "[--clear-skills] [--notify X][/yellow]"
            )
            return SlashResult(handled=True)
        from opencomputer.cron.jobs import update_job
        from opencomputer.cron.threats import assert_cron_prompt_safe

        edit_job_id = rest[0]
        existing = get_job(edit_job_id)
        if not existing:
            ctx.console.print(f"[red]Cron job {edit_job_id!r} not found[/red]")
            return SlashResult(handled=True)

        edit_updates: dict = {}
        edit_skills: list[str] = []
        edit_add_skills: list[str] = []
        edit_remove_skills: list[str] = []
        clear_skills = False
        i = 1
        while i < len(rest):
            tok = rest[i]
            if tok == "--schedule" and i + 1 < len(rest):
                # Schedule may span tokens (every 1h, 0 9 * * *) — consume
                # until next --flag or end, then parse.
                j = i + 1
                tokens = []
                while j < len(rest) and not rest[j].startswith("--"):
                    tokens.append(rest[j])
                    j += 1
                edit_updates["schedule"] = " ".join(tokens)
                i = j
            elif tok == "--prompt" and i + 1 < len(rest):
                # --prompt consumes until next --flag.
                j = i + 1
                tokens = []
                while j < len(rest) and not rest[j].startswith("--"):
                    tokens.append(rest[j])
                    j += 1
                try:
                    assert_cron_prompt_safe(" ".join(tokens))
                except CronThreatBlocked as e:
                    ctx.console.print(f"[red]Blocked: {e}[/red]")
                    return SlashResult(handled=True)
                edit_updates["prompt"] = " ".join(tokens)
                i = j
            elif tok == "--skill" and i + 1 < len(rest):
                edit_skills.append(rest[i + 1])
                i += 2
            elif tok == "--add-skill" and i + 1 < len(rest):
                edit_add_skills.append(rest[i + 1])
                i += 2
            elif tok == "--remove-skill" and i + 1 < len(rest):
                edit_remove_skills.append(rest[i + 1])
                i += 2
            elif tok == "--clear-skills":
                clear_skills = True
                i += 1
            elif tok == "--notify" and i + 1 < len(rest):
                edit_updates["notify"] = rest[i + 1] or None
                i += 2
            else:
                ctx.console.print(f"[yellow]Unknown flag: {tok}[/yellow]")
                return SlashResult(handled=True)

        # Apply skill mutations (clear → set → add → remove).
        new_skills = list(
            existing.get("skills") or ([existing["skill"]] if existing.get("skill") else [])
        )
        skill_touched = False
        if clear_skills:
            new_skills = []
            skill_touched = True
        if edit_skills:
            new_skills = list(edit_skills)
            skill_touched = True
        if edit_add_skills:
            for s in edit_add_skills:
                if s not in new_skills:
                    new_skills.append(s)
            skill_touched = True
        if edit_remove_skills:
            new_skills = [s for s in new_skills if s not in set(edit_remove_skills)]
            skill_touched = True
        if skill_touched:
            edit_updates["skills"] = new_skills if new_skills else None
            edit_updates["skill"] = None
            # Mutual exclusion: skill active ⇒ clear stale prompt.
            if new_skills and "prompt" not in edit_updates:
                edit_updates["prompt"] = None
        # Mirror: --prompt active ⇒ clear stale skills.
        if "prompt" in edit_updates and edit_updates["prompt"] and not skill_touched:
            if existing.get("skills") or existing.get("skill"):
                edit_updates["skills"] = None
                edit_updates["skill"] = None

        if not edit_updates:
            ctx.console.print(
                "[yellow]Nothing to update. Pass at least one of "
                "--schedule/--prompt/--skill/etc.[/yellow]"
            )
            return SlashResult(handled=True)

        try:
            updated = update_job(edit_job_id, edit_updates)
        except CronThreatBlocked as e:
            ctx.console.print(f"[red]Blocked: {e}[/red]")
            return SlashResult(handled=True)
        except ValueError as e:
            ctx.console.print(f"[red]Error: {e}[/red]")
            return SlashResult(handled=True)
        if not updated:
            ctx.console.print(f"[red]Cron job {edit_job_id!r} not found[/red]")
            return SlashResult(handled=True)
        ctx.console.print(
            f"[green]✓[/green] Updated cron {updated['id']} "
            f"({updated['schedule_display']})"
        )
        return SlashResult(handled=True)

    if sub in ("help", "?"):
        ctx.console.print(
            "## /cron commands\n"
            "  /cron list [all]                    — show jobs (default)\n"
            "  /cron add <schedule> <prompt>       — create with prompt\n"
            "  /cron add <schedule> --skill X      — create with skill\n"
            "  /cron pause|resume|run|remove <id>  — manage by id\n"
        )
        return SlashResult(handled=True)

    ctx.console.print(
        f"[yellow]Unknown /cron subcommand: {sub!r}. Try /cron help.[/yellow]"
    )
    return SlashResult(handled=True)


def _handle_agents_inline(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/agents [list|kill <id>|help]`` — Hermes parity TUI-overlay-lite.

    Hermes parity (2026-05-08, extended 2026-05-09): the spec mentions a
    TUI overlay with kill/pause controls. This is the slash version:
    bare ``/agents`` prints the live tree (read-only), ``/agents kill <id>``
    cancels a running subagent. Pause is not implemented (Hermes itself
    treats it as advisory; OC's asyncio-task model only supports cancel).
    """
    sub = args[0].lower() if args else "list"
    rest = args[1:]

    try:
        from opencomputer.agent.subagent_registry import SubagentRegistry
        registry = SubagentRegistry.instance()
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Subagent registry unavailable: {e}[/yellow]")
        return SlashResult(handled=True)

    if sub == "kill":
        if not rest:
            ctx.console.print("[yellow]Usage: /agents kill <agent_id>[/yellow]")
            return SlashResult(handled=True)
        target_id = rest[0]
        # Allow ID prefix match for convenience (mirrors `oc agents kill`).
        running = registry.list_running()
        match = next(
            (r for r in running if r.agent_id == target_id or r.agent_id.startswith(target_id)),
            None,
        )
        if match is None:
            ctx.console.print(
                f"[red]No running subagent matches {target_id!r}.[/red] "
                "Try `/agents list` for ids."
            )
            return SlashResult(handled=True)
        ok = registry.kill(match.agent_id)
        if ok:
            ctx.console.print(
                f"[green]✓[/green] Killed subagent {match.agent_id[:8]} "
                f"({match.goal[:40] if match.goal else '?'})"
            )
        else:
            ctx.console.print(
                f"[yellow]Subagent {match.agent_id[:8]} could not be killed "
                "(already finished?)[/yellow]"
            )
        return SlashResult(handled=True)

    if sub in ("help", "?"):
        ctx.console.print(
            "## /agents commands\n"
            "  /agents [list]             — show subagent tree (default)\n"
            "  /agents kill <agent_id>    — cancel a running subagent\n"
        )
        return SlashResult(handled=True)

    if sub != "list":
        ctx.console.print(
            f"[yellow]Unknown /agents subcommand: {sub!r}. Try /agents help.[/yellow]"
        )
        return SlashResult(handled=True)

    try:
        from datetime import UTC, datetime

        running = registry.list_running()
        finished = registry.history(limit=20)
        records = list(running) + list(finished)
    except Exception as e:  # noqa: BLE001
        ctx.console.print(f"[yellow]Subagent registry unavailable: {e}[/yellow]")
        return SlashResult(handled=True)

    if not records:
        ctx.console.print("[dim]No subagents running or recently finished.[/dim]")
        return SlashResult(handled=True)

    # Group by parent_id. None == top-level.
    by_parent: dict[str | None, list] = {}
    for r in records:
        by_parent.setdefault(r.parent_id, []).append(r)

    state_icon = {"running": "▶", "completed": "✓", "failed": "✗", "killed": "⊘"}
    lines = [f"## Subagents ({len(records)})\n"]

    def _emit(rec, depth: int) -> None:
        indent = "  " * depth
        icon = state_icon.get(rec.state, "?")
        if rec.ended_at:
            elapsed = f" ({(rec.ended_at - rec.started_at).total_seconds():.1f}s)"
        elif rec.state == "running":
            elapsed = (
                f" ({(datetime.now(UTC) - rec.started_at).total_seconds():.0f}s)"
            )
        else:
            elapsed = ""
        goal = (rec.goal or "")[:60]
        lines.append(
            f"{indent}{icon} {rec.agent_id[:8]} [{rec.state}]{elapsed}  {goal}"
        )
        for child in by_parent.get(rec.agent_id, []):
            _emit(child, depth + 1)

    for top in by_parent.get(None, []):
        _emit(top, 0)
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
        names = sorted(_treg.names())
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


def _handle_paste(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/paste`` — attach an image from the system clipboard.

    Hermes-parity ("Attach clipboard image from your clipboard"). Pulls
    the image off the OS clipboard via the cross-platform engine in
    :mod:`opencomputer.cli_ui.clipboard`, writes it to a temp PNG, and
    queues it for the next user message through ``on_image_attach`` —
    the same callback ``/image`` uses. Clipboard-only; ``args`` ignored.
    """
    import tempfile
    import time

    from opencomputer.cli_ui import clipboard

    if not clipboard.has_clipboard_image():
        ctx.console.print(
            "[yellow]No image found on the clipboard. "
            "Copy an image first, or use /image <path> for a file.[/yellow]"
        )
        return SlashResult(handled=True)

    ts = time.strftime("%Y%m%d-%H%M%S")
    counter = int(time.time() * 1000) % 100000
    dest = (
        Path(tempfile.gettempdir())
        / "opencomputer-clipboard"
        / f"paste_{ts}_{counter}.png"
    )
    if not clipboard.save_clipboard_image(dest):
        ctx.console.print(
            "[yellow]Couldn't read the image from your clipboard.[/yellow]"
        )
        return SlashResult(handled=True)

    ok, msg = ctx.on_image_attach(str(dest))
    if ok:
        ctx.console.print(f"[green]✓[/green] {msg}")
    else:
        ctx.console.print(f"[yellow]{msg}[/yellow]")
    return SlashResult(handled=True)


def _handle_undo(ctx: SlashContext, args: list[str]) -> SlashResult:
    """``/undo`` — remove the last user/assistant exchange.

    Bridges to the agent ``UndoCommand`` via the ``on_undo`` callback,
    which the chat loop binds to a closure over the live SessionDB.
    """
    # markup=False — the status text is plain (it can carry an exception
    # message), so a stray ``[`` must not be parsed as Rich markup.
    ctx.console.print(ctx.on_undo(), markup=False)
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
    "mcp": _handle_mcp,
    "debug": _handle_debug,
    "compress": _handle_compress,
    "config":   _handle_config,
    "insights": _handle_insights,
    "skills":   _handle_skills_inline,
    "cron":     _handle_cron_inline,
    "agents":   _handle_agents_inline,
    "plugins":  _handle_plugins_inline,
    "profile":  _handle_profile_inline,
    "image":    _handle_image,
    "paste":    _handle_paste,
    "tools":    _handle_tools_inline,
    "retry":    _handle_retry,
    "undo":     _handle_undo,
    "stop":     _handle_stop_bg,
    "reasoning": _handle_reasoning,
    "sources": _handle_sources,
}


def dispatch_slash(
    text: str,
    ctx: SlashContext,
    on_unknown: Callable[[str], SlashResult] | None = None,
) -> SlashResult:
    """Dispatch a slash-command string to its handler.

    Returns ``SlashResult(handled=False)`` for non-slash text so the
    caller can fall back to "treat as normal message".

    A slash command not in the cli_ui registry routes to ``on_unknown``
    when one is supplied — the ``oc chat`` REPL wires this to the agent
    slash registry so commands like ``/copy`` / ``/rollback`` work in
    chat, not only on gateway/wire/ACP. With no ``on_unknown`` hook the
    command is consumed (handled=True) with an error message — we don't
    want a stray slash leaking to the LLM.
    """
    if not is_slash_command(text):
        return SlashResult(handled=False)
    name, args = _split_args(text)
    cmd: CommandDef | None = resolve_command(name)
    # Native System-B handler — the fast path (R2).
    if cmd is not None and cmd.name in _HANDLERS:
        return _HANDLERS[cmd.name](ctx, args)
    # No native handler: try the System-A built-in bridge first (R2 —
    # fixes the KeyError for commands with a CommandDef but no native
    # System-B handler, by running the matching System-A Command with
    # the live RuntimeContext).
    found, output = ctx.on_builtin_dispatch(name, " ".join(args))
    if found:
        if output:
            ctx.console.print(output)
        return SlashResult(handled=True)
    # Still unhandled — fall through to ``on_unknown`` (#639's agent-slash
    # fallthrough, used by ``oc chat`` to reach the agent SlashCommand
    # registry for slashes the System-B registry never saw).
    if on_unknown is not None:
        return on_unknown(text)
    ctx.console.print(f"[red]unknown command:[/red] /{name}  (try /help)")
    return SlashResult(handled=True)


def dispatch_agent_slash_to_console(
    text: str, runtime: RuntimeContext, console: Console
) -> SlashResult:
    """Dispatch ``text`` via the agent slash registry and render the result.

    This is the tested core of the ``oc chat`` REPL fallthrough — the
    ``on_unknown`` hook ``dispatch_slash`` calls for a slash absent from
    the cli_ui registry. It dispatches through the agent ``SlashCommand``
    registry via ``try_dispatch_agent_slash`` (so ``/copy``, ``/rollback``,
    ``/background``, … reach `oc chat`), prints the command's output —
    or an "unknown command" line when no agent command matches either —
    and returns a handled :class:`SlashResult`.

    ``markup=False`` on the output print: an agent command's text is
    plain (it can carry a filename or exception message), so a stray
    ``[`` must not be parsed as Rich markup.
    """
    from opencomputer.agent.slash_commands import try_dispatch_agent_slash

    result = try_dispatch_agent_slash(text, runtime)
    if result is None:
        parts = text.lstrip("/").split(maxsplit=1)
        name = parts[0] if parts else ""
        console.print(f"[red]unknown command:[/red] /{name}  (try /help)")
    elif result.output:
        console.print(result.output, markup=False)
    return SlashResult(handled=True)


# ── user markdown commands (best-of-three Recipe 1) ──────────────────


def _make_markdown_handler(
    md: Any,
) -> Callable[[SlashContext, list[str]], SlashResult]:
    """Build the handler for one discovered markdown command.

    The handler renders the command body (substituting ``{{args}}``) and
    pushes it onto the per-session next-turn queue — the chat outer loop
    drains that queue ahead of stdin, so the rendered prompt runs as the
    very next turn with no extra keypress.
    """
    from opencomputer.agent.markdown_commands import render_command_body

    def _handler(ctx: SlashContext, args: list[str]) -> SlashResult:
        body = render_command_body(md, " ".join(args))
        if not body.strip():
            ctx.console.print(
                f"[yellow]/{md.name}: command body is empty[/yellow]"
            )
            return SlashResult(handled=True)
        if ctx.on_queue_add(body):
            ctx.console.print(f"[dim](/{md.name})[/dim]")
        else:
            ctx.console.print(
                f"[red]/{md.name}: next-turn queue is full[/red]"
            )
        return SlashResult(handled=True)

    return _handler


def install_markdown_commands(
    profile_home: Path,
    *,
    project_cwd: Path | None = None,
) -> list[Any]:
    """Discover user markdown commands and fold them into the registry.

    Called once at chat-session boot. Returns the discovered
    :class:`~opencomputer.agent.markdown_commands.MarkdownCommand` list
    (empty when there are none) so the caller can report a count.
    Idempotent — re-running replaces prior markdown registrations.
    """
    from opencomputer.agent.markdown_commands import (
        discover_markdown_commands,
    )

    cmds = discover_markdown_commands(profile_home, project_cwd=project_cwd)
    if not cmds:
        return []
    defs: list[CommandDef] = []
    for md in cmds:
        defs.append(
            CommandDef(
                name=md.name,
                description=(
                    md.description
                    or f"User markdown command ({md.source_path.name})"
                ),
                category=md.category or "custom",
                args_hint=md.args_hint,
            )
        )
        _HANDLERS[md.name] = _make_markdown_handler(md)
    register_extra_commands(defs)
    return cmds


# ── System-A built-in command sync (best-of-three Recipe 2) ──────────


def sync_builtin_commands() -> list[str]:
    """Surface every System-A built-in command in System B's registry.

    The CLI chat dispatcher (``cli_ui/slash``) and the gateway/wire/ACP
    dispatcher (``agent/slash_commands``) are two registries that have
    drifted: many ``agent/slash_commands_impl`` Command classes are
    registered in System A but were never given a ``CommandDef`` in
    System B, so they did not appear in ``/help`` or autocomplete and
    could not be typed in ``oc chat``.

    This adds a ``CommandDef`` for each System-A command whose name (or
    an alias) does not already resolve in System B — built-ins with a
    native handler keep it, since :func:`dispatch_slash` checks
    ``_HANDLERS`` first. Synced commands have no native handler on
    purpose: :func:`dispatch_slash` falls through to ``on_builtin_dispatch``,
    which runs the real System-A command.

    Returns the list of newly-surfaced command names. Idempotent.
    """
    try:
        from opencomputer.agent.slash_commands import get_registered_commands
    except Exception:  # noqa: BLE001 — never block boot on import drift
        return []

    # Dedup System-A entries: aliases register the same instance under
    # multiple keys, so iterate unique instances by primary name.
    seen: set[str] = set()
    new_defs: list[CommandDef] = []
    for command in get_registered_commands():
        name = getattr(command, "name", None)
        if not name or name in seen:
            continue
        seen.add(name)
        # Skip anything System B already resolves (native command or an
        # alias of one — e.g. System-A `title` is System-B `/rename`).
        if resolve_command(name) is not None:
            continue
        aliases = tuple(
            a for a in getattr(command, "aliases", ()) or () if a
        )
        new_defs.append(
            CommandDef(
                name=name,
                description=(
                    getattr(command, "description", "") or "Built-in command."
                ),
                category=getattr(command, "category", "builtin") or "builtin",
                aliases=aliases,
                args_hint=getattr(command, "args_hint", "") or "",
            )
        )
    if new_defs:
        register_extra_commands(new_defs)
    return [d.name for d in new_defs]
