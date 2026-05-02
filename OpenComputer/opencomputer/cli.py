"""OpenComputer CLI entry point — an actual working chat loop."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import sys
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme as _RichTheme

from opencomputer import __version__
from opencomputer.agent.config import Config, default_config
from opencomputer.agent.config_store import (
    config_file_path,
    get_value,
    load_config,
    save_config,
    set_value,
)
from opencomputer.agent.loop import AgentLoop
from opencomputer.agent.memory_bridge import MemoryBridge
from opencomputer.hooks.engine import engine as hook_engine
from opencomputer.hooks.shell_handlers import make_shell_hook_handler
from opencomputer.observability.logging_config import configure as configure_logging
from opencomputer.plugins.registry import registry as plugin_registry
from opencomputer.tools.ask_user_question import AskUserQuestionTool
from opencomputer.tools.bash import BashTool
from opencomputer.tools.clarify import ClarifyTool
from opencomputer.tools.cron_tool import CronTool
from opencomputer.tools.delegate import DelegateTool
from opencomputer.tools.glob import GlobTool
from opencomputer.tools.grep import GrepTool
from opencomputer.tools.notebook_edit import NotebookEditTool
from opencomputer.tools.push_notification import PushNotificationTool
from opencomputer.tools.python_exec import PythonExec
from opencomputer.tools.read import ReadTool
from opencomputer.tools.recall import RecallTool
from opencomputer.tools.registry import registry
from opencomputer.tools.sessions import SessionsHistory, SessionsList, SessionsStatus
from opencomputer.tools.skill import SkillTool
from opencomputer.tools.skill_manage import SkillManageTool
from opencomputer.tools.voice_synthesize import VoiceSynthesizeTool
from opencomputer.tools.voice_transcribe import VoiceTranscribeTool
from opencomputer.tools.web_fetch import WebFetchTool
from opencomputer.tools.web_search import WebSearchTool
from opencomputer.tools.write import WriteTool
from plugin_sdk import PermissionMode
from plugin_sdk.hooks import HookEvent, HookSpec
from plugin_sdk.runtime_context import RuntimeContext

_DEPRECATION_WARNED: set[str] = set()


def _derive_permission_mode(
    *, plan: bool, auto: bool, accept_edits: bool
) -> PermissionMode:
    """Map the three CLI mode flags onto the canonical ``PermissionMode``.

    Precedence: ``plan > auto > accept-edits > default``. Mirrors the
    pre-existing ``cli.py:879`` rule that "if both set, plan_mode wins".
    """
    if plan:
        return PermissionMode.PLAN
    if auto:
        return PermissionMode.AUTO
    if accept_edits:
        return PermissionMode.ACCEPT_EDITS
    return PermissionMode.DEFAULT


def _emit_yolo_deprecation() -> None:
    """One-shot stderr deprecation warning when --yolo / /yolo is used.

    Fires at most once per process so we don't spam logs when both the CLI
    flag and the slash command alias trigger the warning.
    """
    if "yolo" in _DEPRECATION_WARNED:
        return
    _DEPRECATION_WARNED.add("yolo")
    typer.secho(
        "[deprecated] --yolo / /yolo will be removed in a future release — "
        "use --auto / /auto.",
        fg=typer.colors.YELLOW,
        err=True,
    )

_log = logging.getLogger("opencomputer.cli")

_LOGGING_CONFIGURED = False
"""Sentinel guarding :func:`_configure_logging_once` against duplicate handler
attachment when multiple Typer subcommands run inside a single process
(tests, REPLs)."""


def _build_thinking_callback(forward):
    """Return a callback that forwards each thinking-delta chunk to ``forward``.

    Pulled out as a function so the wiring is testable without spinning
    up a full chat loop. The ``forward`` argument is typically
    ``StreamingRenderer.on_thinking_chunk``.
    """
    def _cb(text: str) -> None:
        forward(text)
    return _cb


def _configure_logging_once() -> None:
    """Wire :mod:`opencomputer.observability.logging_config` once per process.

    Round 2B P-4. ``configure()`` adds rotating file handlers + the
    session-context filter to the ``opencomputer`` / ``opencomputer.gateway``
    / ``opencomputer.errors`` loggers. We must not call it twice in the
    same process — Python's logging module appends handlers without
    de-duplication, so a second call doubles every record.
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    try:
        from opencomputer.agent.config import _home

        configure_logging(_home())
        _LOGGING_CONFIGURED = True
    except Exception as e:  # noqa: BLE001 — logging setup must never crash startup
        _log.warning("logging configuration failed: %s", e)


def _memory_shutdown_atexit() -> None:
    """Drain memory-provider shutdown + fire-and-forget hooks at CLI exit.

    Two responsibilities:

    1. ``MemoryBridge.shutdown_all()`` (II.5 from Hermes parity) — close memory
       provider connections cleanly.
    2. ``hooks.runner.drain_pending()`` (G.5 / Tier 2.6) — await any in-flight
       fire-and-forget hooks (e.g. F1 audit-log writers) before exit so the
       audit chain doesn't develop gaps at SIGTERM time.

    Runs outside any event loop (atexit fires after the last loop closes),
    so this helper spins up a fresh ``asyncio.run`` call. Every exception
    is swallowed — atexit handlers that raise become scary tracebacks for
    users at exit time, and these are best-effort cleanup paths.

    Mirrors Hermes' ``_run_cleanup`` atexit at
    ``sources/hermes-agent/cli.py:717-723``.
    """
    try:
        from opencomputer.hooks.runner import drain_pending

        async def _drain_all() -> None:
            # Drain pending hooks BEFORE memory shutdown so any audit-log
            # writes triggered by hooks land before connections close.
            await drain_pending(timeout=5.0)
            await MemoryBridge.shutdown_all()

        asyncio.run(_drain_all())
    except Exception as e:  # noqa: BLE001 — atexit must never propagate
        _log.debug("atexit cleanup swallowed: %s", e)


# Register once at import time so every CLI subcommand + the gateway
# daemon inherit the hook. ``atexit`` is idempotent across duplicate
# registrations of the same callable, so even if this module is re-
# imported under a test harness we only get one handler.
atexit.register(_memory_shutdown_atexit)


def _apply_profile_override() -> None:
    """Intercept ``-p`` / ``--profile`` from sys.argv and set OPENCOMPUTER_HOME.

    Called from :func:`main` before ``app()`` runs. Stripping must happen
    before Typer parses argv (otherwise Typer flags ``-p`` as an unknown
    option on subcommands). Setting ``OPENCOMPUTER_HOME`` must happen
    before any code calls :func:`opencomputer.agent.config._home` — today
    that's always deferred until a Typer command body runs (module-level
    callers use ``default_factory=lambda: _home() / ...``), so calling
    from ``main()`` is sufficient. Flag beats sticky ``active_profile``
    file beats default root.

    Safe to call multiple times: each call re-derives argv from the
    current ``sys.argv`` and overwrites it in place. Exception handling
    is intentionally narrow — this function MUST NOT crash the CLI; a
    bad profile name falls back to default and the user gets a normal
    error downstream.
    """
    argv = sys.argv
    profile_name: str | None = None
    # Strip -p/--profile flag from argv so Typer doesn't see it as unknown option
    new_argv: list[str] = [argv[0]] if argv else []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-p", "--profile"):
            if i + 1 < len(argv):
                profile_name = argv[i + 1]
                i += 2
            else:
                # -p with no following value: strip the flag, fall back to
                # default. Don't crash — let Typer report any downstream issue
                # cleanly (in practice there's nothing after -p to confuse it).
                i += 1
            continue
        if arg.startswith("--profile="):
            profile_name = arg.split("=", 1)[1]
            i += 1
            continue
        new_argv.append(arg)
        i += 1
    sys.argv = new_argv

    # Normalise empty-string profile (e.g. `--profile=`) to None so the
    # fallback path is explicit rather than a silent falsy pass-through.
    profile_name = profile_name or None

    # No flag + OPENCOMPUTER_HOME unset = consult the sticky active-profile
    # file. Parent-process env var wins when no flag was given.
    if profile_name is None and "OPENCOMPUTER_HOME" not in os.environ:
        try:
            from opencomputer.profiles import read_active_profile

            profile_name = read_active_profile()
        except Exception:
            profile_name = None

    # Explicit flag always wins — even if OPENCOMPUTER_HOME was pre-set in
    # the parent process. Without this, `opencomputer -p coder` would be
    # silently suppressed whenever a parent had OPENCOMPUTER_HOME exported.
    if profile_name and profile_name != "default":
        try:
            from opencomputer.profiles import get_profile_dir

            os.environ["OPENCOMPUTER_HOME"] = str(get_profile_dir(profile_name))
            # NOTE: do NOT mutate HOME / XDG_* in the parent process —
            # that pollutes Path.home() for every in-process consumer
            # (the Jinja system prompt's user_home, snapshot tarball
            # destinations, ~/.local/bin wrapper paths, the workspace
            # walk-up's home guard, identity bootstrap scan roots …).
            #
            # Subprocess HOME-scoping is now done at each spawn boundary
            # via scope_subprocess_env() — see opencomputer/tools/bash.py
            # and opencomputer/mcp/client.py. That gives subprocesses
            # (git/ssh/npm/MCP servers) profile-scoped credentials
            # without polluting the parent.
        except Exception:
            # Invalid profile name (from argv or sticky file) — silently fall
            # back to default. _apply_profile_override MUST NOT crash the CLI.
            pass


app = typer.Typer(
    name="opencomputer",
    help="Personal AI agent framework — plugin-first, self-improving, multi-channel.",
    no_args_is_help=False,
)
# record=True enables /screenshot + /export — Rich keeps a render
# log on the console so console.save_text/save_html/save_svg can
# replay every printed segment. Phase 1 TUI uplift dependency.
#
# Theme override: Rich's default Markdown style applies a dark
# background to inline code spans (`like this`) and code blocks via
# Pygments' monokai theme. On dark terminals that bg shows up as a
# black band that contrasts badly with the surrounding render. We
# override the relevant styles to use foreground color only — no bg.
_OC_THEME = _RichTheme(
    {
        # Inline `code` spans — keep them visually distinct via color
        # but drop the background.
        "markdown.code": "bold cyan",
        "markdown.code_block": "cyan",
        # Block quotes — same treatment, text only.
        "markdown.block_quote": "italic",
    }
)
console = Console(record=True, theme=_OC_THEME)


def _register_builtin_tools() -> None:
    """Register the core bundled tools. Only runs once per process."""
    if "Read" in registry.names():
        return
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(BashTool())
    registry.register(PythonExec())
    registry.register(GrepTool())
    registry.register(GlobTool())
    registry.register(SkillManageTool())
    registry.register(DelegateTool())
    # Phase 10e — web tools
    registry.register(WebFetchTool())
    registry.register(WebSearchTool())
    # Phase 11b — Claude Code parity (core slice)
    registry.register(NotebookEditTool())
    registry.register(SkillTool())
    registry.register(PushNotificationTool())  # CLI mode by default
    registry.register(AskUserQuestionTool())
    # Sub-project 1.G of openclaw-tier1 — narrow disambiguation prompt.
    # Thin wrapper over AskUserQuestion that constrains the agent to
    # supply 2-4 concrete options.
    registry.register(ClarifyTool())
    # Phase 12a — episodic recall + long-term note. Companion to the
    # declarative MemoryTool wired in AgentLoop (10f.D).
    registry.register(RecallTool())
    # Sub-project 1.F-read of the OpenClaw Tier 1 port (2026-04-28) —
    # read-only window into SessionDB. Spawn / Send sub-agent tools are
    # deferred (see plans/2026-04-28-openclaw-tier1-port-AMENDMENTS.md).
    # No capability_claims: read-only local-SQL access matches every
    # other unprivileged tool in the bundle.
    registry.register(SessionsList())
    registry.register(SessionsHistory())
    registry.register(SessionsStatus())
    # G.1 — Cron jobs (Tier 1.1 of Sub-project G — see plan
    # ~/.claude/plans/toasty-wiggling-eclipse.md). Capability-claimed
    # through F1 ConsentGate so the agent can self-schedule with consent.
    registry.register(CronTool())
    # Phase 1.1 of catch-up plan — voice as agent-invocable tools.
    # The opencomputer.voice module already shipped (cost-guarded TTS/STT);
    # these expose it explicitly so the agent can synthesize / transcribe
    # without going through a channel-specific path.
    registry.register(VoiceSynthesizeTool())
    registry.register(VoiceTranscribeTool())
    # Tier 1.B Tools 1+2 — first-class cross-platform send + vision
    # analyze (per docs/refs/hermes-agent/2026-04-28-major-gaps.md).
    # Promotes the MCP messages_send capability + image-paste vision
    # path into core tools so the agent reaches for them by reflex.
    from opencomputer.tools.image_generate import ImageGenerateTool
    from opencomputer.tools.mixture_of_agents import MixtureOfAgentsTool
    from opencomputer.tools.send_message import SendMessageTool
    from opencomputer.tools.vision_analyze import VisionAnalyzeTool
    registry.register(SendMessageTool())
    registry.register(VisionAnalyzeTool())
    registry.register(ImageGenerateTool())
    registry.register(MixtureOfAgentsTool())
    # Phase 2.1 + 2.2 of catch-up plan — GUI tools (macOS only).
    # PointAndClickTool: programmatic mouse click via Quartz/osascript.
    # AppleScriptRunTool: AppleScript snippet runner with denylist guard.
    # Both gated PER_ACTION; only registered on darwin.
    if sys.platform == "darwin":
        from opencomputer.tools.applescript_run import AppleScriptRunTool
        from opencomputer.tools.point_click import PointAndClickTool
        registry.register(PointAndClickTool())
        registry.register(AppleScriptRunTool())

    # PowerShellRun — Windows AppleScriptRun-equivalent. Hard-gates
    # internally to sys.platform == "win32"; safe to register on every
    # platform (returns an error if invoked off Windows).
    from opencomputer.tools.powershell_run import PowerShellRunTool
    registry.register(PowerShellRunTool())

    # DBusCall — Linux desktop AppleScriptRun-equivalent. Hard-gates
    # internally to Linux; safe to register on every platform.
    from opencomputer.tools.dbus_call import DBusCallTool
    registry.register(DBusCallTool())

    # Cross-platform GUI tools — register unconditionally; they self-detect
    # the platform at call time and dispatch to the right backend (Quartz /
    # pyautogui / xdotool / ydotool / osascript / PowerShell).
    from opencomputer.tools.system_click import SystemClickTool
    from opencomputer.tools.system_keystroke import SystemKeystrokeTool
    from opencomputer.tools.system_notify import SystemNotifyTool
    registry.register(SystemClickTool())
    registry.register(SystemKeystrokeTool())
    registry.register(SystemNotifyTool())


def _resolve_plugin_filter():
    """Resolve the active ``enabled_ids`` filter for plugin loading.

    Reads ``profile.yaml`` in the active profile dir (``_home()``),
    walks CWD for a workspace overlay, expands any presets, and returns
    the resulting ``enabled_ids`` argument for ``load_all``. Returns
    ``None`` if anything upstream is missing or malformed, which means
    "load everything" — the safe pre-Phase-14 default.

    Malformed configuration is logged as a warning but never crashes
    startup: a user with a broken ``profile.yaml`` still gets a working
    agent, just without the filtering they asked for.
    """
    import logging

    from opencomputer.agent.config import _home
    from opencomputer.agent.profile_config import (
        ProfileConfigError,
        load_profile_config,
        resolve_enabled_plugins,
    )
    from opencomputer.agent.workspace import find_workspace_overlay

    log = logging.getLogger("opencomputer.cli")

    try:
        profile_cfg = load_profile_config(_home())
    except ProfileConfigError as e:
        log.warning("profile.yaml is malformed — loading all plugins: %s", e)
        return None

    try:
        overlay = find_workspace_overlay()
    except ValueError as e:
        log.warning(
            "workspace .opencomputer/config.yaml is malformed — ignoring overlay: %s",
            e,
        )
        overlay = None

    try:
        resolved = resolve_enabled_plugins(profile_cfg, overlay)
    except FileNotFoundError as e:
        log.warning(
            "profile/overlay references a missing preset — loading all plugins: %s",
            e,
        )
        return None

    if resolved.source:
        log.info("plugin filter: %s", resolved.source)
    if overlay is not None and overlay.source_path is not None:
        log.info("workspace overlay active: %s", overlay.source_path)

    return resolved.enabled


def _discover_plugins() -> int:
    """Discover + load plugins from the canonical search paths. Returns count loaded.

    See ``opencomputer.plugins.discovery.standard_search_paths`` for the
    shared search-order contract (profile-local → global → bundled).
    """
    from opencomputer.plugins.discovery import standard_search_paths

    search_paths = standard_search_paths()
    enabled = _resolve_plugin_filter()
    loaded = plugin_registry.load_all(search_paths, enabled_ids=enabled)
    return len(loaded)


def _apply_model_overrides() -> int:
    """Round 2A P-11 — replay ``model_overrides.yaml`` against the registry.

    Runs AFTER :func:`_discover_plugins` so user-curated entries
    (added via ``opencomputer models add``) win over plugin-shipped
    catalogs. Missing or empty file → 0, no-op. Returns the count of
    entries applied so the chat banner can surface it.

    Errors loading the file are logged inside ``apply_overrides_file``
    and treated as 0 entries — fail-safe per plan.
    """
    from opencomputer.agent.model_metadata import apply_overrides_file

    try:
        return apply_overrides_file()
    except Exception as e:  # noqa: BLE001 — never break startup over overrides
        _log.warning("model_overrides apply failed: %s", e)
        return 0


def _discover_and_register_agents() -> int:
    """III.5 — scan agent-template dirs and register with DelegateTool.

    Runs the same three-tier discovery as :func:`discover_agents` and
    pushes the result onto the class-level template map. Intentionally
    silent on errors (a bad template is logged at WARNING inside the
    discovery helper, never raised) so CLI startup never breaks over a
    malformed frontmatter file.

    Returns the count of registered templates — surfaced in the chat
    banner alongside plugins / MCP counts.
    """
    try:
        from opencomputer.agent.agent_templates import discover_agents
        from opencomputer.plugins.discovery import standard_search_paths

        # Plugin search paths are also the roots whose ``agents/`` dirs
        # we check — matches Claude Code's ``plugins/<id>/agents/*.md``
        # shape (sources/claude-code/plugins/feature-dev/agents/).
        plugin_roots = standard_search_paths()
        templates = discover_agents(plugin_roots=plugin_roots)
    except Exception as e:  # noqa: BLE001 — discovery MUST NOT break CLI startup
        _log.warning("agent template discovery failed: %s", e)
        templates = {}
    DelegateTool.set_templates(templates)
    return len(templates)


def _register_settings_hooks(cfg: Config) -> int:
    """III.6 — register shell-command hooks declared in ``config.yaml``.

    Iterates ``cfg.hooks`` and wraps each :class:`HookCommandConfig` in
    a shell-invoking async handler (see
    :func:`opencomputer.hooks.shell_handlers.make_shell_hook_handler`)
    then registers it against the global hook engine.

    Settings-declared hooks run AFTER plugin-declared hooks because
    plugins call ``api.register_hook`` at plugin-load time (which is
    earlier than this CLI-time call). Coexistence is by design — both
    fire for matching events.

    Invalid ``event`` names are logged at WARNING and skipped, not raised,
    so a single bad entry can't wedge CLI startup. Returns the count
    successfully registered (used by the chat banner).
    """
    if not cfg.hooks:
        return 0
    registered = 0
    for h in cfg.hooks:
        try:
            event = HookEvent(h.event)
        except ValueError:
            _log.warning(
                "settings hook: unknown event %r on command %r; skipping",
                h.event,
                h.command,
            )
            continue
        hook_engine.register(
            HookSpec(
                event=event,
                handler=make_shell_hook_handler(h),
                matcher=h.matcher,
            )
        )
        registered += 1
    return registered


def _resolve_provider(provider_name: str):
    """Resolve a provider by name from the plugin registry.

    Providers are plugins — discovered via plugin.json + activated on demand.
    There is no in-tree fallback: if a provider isn't registered, the user
    needs to install (or enable) the corresponding plugin.
    """
    registered = plugin_registry.providers.get(provider_name)
    if registered is None:
        installed = list(plugin_registry.providers.keys()) or ["none"]
        raise RuntimeError(
            f"Provider '{provider_name}' is not available.\n"
            f"\n"
            f"  Installed: {installed}\n"
            f"\n"
            f"  To fix:\n"
            f"    › Edit ~/.opencomputer/config.yaml and set "
            f"`model.provider` to one of the installed names\n"
            f"    › OR install the missing provider plugin into "
            f"~/.opencomputer/plugins/ or extensions/\n"
            f"    › Run `oc auth` to see which credentials each "
            f"provider expects (e.g. ANTHROPIC_API_KEY, OPENAI_API_KEY)\n"
        )
    # Plugins register the CLASS — instantiate with defaults (reads env vars)
    return registered() if isinstance(registered, type) else registered


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit."),
    headless: bool = typer.Option(
        False, "--headless",
        help=(
            "Force headless mode: no Rich Live, no prompt-toolkit pickers, "
            "no terminal bell. Sets OPENCOMPUTER_HEADLESS=1 for the rest "
            "of the process. Auto-detected from sys.stdin.isatty() when "
            "the flag isn't passed."
        ),
    ),
) -> None:
    if headless:
        os.environ["OPENCOMPUTER_HEADLESS"] = "1"
    if version:
        console.print(f"opencomputer {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        _run_chat_session(resume="", plan=False, no_compact=False, yolo=False)


def _resolve_resume_target(spec: str) -> str | None:
    """Resolve a magic ``--resume`` value to a concrete session id.

    Supports two magic spellings:

    - ``last`` → most-recent session by ``started_at``
    - ``pick`` → interactive prompt listing the last 10 sessions

    Returns the resolved id, or ``None`` when there are no sessions to
    pick from (caller falls back to a fresh session). Reuses
    :meth:`SessionDB.list_sessions` — no duplicate query path.
    """
    from opencomputer.agent.config import default_config
    from opencomputer.agent.state import SessionDB

    cfg = default_config()
    db = SessionDB(cfg.session.db_path)
    rows = db.list_sessions(limit=10)
    if not rows:
        return None
    if spec == "last":
        return str(rows[0]["id"])

    # spec == "pick" — open the polished alt-screen picker (PR #207).
    # Falls back to None if the user cancels (Esc / Ctrl+C).
    from opencomputer.cli_ui.resume_picker import SessionRow, run_resume_picker

    def _coerce_started_at(v) -> float:
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    picker_rows = [
        SessionRow(
            id=str(r.get("id", "")),
            title=r.get("title") or "",
            started_at=_coerce_started_at(r.get("started_at")),
            message_count=int(r.get("message_count", 0) or 0),
        )
        for r in rows
        if r.get("id")
    ]
    return run_resume_picker(picker_rows, db=db)


_STREAM_HOOKS_WIRED = False


def _wire_streaming_renderer_hooks() -> None:
    """Register hook + bus subscriptions that bridge tool-dispatch
    events to the active :class:`StreamingRenderer`.

    Round 5 / Grok-style terminal. Fires once per process. PRE_TOOL_USE
    runs before each dispatch and tells the renderer "tool starting";
    the typed-event bus's ``tool_call`` events tell the renderer
    "tool finished". Both no-op when no renderer is active (e.g.
    non-TTY runs use the plain-stream code path which never enters a
    StreamingRenderer context).
    """
    global _STREAM_HOOKS_WIRED
    if _STREAM_HOOKS_WIRED:
        return
    _STREAM_HOOKS_WIRED = True

    from opencomputer.cli_ui import current_renderer
    from opencomputer.ingestion.bus import default_bus
    from plugin_sdk.hooks import HookEvent, HookSpec

    # Per-call ids so on_tool_end can match its on_tool_start.
    # The dict key is the ToolCall.id (set by the agent loop).
    _tool_idx_by_call_id: dict[str, tuple[str, int]] = {}

    async def _on_pre_tool_use(ctx) -> None:  # type: ignore[no-untyped-def]
        renderer = current_renderer()
        if renderer is None:
            return
        try:
            call = ctx.tool_call
            args_preview = (
                ", ".join(f"{k}={v}" for k, v in (call.arguments or {}).items())
                if call.arguments
                else ""
            )
            idx = renderer.on_tool_start(call.name, args_preview)
            _tool_idx_by_call_id[call.id] = (call.name, idx)
        except Exception as exc:  # noqa: BLE001
            _log.debug("renderer on_tool_start hook failed: %s", exc)

    hook_engine.register(
        HookSpec(event=HookEvent.PRE_TOOL_USE, handler=_on_pre_tool_use)
    )

    def _on_tool_call_complete(event) -> None:
        renderer = current_renderer()
        if renderer is None:
            return
        # ToolCallEvent doesn't carry the original ToolCall.id (it's
        # built fresh from the dispatched call), so we re-derive from
        # the (name, ordering) — last-recorded entry for that name
        # wins. Acceptable for the rendering use case; concurrent
        # dispatches of the SAME tool are rare and the worst-case is
        # one row's status flipping a few hundred ms early.
        try:
            for key, (name, idx) in list(_tool_idx_by_call_id.items()):
                if name == event.tool_name:
                    renderer.on_tool_end(name, idx, ok=event.outcome == "success")
                    _tool_idx_by_call_id.pop(key, None)
                    break
        except Exception as exc:  # noqa: BLE001
            _log.debug("renderer on_tool_end bus subscription failed: %s", exc)

    default_bus.subscribe("tool_call", _on_tool_call_complete)


def _print_update_hint_if_any() -> None:
    """Print the upgrade hint at chat exit when one is ready.

    Hermes parity (``hermes_cli/main.py:4399`` consumes
    ``check_for_updates()`` similarly). We swallow every error so a
    background thread crash, network blip, or PyPI outage can never
    leak into the user's bye message — the worst case is a silently
    skipped hint, which the user will see on the next session.
    """
    try:
        from opencomputer.cli_update_check import get_update_hint

        hint = get_update_hint(timeout=0.2)
        if hint:
            console.print(f"[dim cyan]ℹ {hint}[/dim cyan]")
    except Exception as e:  # noqa: BLE001 — bye message must always succeed
        _log.debug("update-hint print failed: %s", e)


_BUILTIN_PROVIDER_ENV_FALLBACK = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}
"""Last-resort env-var lookup for provider plugins shipped before G.23.

Sub-project G.23 pushes this knowledge into ``plugin.json::setup.providers``.
The fallback dict only fires when discovery fails or the bundled plugin
manifest does not yet declare ``setup.providers`` — keep it minimal so
new providers must self-describe via manifest rather than core."""


def _require_tty(command_name: str) -> None:
    """Exit with a clear stderr message when stdin is not a terminal.

    Ported from hermes-agent's ``hermes_cli/main.py::_require_tty`` —
    interactive wizards that depend on ``input()`` / Rich prompts spin
    or hang when stdin is a pipe. Calling this at the top of a wizard
    entry point catches accidental ``opencomputer setup < something.txt``
    invocations early with a helpful error.
    """
    import sys as _sys

    stdin = getattr(_sys, "stdin", None)
    if stdin is None or not stdin.isatty():
        print(
            f"Error: 'opencomputer {command_name}' requires an interactive terminal.\n"
            f"It cannot be run through a pipe or non-interactive subprocess.\n"
            f"Run it directly in your terminal instead.",
            file=_sys.stderr,
        )
        _sys.exit(1)


def _has_any_provider_configured() -> bool:
    """Return True if at least one provider plugin's primary env var is set.

    Ported from hermes-agent's ``hermes_cli/main.py::
    _has_any_provider_configured``. We check three layers in order:

    1. Process env — what ``os.environ`` knows right now.
    2. Plugin manifests' declared ``setup.providers[*].env_vars`` — so a
       freshly added provider plugin doesn't need a code change here.
    3. Fallback set (``_BUILTIN_PROVIDER_ENV_FALLBACK``) — last resort
       for the bundled providers shipped before G.23 self-description.

    Returns True on the first match. Used to gate the inline first-run
    setup offer in :func:`chat`.
    """
    candidate_env_vars: set[str] = set(_BUILTIN_PROVIDER_ENV_FALLBACK.values())
    candidate_env_vars.add("ANTHROPIC_BASE_URL")
    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths

        for cand in discover(standard_search_paths()):
            setup = cand.manifest.setup
            if setup is None:
                continue
            for prov in setup.providers:
                for env_var in prov.env_vars:
                    if env_var:
                        candidate_env_vars.add(env_var)
    except Exception:  # noqa: BLE001
        pass
    return any(os.environ.get(v) for v in candidate_env_vars)


def _offer_setup_or_exit(reason: str) -> None:
    """Inline first-run helper — print reason, then offer to launch the wizard.

    Mirrors hermes-agent's first-run offer at ``hermes_cli/main.py:1082-1112``.
    Uses raw stdlib ``input()`` (not Rich) so the prompt looks identical
    on every terminal and never hangs on a non-TTY (we short-circuit to
    static guidance + exit 1 in that case). On 'y' / Enter we hand off
    to :func:`opencomputer.setup_wizard.run_setup` and exit cleanly so
    the user re-runs ``opencomputer`` after setup writes config + env.

    Diagnostic / guidance text goes to stderr so a piped stdout (CI,
    ``opencomputer chat | grep …``) doesn't get polluted with the
    error banner.
    """
    import sys as _sys

    print(
        f"\n! {reason} — looks like a first-run install.",
        file=_sys.stderr,
    )
    stdin = getattr(_sys, "stdin", None)
    is_tty = bool(stdin is not None and stdin.isatty())
    if not is_tty:
        print(
            "Run `opencomputer setup` to configure.",
            file=_sys.stderr,
        )
        raise typer.Exit(1)
    try:
        reply = input("Run `opencomputer setup` now? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        reply = "n"
    if reply in ("", "y", "yes"):
        from opencomputer.setup_wizard import run_setup

        run_setup()
        raise typer.Exit(0)
    print(
        "\nYou can run `opencomputer setup` at any time to configure.",
        file=_sys.stderr,
    )
    raise typer.Exit(1)


def _check_provider_key(provider_name: str) -> None:
    """Verify the right env var is set for the configured provider.

    Reads the env-var requirement from the active plugin manifests
    (Sub-project G.23) and falls back to the legacy hard-coded dict
    only when discovery yields nothing — e.g. plugin not installed or
    ``setup.providers`` not yet declared on the manifest.
    """
    key_env: str | None = None
    try:
        from opencomputer.plugins.discovery import (
            discover,
            find_setup_env_vars_for_provider,
            standard_search_paths,
        )

        env_vars = find_setup_env_vars_for_provider(
            provider_name, discover(standard_search_paths())
        )
        if env_vars:
            # Manifest order is canonical — first env var is what setup
            # tools consider the "primary" credential source.
            key_env = env_vars[0]
    except Exception:  # noqa: BLE001
        # Discovery failed (filesystem permission, etc.). Fall through
        # to the hard-coded fallback rather than blocking startup.
        _log.debug("provider env-var discovery failed; using fallback", exc_info=True)
    if key_env is None:
        key_env = _BUILTIN_PROVIDER_ENV_FALLBACK.get(provider_name)
    if key_env and not os.environ.get(key_env):
        _offer_setup_or_exit(f"{key_env} is not set in your environment")


def _run_chat_session(
    *,
    resume: str,
    plan: bool,
    no_compact: bool,
    yolo: bool = False,
    accept_edits: bool = False,
    permission_mode: PermissionMode = PermissionMode.DEFAULT,
) -> None:
    """Shared interactive REPL used by ``chat`` and ``code`` commands.

    V3.A-T7 — extracted from ``chat`` so ``code`` can reuse the full
    setup/loop without copy-paste. ``yolo`` threads through ``RuntimeContext``
    so the consent layer can skip per-action prompts when the user opts in.
    """
    _configure_logging_once()
    if not config_file_path().exists() and not _has_any_provider_configured():
        _offer_setup_or_exit("No OpenComputer config found")
    try:
        from opencomputer.cli_update_check import prefetch_update_check

        prefetch_update_check()
    except Exception as e:  # noqa: BLE001 — update check must never crash startup
        _log.debug("update-check prefetch failed: %s", e)
    # User vision: agent should know about the user before they start.
    # PR #143 shipped the bootstrap orchestrator; this fires it
    # automatically (background, quick mode) on first chat so users
    # don't have to discover ``opencomputer profile bootstrap``.
    try:
        from opencomputer.profile_bootstrap.auto_trigger import (
            kick_off_in_background,
        )

        if kick_off_in_background() is not None:
            console.print(
                "[dim]Building your profile in background "
                "(identity + recent code) — won't interrupt this session.[/dim]"
            )
    except Exception as e:  # noqa: BLE001 — must never crash the chat loop
        _log.debug("auto-bootstrap kick-off failed: %s", e)
    cfg = load_config()
    # Follow-up #25 — one-shot hint if Docker became available after setup.
    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    maybe_print_docker_toggle_hint(cfg)
    _check_provider_key(cfg.model.provider)

    from opencomputer.mcp.client import MCPManager

    _register_builtin_tools()
    _discover_plugins()
    _apply_model_overrides()
    _discover_and_register_agents()
    n_settings_hooks = _register_settings_hooks(cfg)
    provider = _resolve_provider(cfg.model.provider)
    runtime = RuntimeContext(
        plan_mode=plan, yolo_mode=yolo, permission_mode=permission_mode,
    )
    loop = AgentLoop(provider=provider, config=cfg, compaction_disabled=no_compact)
    mcp_mgr = MCPManager(tool_registry=registry)

    # Wire the delegate factory so the model can spawn subagents
    DelegateTool.set_factory(
        lambda: AgentLoop(provider=provider, config=cfg, compaction_disabled=no_compact)
    )
    DelegateTool.set_runtime(runtime)

    # Connect MCP servers synchronously in chat mode (simpler — no event loop yet)
    n_mcp_tools = 0
    if cfg.mcp.servers:
        n_mcp_tools = asyncio.run(
            mcp_mgr.connect_all(
                list(cfg.mcp.servers),
                osv_check_enabled=cfg.mcp.osv_check_enabled,
                osv_check_fail_closed=cfg.mcp.osv_check_fail_closed,
            )
        )

    if resume in ("last", "pick"):
        resume = _resolve_resume_target(resume)
        if resume is None:
            console.print("[dim]No prior sessions to resume; starting fresh.[/dim]")
            resume = ""
    session_id = resume or str(uuid.uuid4())
    # P-4 — bind session id onto the ContextVar so log records emitted
    # during this chat are stamped with it. SessionDB.create_session
    # also stamps when a fresh session is persisted; doing it here too
    # covers the resume path (no DB insert) and any logs between now
    # and the first DB write.
    from opencomputer.observability.logging_config import set_session_id

    set_session_id(session_id)
    # Hermes-style welcome banner (Sub-project F2). Replaces the bare
    # 3-line preamble with categorized tools/skills listing + ASCII art.
    from pathlib import Path as _Path

    from opencomputer.cli_banner import build_welcome_banner

    _banner_home_env = os.environ.get("OPENCOMPUTER_HOME")
    _banner_home = _Path(_banner_home_env) if _banner_home_env else _Path.home() / ".opencomputer"
    build_welcome_banner(
        console,
        model=f"{cfg.model.model} ({cfg.model.provider})",
        cwd=str(_Path.cwd()),
        session_id=session_id,
        home=_banner_home,
    )
    # tools / plugins / agents counts intentionally hidden from the
    # startup banner — they're noise for an interactive session. Run
    # ``opencomputer plugins``, ``opencomputer skills``, etc. to inspect
    # them on demand. Counters still computed above for any callers
    # that depend on n_plugins / n_agents in the same scope.
    if n_settings_hooks:
        console.print(f"[dim]hooks:   {n_settings_hooks} from settings.yaml[/dim]")
    # Banner reflects the canonical effective mode (handles --plan / --auto /
    # --accept-edits / legacy --yolo uniformly via the resolution helper).
    if permission_mode == PermissionMode.PLAN:
        console.print(
            "[bold yellow]plan mode ON[/bold yellow] — destructive tools will be refused"
        )
    elif permission_mode == PermissionMode.AUTO:
        console.print(
            "[bold red]auto mode ON[/bold red] — per-action confirmation prompts skipped"
        )
    elif permission_mode == PermissionMode.ACCEPT_EDITS:
        console.print(
            "[bold blue]accept-edits mode ON[/bold blue] — Edit/Write/MultiEdit/NotebookEdit "
            "auto-approved; Bash and network still prompt"
        )
    if no_compact:
        console.print("[dim]compaction disabled[/dim]")
    if cfg.mcp.servers:
        console.print(
            f"[dim]mcp:     {n_mcp_tools} tool(s) from {len(cfg.mcp.servers)} server(s)[/dim]"
        )
    console.print("[dim]Type 'exit' to quit. Ctrl+C to interrupt.[/dim]\n")

    # Resume mode: render the prior conversation so the user sees what
    # they were doing rather than facing a blank prompt with only a
    # session-id banner. Mirrors Claude Code's `claude --resume` UX.
    # Skipped for fresh sessions (resume falsy → no prior messages).
    if resume:
        from rich.markdown import Markdown as _ResumeMarkdown
        from rich.panel import Panel as _ResumePanel
        from rich.text import Text as _ResumeText

        from opencomputer.agent.state import SessionDB as _ResumeDB

        try:
            _resume_db = _ResumeDB(cfg.session.db_path)
            _resume_msgs = _resume_db.get_messages(session_id)
        except Exception as _e:  # noqa: BLE001 — never crash the chat loop on a resume render hiccup
            _log.warning("resume history render failed: %s", _e)
            _resume_msgs = []

        if _resume_msgs:
            console.print(
                f"[dim]──── prior conversation ({len(_resume_msgs)} message"
                f"{'s' if len(_resume_msgs) != 1 else ''}) ────[/dim]\n"
            )
            for _m in _resume_msgs:
                _content = (_m.content or "").strip()
                if not _content:
                    continue
                if _m.role == "user":
                    console.print(
                        _ResumePanel(
                            _ResumeText(_content, style="bold"),
                            border_style="green",
                            padding=(0, 1),
                            expand=False,
                            title="[bold green]you[/bold green]",
                            title_align="left",
                        )
                    )
                elif _m.role == "assistant":
                    console.print("[bold magenta]oc ›[/bold magenta]")
                    console.print(_ResumeMarkdown(_content, code_theme="ansi_dark"))
                    console.print()
                # tool / system / etc. messages are intentionally skipped —
                # they're noise for the user trying to recall context.
            console.print("[dim]──── continue below ────[/dim]\n")

    # Round 5 — bridge agent loop tool dispatches → StreamingRenderer.
    # PRE_TOOL_USE hook fires on tool start; bus subscription on
    # ToolCallEvent fires on completion. Both check current_renderer()
    # so they no-op when the chat loop isn't actively rendering.
    _wire_streaming_renderer_hooks()

    # Round 5 — Grok-style terminal: live markdown + spinner + tool
    # status + thinking panel + token-rate readout. Falls back to the
    # plain-stream path on non-TTY (Rich.Live escape sequences would
    # pollute a piped stdout).
    from opencomputer.headless import is_headless
    use_live_ui = sys.stdout.isatty() and not is_headless()

    # Phase 1 TUI uplift — closure-captured cumulative token tally so
    # /cost can read it. Mutated (not rebound) inside both _run_turn
    # variants below; no `nonlocal` needed.
    _token_tally = {"in": 0, "out": 0}

    async def _run_turn(user_input: str, images: list[str] | None = None) -> None:
        if not use_live_ui:
            await _run_turn_plain(user_input, images=images)
            return

        from opencomputer.cli_ui import StreamingRenderer

        with StreamingRenderer(console) as renderer:
            renderer.start_thinking()
            import time as _time

            t_start = _time.monotonic()
            result = await loop.run_conversation(
                user_message=user_input,
                session_id=session_id,
                runtime=runtime,
                stream_callback=renderer.on_chunk,
                thinking_callback=_build_thinking_callback(
                    renderer.on_thinking_chunk
                ),
                images=images,
            )
            elapsed = _time.monotonic() - t_start
            _token_tally["in"] += result.input_tokens
            _token_tally["out"] += result.output_tokens
            renderer.finalize(
                reasoning=getattr(result.final_message, "reasoning", None),
                iterations=result.iterations,
                in_tok=result.input_tokens,
                out_tok=result.output_tokens,
                elapsed_s=elapsed,
                show_reasoning=runtime.custom.get("show_reasoning", False),
            )
            # Tier 2.B — terminal bell on turn complete (if /bell on).
            from opencomputer.cli_ui.bell import maybe_emit_bell
            maybe_emit_bell(runtime)

    async def _run_turn_plain(
        user_input: str, images: list[str] | None = None
    ) -> None:
        # Legacy path — kept verbatim so `printf … | opencomputer chat`
        # still produces clean piped output (no Rich Live escapes).
        printed_header = {"val": False}

        def on_chunk(text: str) -> None:
            if not printed_header["val"]:
                console.print("[bold magenta]oc ›[/bold magenta] ", end="")
                printed_header["val"] = True
            console.print(text, end="", markup=False, highlight=False)

        result = await loop.run_conversation(
            user_message=user_input,
            session_id=session_id,
            runtime=runtime,
            stream_callback=on_chunk,
            images=images,
        )
        # Tier 2.B — terminal bell on turn complete (if /bell on).
        from opencomputer.cli_ui.bell import maybe_emit_bell
        maybe_emit_bell(runtime)
        _token_tally["in"] += result.input_tokens
        _token_tally["out"] += result.output_tokens
        if printed_header["val"]:
            console.print()
        if result.final_message.content.strip() and not printed_header["val"]:
            console.print("[bold magenta]oc ›[/bold magenta]")
            console.print(Markdown(result.final_message.content, code_theme="ansi_dark"))
        console.print(
            f"[dim]({result.iterations} iterations · "
            f"{result.input_tokens} in / {result.output_tokens} out)[/dim]\n"
        )

    # Phase 1 TUI uplift — PromptSession + slash dispatch + cancel scope
    # + KeyboardListener for mid-stream ESC. Falls back to legacy line-
    # by-line path on non-TTY (pipes / CI / `printf … | oc chat`).
    from opencomputer.agent.config import _home as _profile_home_fn
    from opencomputer.cli_ui import (
        KeyboardListener,
        SlashContext,
        TurnCancelScope,
        dispatch_slash,
        is_slash_command,
        read_user_input,
    )
    from opencomputer.cli_ui.input_loop import extract_image_attachments
    from opencomputer.cli_ui.paste_folder import PasteFolder

    profile_home = _profile_home_fn()

    # Per-session paste-fold storage. Pastes >5 lines get folded to
    # ``[Pasted text #N +M lines]`` placeholders in the input buffer;
    # full content stored here for submit-time expansion. Reset on /clear.
    paste_folder = PasteFolder()

    # Per-session next-turn prompt buffer for /queue. FIFO, capped to keep
    # a runaway agent from filling memory. Drained one item per outer loop
    # iteration *before* reading from the user — so a queued prompt fires
    # the next turn even when the user hasn't pressed Enter.
    _QUEUE_CAP = 50
    _session_queues: dict[str, list[str]] = {}

    def _on_queue_add(text: str) -> bool:
        q = _session_queues.setdefault(session_id, [])
        if len(q) >= _QUEUE_CAP:
            return False
        q.append(text)
        return True

    def _on_queue_list() -> list[str]:
        return list(_session_queues.get(session_id, []))

    def _on_queue_clear() -> int:
        q = _session_queues.get(session_id, [])
        n = len(q)
        _session_queues[session_id] = []
        return n

    def _on_clear() -> None:
        nonlocal session_id
        session_id = str(uuid.uuid4())
        _token_tally["in"] = 0
        _token_tally["out"] = 0
        # Drop the queue when starting a fresh session — queued prompts
        # were authored against the old session's context.
        _session_queues.pop(session_id, None)
        # Drop folded-paste blobs — placeholder ids reset to #1 on the new session.
        paste_folder.clear()
        console.clear()

    def _on_snapshot_create(label: str | None) -> str | None:
        from opencomputer.snapshot import create_snapshot

        return create_snapshot(profile_home, label=label)

    def _on_snapshot_list() -> list[dict]:
        from opencomputer.snapshot import list_snapshots

        return list_snapshots(profile_home, limit=50)

    def _on_snapshot_restore(snapshot_id: str) -> int:
        from opencomputer.snapshot import restore_snapshot

        return restore_snapshot(profile_home, snapshot_id)

    def _on_snapshot_prune() -> int:
        from opencomputer.snapshot import prune_snapshots

        return prune_snapshots(profile_home)

    def _on_reload() -> dict:
        """Re-read .env + config.yaml. Mutates live cfg in place."""
        out: dict = {"env_keys_changed": 0, "config_changed": False, "error": None}
        try:
            from opencomputer.agent.config_store import load_config

            try:
                from dotenv import dotenv_values, load_dotenv

                env_path = profile_home / ".env"
                if env_path.exists():
                    new_vals = dotenv_values(str(env_path))
                    load_dotenv(str(env_path), override=True)
                    out["env_keys_changed"] = sum(1 for v in new_vals.values() if v is not None)
            except ImportError:
                pass

            cfg_path = profile_home / "config.yaml"
            if cfg_path.exists():
                new_cfg = load_config(cfg_path)
                if new_cfg != cfg:
                    for f in cfg.__dataclass_fields__:
                        setattr(cfg, f, getattr(new_cfg, f))
                    out["config_changed"] = True
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    def _on_reload_mcp() -> dict:
        """Disconnect every MCP server, re-discover, re-register tools."""
        out: dict = {
            "servers_before": 0,
            "servers_after": 0,
            "tools_after": 0,
            "error": None,
        }
        try:
            out["servers_before"] = len(mcp_mgr.connections)
            asyncio.run(mcp_mgr.shutdown())
            servers = getattr(cfg, "mcp", None)
            server_list = list(getattr(servers, "servers", [])) if servers else []
            n = asyncio.run(
                mcp_mgr.connect_all(
                    server_list,
                    osv_check_enabled=getattr(servers, "osv_check_enabled", True) if servers else True,
                    osv_check_fail_closed=getattr(servers, "osv_check_fail_closed", False) if servers else False,
                )
            )
            out["servers_after"] = len(mcp_mgr.connections)
            out["tools_after"] = n
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    def _get_cost_summary() -> dict[str, int]:
        return dict(_token_tally)

    def _get_session_list() -> list[dict]:
        try:
            from opencomputer.agent.state import SessionDB

            db = SessionDB(profile_home / "sessions.db")
            rows = db.list_sessions(limit=20)
            return [
                {"id": r.get("id", "?"), "started_at": r.get("started_at", "?")}
                for r in rows
            ]
        except Exception:
            return []

    def _on_rename(title: str) -> bool:
        """``/rename <title>`` → persist via SessionDB.set_session_title.

        Returns True on success. The auto-titler in
        ``title_generator.maybe_auto_title`` already skips sessions that
        have a title, so manual renames stick.
        """
        try:
            from opencomputer.agent.state import SessionDB

            db = SessionDB(profile_home / "sessions.db")
            db.set_session_title(session_id, title)
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("rename failed: %s", e)
            return False

    def _on_resume(target: str) -> bool:
        """``/resume [last|<id-prefix>|pick]`` → swap active session.

        Mutates ``nonlocal session_id``. Returns False on no-match,
        ambiguous prefix, or DB error. Audit-refined behaviors:
        - Short-circuits when resolved == current session_id.
        - Lists matches when an id-prefix is ambiguous.
        - Post-resume banner shows the session title.
        """
        nonlocal session_id
        try:
            from opencomputer.agent.state import SessionDB

            db = SessionDB(profile_home / "sessions.db")
            if target in ("pick", "last"):
                resolved = _resolve_resume_target(target)
            else:
                rows = db.list_sessions(limit=200)
                matches = [
                    r["id"] for r in rows if r["id"].startswith(target)
                ]
                if len(matches) > 1:
                    console.print(
                        f"[yellow]ambiguous prefix[/yellow] {target!r} "
                        f"matches {len(matches)} sessions:"
                    )
                    for mid in matches[:10]:
                        title = db.get_session_title(mid) or "(untitled)"
                        console.print(f"  [dim]{mid[:8]}[/dim]  {title}")
                    return False
                resolved = matches[0] if matches else None
            if not resolved:
                return False
            if resolved == session_id:
                console.print(
                    "[dim]already on this session — nothing to resume.[/dim]"
                )
                return True
            session_id = resolved
            _token_tally["in"] = 0
            _token_tally["out"] = 0
            title = db.get_session_title(session_id) or "(untitled)"
            console.print(
                f"[bold cyan]resumed →[/bold cyan] {session_id[:8]} "
                f"[dim]({title})[/dim]"
            )
            return True
        except Exception as e:  # noqa: BLE001
            _log.warning("resume failed: %s", e)
            return False

    if not sys.stdin.isatty():
        # Non-TTY (piped stdin) — keep the old line-by-line behavior.
        for line in sys.stdin:
            user_input = line.rstrip("\n")
            if not user_input.strip():
                continue
            if user_input.strip().lower() in {"exit", "quit", ":q"}:
                break
            try:
                asyncio.run(_run_turn(user_input))
            except Exception as e:
                console.print(f"[bold red]error:[/bold red] {type(e).__name__}: {e}")
        _print_update_hint_if_any()
        return

    # Hermes-parity Tier A (2026-04-30) — image queue.
    # Lives across all turns so ``/image <path>`` queues get drained
    # at the top of the NEXT turn. Defined OUTSIDE the while-True
    # loop so both the drain (turn start) and the closure inside
    # SlashContext (mid-turn) reference the same list.
    _image_queue: list[str] = []

    while True:
        # Fetch the session title each turn so a fresh /rename takes effect
        # immediately (the title indicator updates on the very next prompt).
        try:
            from opencomputer.agent.state import SessionDB as _TitleDB

            _title_db = _TitleDB(cfg.session.db_path)
            _current_title = _title_db.get_session_title(session_id) or None
        except Exception:  # noqa: BLE001 — never crash the prompt loop on a title fetch
            _current_title = None

        # Bind ``_current_title`` via default arg so each loop iteration's
        # closure captures *that* iteration's title, not the late-bound
        # outer name (ruff B023).
        async def _read_one(_title: str | None = _current_title) -> str:
            scope = TurnCancelScope()
            return await read_user_input(
                profile_home=profile_home,
                scope=scope,
                session_title=_title,
                paste_folder=paste_folder,
                memory_manager=loop.memory if loop is not None else None,
                runtime=loop._runtime if loop is not None else None,
            )

        # Drain a queued prompt (set via /queue <text>) before prompting
        # the user. FIFO order — oldest queued first. Visible "(queued)"
        # marker so the user knows what's running.
        _q = _session_queues.get(session_id, [])
        if _q:
            user_input = _q.pop(0)
            console.print(f"[dim](queued)[/dim] [bold]{user_input}[/bold]")
        else:
            try:
                user_input = asyncio.run(_read_one())
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]bye.[/dim]")
                _print_update_hint_if_any()
                return
        if not user_input.strip():
            continue

        # Extract image attachments inserted by the BracketedPaste / Ctrl+V
        # handlers as ``[image: /abs/path]`` placeholder tokens. The cleaned
        # text is what the model sees; the path list flows through to
        # ``loop.run_conversation(images=...)`` which sets them on the user
        # Message's ``attachments`` field for the provider to convert into
        # multimodal content blocks.
        # Expand any folded-paste placeholders to their stored full text
        # before we extract image attachments. The LLM sees the full
        # pasted content; the visible buffer kept the compact placeholder.
        user_input = paste_folder.expand_all(user_input)
        # Hermes-parity Tier A (2026-04-30) — drain queued images
        # from any prior ``/image <path>`` slash invocations and
        # prepend them as ``[image: /path]`` tokens that
        # ``extract_image_attachments`` already understands.
        if _image_queue:
            queued_tokens = "".join(f"[image: {p}]" for p in _image_queue)
            user_input = queued_tokens + user_input
            _image_queue.clear()
        cleaned_text, _image_paths = extract_image_attachments(user_input)
        # If the only thing in the input was an image placeholder, give the
        # model a generic prompt so it knows to describe the image.
        if _image_paths and not cleaned_text.strip():
            cleaned_text = "(See attached image.)"

        # Render the user's message inside a green-bordered Panel so it
        # is visually distinct from the assistant's response. PromptSession
        # is configured with erase_when_done=True so the typed prompt line
        # is gone by now; this Panel is the only visible record of the
        # turn input, matching Claude Code's left-bar boundary style.
        from rich.panel import Panel as _UserPanel
        from rich.text import Text as _UserText

        _panel_body_parts: list[_UserText] = []
        if cleaned_text:
            _panel_body_parts.append(_UserText(cleaned_text, style="bold"))
        for _img_path in _image_paths:
            if _panel_body_parts:
                _panel_body_parts.append(_UserText("\n"))
            _panel_body_parts.append(
                _UserText(f"📎 {_img_path}", style="dim cyan")
            )
        _panel_body = _UserText.assemble(*_panel_body_parts) if _panel_body_parts else _UserText(user_input, style="bold")

        console.print(
            _UserPanel(
                _panel_body,
                border_style="green",
                padding=(0, 1),
                expand=False,
                title=_UserText("you", style="bold green"),
                title_align="left",
            )
        )

        if is_slash_command(user_input):
            def _on_model_swap(new_model: str) -> tuple[bool, str]:
                """``/model <id>`` mid-session swap (Sub-project C).

                Resolves alias, mutates ``loop.config`` via dataclasses.replace
                so subsequent turns pick up the new id. AgentLoop reads
                ``self.config.model.model`` per turn (loop.py:1971), so the
                swap takes effect immediately.
                """
                import dataclasses as _dc

                from opencomputer.agent.model_resolver import resolve_model

                aliases = getattr(loop.config.model, "model_aliases", None) or {}
                try:
                    canonical = resolve_model(new_model, aliases)
                except ValueError as e:
                    return (False, str(e))
                if not canonical or not isinstance(canonical, str):
                    return (False, f"invalid model id: {new_model!r}")
                new_model_cfg = _dc.replace(loop.config.model, model=canonical)
                loop.config = _dc.replace(loop.config, model=new_model_cfg)
                return (True, f"swapped to {canonical}")

            def _on_provider_swap(new_provider: str) -> tuple[bool, str]:
                """``/provider <name>`` mid-session swap (Sub-project D).

                Looks up the provider plugin by name, instantiates it
                (provider.__init__ raises if env keys missing), swaps
                ``loop.provider`` AND mutates ``loop.config.model.provider``
                so subsequent calls route through the new provider.
                """
                import dataclasses as _dc

                from opencomputer.agent.provider_swap import lookup_provider

                try:
                    new_prov = lookup_provider(new_provider)
                except (ValueError, RuntimeError) as e:
                    return (False, str(e))
                loop.provider = new_prov
                new_model_cfg = _dc.replace(
                    loop.config.model, provider=new_provider
                )
                loop.config = _dc.replace(loop.config, model=new_model_cfg)
                return (True, f"swapped to {new_provider}")

            def _on_compress() -> tuple[bool, int, int, str]:
                """Hermes-parity (2026-04-30) — flag the next iteration to
                force-compact regardless of token threshold.

                Returns ``(ok, before_count, after_count, reason)``. We
                can't compute before/after counts here because compaction
                runs on the AgentLoop's in-memory message list during the
                NEXT user turn — so "queued" semantics is the honest
                contract. Reports queued-OK with both counts equal so the
                handler emits "queued" rather than fake numbers.
                """
                try:
                    loop.request_force_compaction()
                except Exception as e:  # noqa: BLE001
                    return (False, 0, 0,
                            f"compress unavailable: {type(e).__name__}: {e}")
                return (True, 0, 0,
                        "queued — compaction will run on next user turn")

            def _on_retry() -> tuple[bool, str]:
                """Hermes-parity Tier B (2026-04-30) — re-queue last user msg.

                Reads the most-recent user message from the SessionDB and
                pushes it onto the per-session next-turn queue (same lane
                as ``/queue <text>``). The agent loop's outer wrapper
                drains this queue ahead of stdin, so ``/retry`` causes
                the next iteration to re-enter with the same input.
                """
                try:
                    messages = loop.db.get_messages(session_id)
                except Exception as e:  # noqa: BLE001
                    return (False, f"retry unavailable: {e}")
                last_user = next(
                    (
                        m for m in reversed(messages)
                        if m.role == "user" and isinstance(m.content, str)
                        and m.content.strip()
                    ),
                    None,
                )
                if last_user is None:
                    return (False, "no previous user message to retry")
                content = str(last_user.content)
                ok = _on_queue_add(content)
                if not ok:
                    return (False, "queue full — drain before retrying")
                return (True, content)

            def _on_stop_bg() -> int:
                """Hermes-parity Tier B (2026-04-30) — kill all bg procs.

                Calls into ``extensions/coding-harness/tools/background``
                via lazy-import so the slash command degrades to "0 killed"
                if the coding-harness extension isn't installed.
                """
                try:
                    import asyncio as _asyncio_local

                    from extensions.coding_harness.tools.background import (
                        stop_all_processes,
                    )
                except Exception:  # noqa: BLE001
                    try:
                        # Hyphenated → underscored alternate import
                        # (extensions/coding-harness/tools/background.py).
                        import importlib
                        mod = importlib.import_module(
                            "coding_harness.tools.background",
                        )
                        stop_all_processes = mod.stop_all_processes
                    except Exception:  # noqa: BLE001
                        return 0
                try:
                    return _asyncio_local.run(stop_all_processes())
                except RuntimeError:
                    # Already inside a running loop — schedule and best-effort.
                    loop_inner = _asyncio_local.get_event_loop()
                    fut = _asyncio_local.run_coroutine_threadsafe(
                        stop_all_processes(), loop_inner,
                    )
                    try:
                        return fut.result(timeout=10.0)
                    except Exception:  # noqa: BLE001
                        return 0

            # _image_queue is defined OUTSIDE the while-True loop so it
            # persists across turns (see top of run_chat_session).
            def _on_image_attach(path: str) -> tuple[bool, str]:
                from pathlib import Path as _PathLocal
                p = _PathLocal(path).expanduser()
                if not p.exists():
                    return (False, f"file not found: {p}")
                if not p.is_file():
                    return (False, f"not a file: {p}")
                _image_queue.append(str(p.resolve()))
                return (True, f"queued image for next turn: {p.name}")

            slash_ctx = SlashContext(
                console=console,
                session_id=session_id,
                config=cfg,
                on_clear=_on_clear,
                get_cost_summary=_get_cost_summary,
                get_session_list=_get_session_list,
                on_rename=_on_rename,
                on_resume=_on_resume,
                on_queue_add=_on_queue_add,
                on_queue_list=_on_queue_list,
                on_queue_clear=_on_queue_clear,
                on_snapshot_create=_on_snapshot_create,
                on_snapshot_list=_on_snapshot_list,
                on_snapshot_restore=_on_snapshot_restore,
                on_snapshot_prune=_on_snapshot_prune,
                on_reload=_on_reload,
                on_reload_mcp=_on_reload_mcp,
                on_model_swap=_on_model_swap,
                on_provider_swap=_on_provider_swap,
                on_compress=_on_compress,
                on_retry=_on_retry,
                on_stop_bg=_on_stop_bg,
                on_image_attach=_on_image_attach,
            )
            result = dispatch_slash(user_input, slash_ctx)
            if result.exit_loop:
                if result.message:
                    console.print(f"[dim]{result.message}[/dim]")
                _print_update_hint_if_any()
                return
            continue

        # Run the turn under a cancel scope. ESC during streaming is
        # caught by KeyboardListener; Ctrl+C is caught by the SIGINT
        # handler. Both call scope.request_cancel() which task.cancel()s
        # the in-flight conversation, raising CancelledError that we
        # catch here to print a friendly note.
        async def _run_turn_cancellable(
            input_text: str, images: list[str] | None
        ) -> None:
            scope = TurnCancelScope()
            listener = KeyboardListener(scope)
            with scope.install_sigint_handler():
                listener.start()
                try:
                    await scope.run(_run_turn(input_text, images=images))
                except asyncio.CancelledError:
                    console.print("\n[yellow]turn cancelled.[/yellow]")
                finally:
                    listener.stop()

        try:
            asyncio.run(
                _run_turn_cancellable(cleaned_text, _image_paths or None)
            )
        except Exception as e:
            console.print(f"[bold red]error:[/bold red] {type(e).__name__}: {e}")


@app.command()
def chat(
    action: str | None = typer.Argument(
        None,
        help=(
            "Optional positional verb. ``resume`` opens the picker (same as "
            "``oc resume``); any other value is treated as a session-id prefix "
            "to resume directly."
        ),
    ),
    resume: str = typer.Option(
        "",
        "--resume",
        "-r",
        help=(
            "Resume a session. Pass a session id, or `last` for the most "
            "recent, or `pick` for an interactive picker of the last 10."
        ),
    ),
    plan: bool = typer.Option(
        False, "--plan", help="Plan mode — agent describes actions, refuses destructive tools."
    ),
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Auto mode — skip per-action confirmation prompts (USE WITH CAUTION).",
    ),
    accept_edits: bool = typer.Option(
        False,
        "--accept-edits",
        help="Accept-edits mode — auto-approve Edit/Write/MultiEdit/NotebookEdit; Bash/network still prompt.",
    ),
    yolo: bool = typer.Option(
        False,
        "--yolo",
        help="[deprecated] Alias for --auto.",
    ),
    no_compact: bool = typer.Option(
        False, "--no-compact", help="Disable automatic context compaction (debugging)."
    ),
) -> None:
    """Start an interactive chat session.

    ``oc chat`` starts fresh. ``oc chat resume`` opens the polished
    picker. ``oc chat <id-prefix>`` resumes that session directly.
    """
    if action == "resume":
        # Delegate to the picker flow.
        resume = "pick"
    elif action:
        # Treat as a session-id (or prefix) to resume directly.
        resume = action
    if yolo:
        _emit_yolo_deprecation()
        auto = True
    permission_mode = _derive_permission_mode(plan=plan, auto=auto, accept_edits=accept_edits)
    _run_chat_session(
        resume=resume,
        plan=plan,
        no_compact=no_compact,
        yolo=auto,
        accept_edits=accept_edits,
        permission_mode=permission_mode,
    )


@app.command()
def code(
    path: str | None = typer.Argument(
        None, help="Working directory to start the agent in (defaults to cwd)."
    ),
    resume: str = typer.Option(
        "",
        "--resume",
        "-r",
        help=(
            "Resume a session. Pass a session id, or `last` for the most "
            "recent, or `pick` for an interactive picker of the last 10."
        ),
    ),
    plan: bool = typer.Option(
        False,
        "--plan",
        help="Start in plan mode — agent describes actions, refuses destructive tools.",
    ),
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Auto mode — skip per-action confirmation prompts (USE WITH CAUTION).",
    ),
    accept_edits: bool = typer.Option(
        False,
        "--accept-edits",
        help="Accept-edits mode — auto-approve Edit/Write/MultiEdit/NotebookEdit; Bash/network still prompt.",
    ),
    yolo: bool = typer.Option(
        False,
        "--yolo",
        help="[deprecated] Alias for --auto.",
    ),
    no_compact: bool = typer.Option(
        False, "--no-compact", help="Disable automatic context compaction (debugging)."
    ),
    worktree: bool = typer.Option(
        False,
        "--worktree",
        "-w",
        help=(
            "Spawn a fresh git worktree for this session under "
            "<repo>/.opencomputer-worktrees/<id>/, chdir into it, and "
            "auto-remove on exit. Requires the cwd to be inside a git repo."
        ),
    ),
    keep_worktree: bool = typer.Option(
        False,
        "--keep-worktree",
        help="Do NOT remove the worktree on exit (when --worktree is set).",
    ),
) -> None:
    """Start the coding agent in [path] (or cwd). Snappy entry-point.

    Mirrors ``opencomputer chat`` but is tailored for coding work — Edit,
    MultiEdit, TodoWrite, RunTests etc. are enabled by default. Use
    ``--plan`` for read-only discovery; ``--yolo`` to skip per-action
    confirmation prompts. Use ``--worktree`` to isolate this session in a
    fresh git worktree (auto-removed on exit).
    """
    if path:
        target = os.path.abspath(path)
        if not os.path.isdir(target):
            console.print(f"[bold red]error:[/bold red] not a directory: {target}")
            raise typer.Exit(code=1)
        os.chdir(target)
        console.print(f"[dim]cwd: {target}[/dim]")

    if yolo:
        _emit_yolo_deprecation()
        auto = True
    permission_mode = _derive_permission_mode(plan=plan, auto=auto, accept_edits=accept_edits)

    if worktree:
        from opencomputer.worktree import session_worktree

        with session_worktree(Path.cwd(), keep=keep_worktree) as wt:
            if wt != Path.cwd().parent:  # i.e. the worktree was actually created
                console.print(f"[dim]worktree: {wt}[/dim]")
            _run_chat_session(
                resume=resume,
                plan=plan,
                no_compact=no_compact,
                yolo=auto,
                accept_edits=accept_edits,
                permission_mode=permission_mode,
            )
        return

    _run_chat_session(
        resume=resume,
        plan=plan,
        no_compact=no_compact,
        yolo=auto,
        accept_edits=accept_edits,
        permission_mode=permission_mode,
    )


@app.command()
def resume(
    plan: bool = typer.Option(
        False, "--plan", help="Resume in plan mode."
    ),
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Resume in auto mode (skip per-action confirmation prompts).",
    ),
    accept_edits: bool = typer.Option(
        False,
        "--accept-edits",
        help="Resume in accept-edits mode (auto-approve Edit/Write/MultiEdit/NotebookEdit).",
    ),
    yolo: bool = typer.Option(
        False,
        "--yolo",
        help="[deprecated] Alias for --auto.",
    ),
    no_compact: bool = typer.Option(
        False, "--no-compact", help="Disable automatic context compaction."
    ),
) -> None:
    """Open a full-screen session picker and resume the selected session.

    Equivalent to ``oc chat --resume pick`` but with a polished alt-screen
    picker (search + arrow nav + metadata rows). Alt-screen mode bypasses
    Cursor-Position-Report, so it works in editor terminals (VS Code,
    JetBrains) where the inline dropdown can't render.
    """
    from opencomputer.agent.config import _home as _profile_home_fn
    from opencomputer.agent.state import SessionDB
    from opencomputer.cli_ui.resume_picker import SessionRow, run_resume_picker

    profile_home = _profile_home_fn()
    db = SessionDB(profile_home / "sessions.db")
    db_rows = db.list_sessions(limit=200)

    def _coerce_started_at(v) -> float:
        try:
            return float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    rows = [
        SessionRow(
            id=r.get("id", ""),
            title=r.get("title") or "",
            started_at=_coerce_started_at(r.get("started_at")),
            message_count=int(r.get("message_count", 0) or 0),
        )
        for r in db_rows
        if r.get("id")
    ]
    if not rows:
        console.print("[dim]no sessions yet — start one with `oc chat`.[/dim]")
        return

    selected_id = run_resume_picker(rows, db=db)
    if selected_id is None:
        console.print("[dim]cancelled.[/dim]")
        return

    if yolo:
        _emit_yolo_deprecation()
        auto = True
    permission_mode = _derive_permission_mode(plan=plan, auto=auto, accept_edits=accept_edits)
    _run_chat_session(
        resume=selected_id,
        plan=plan,
        no_compact=no_compact,
        yolo=auto,
        accept_edits=accept_edits,
        permission_mode=permission_mode,
    )


@app.command()
def search(
    query: str = typer.Argument(..., help="Query to search across past sessions."),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Full-text search across saved sessions (FTS5)."""
    from opencomputer.agent.state import SessionDB

    cfg = default_config()
    db = SessionDB(cfg.session.db_path)
    hits = db.search(query, limit=limit)
    if not hits:
        console.print("[dim]no matches[/dim]")
        return
    for h in hits:
        console.print(
            f"[cyan]{h['role']}[/cyan] [dim]({h['session_id'][:8]}…)[/dim]  {h['snippet']}"
        )


@app.command()
def sessions(limit: int = typer.Option(10, "--limit", "-n")) -> None:
    """List recent sessions."""
    from opencomputer.agent.state import SessionDB

    cfg = default_config()
    db = SessionDB(cfg.session.db_path)
    rows = db.list_sessions(limit=limit)
    for r in rows:
        title = r.get("title") or "[untitled]"
        console.print(f"[dim]{r['id'][:8]}…[/dim] msgs={r['message_count']:<3} {title}")


@app.command()
def wire(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(18789, "--port"),
) -> None:
    """Run the wire server — JSON-over-WebSocket API for TUI / IDE / web clients."""
    _configure_logging_once()
    from opencomputer.gateway.wire_server import WireServer

    cfg = load_config()
    # Follow-up #25 — one-shot hint if Docker became available after setup.
    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    maybe_print_docker_toggle_hint(cfg)
    _check_provider_key(cfg.model.provider)

    _register_builtin_tools()
    _discover_plugins()
    _apply_model_overrides()
    _discover_and_register_agents()
    _register_settings_hooks(cfg)

    provider = _resolve_provider(cfg.model.provider)
    loop = AgentLoop(provider=provider, config=cfg)
    DelegateTool.set_factory(lambda: AgentLoop(provider=provider, config=cfg))

    server = WireServer(loop=loop, host=host, port=port)
    console.print(f"[bold cyan]OpenComputer wire server[/bold cyan] — ws://{host}:{port}")
    console.print(f"[dim]model: {cfg.model.model} ({cfg.model.provider})[/dim]")
    console.print("[dim]ctrl+c to stop[/dim]\n")

    async def _run():
        await server.start()
        try:
            await asyncio.Future()  # run forever
        finally:
            await server.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]wire server stopped[/dim]")


@app.command()
def gateway() -> None:
    """Run the gateway daemon — connects all configured channel adapters.

    Requires provider API key + at least one channel token (TELEGRAM_BOT_TOKEN,
    DISCORD_BOT_TOKEN, etc.) in the environment. The same agent loop runs,
    but input comes from channels instead of the terminal.
    """
    _configure_logging_once()
    from opencomputer.gateway.server import Gateway
    from opencomputer.mcp.client import MCPManager

    cfg = load_config()
    # Follow-up #25 — one-shot hint if Docker became available after setup.
    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    maybe_print_docker_toggle_hint(cfg)
    _check_provider_key(cfg.model.provider)

    _register_builtin_tools()
    n_plugins = _discover_plugins()
    _apply_model_overrides()
    _discover_and_register_agents()
    _register_settings_hooks(cfg)

    provider = _resolve_provider(cfg.model.provider)
    loop = AgentLoop(provider=provider, config=cfg)
    DelegateTool.set_factory(lambda: AgentLoop(provider=provider, config=cfg))

    # Connect to MCP servers in the background (kimi-cli deferred pattern)
    mcp_mgr = MCPManager(tool_registry=registry)
    if cfg.mcp.servers:
        console.print(f"[dim]mcp: deferring connection to {len(cfg.mcp.servers)} server(s)[/dim]")

    gw = Gateway(loop=loop, config=cfg.gateway)
    for platform_name, adapter in plugin_registry.channels.items():
        console.print(f"[dim]registering channel:[/dim] [cyan]{platform_name}[/cyan]")
        gw.register_adapter(adapter)

    if not gw.adapters:
        console.print(
            "[bold yellow]warning:[/bold yellow] no channel adapters registered. "
            "Set TELEGRAM_BOT_TOKEN (or another channel token) and ensure the "
            "channel plugin is discovered."
        )
        console.print(f"[dim]plugins loaded: {n_plugins}[/dim]")
        raise typer.Exit(1)

    console.print(
        f"[bold cyan]OpenComputer gateway[/bold cyan] — "
        f"{len(gw.adapters)} channel(s), model={cfg.model.model}"
    )
    console.print("[dim]ctrl+c to stop[/dim]\n")

    async def _run():
        if cfg.mcp.servers:
            asyncio.create_task(
                mcp_mgr.connect_all(
                    list(cfg.mcp.servers),
                    osv_check_enabled=cfg.mcp.osv_check_enabled,
                    osv_check_fail_closed=cfg.mcp.osv_check_fail_closed,
                )
            )
        try:
            await gw.serve_forever()
        finally:
            await mcp_mgr.shutdown()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]gateway stopped[/dim]")


@app.command()
def plugins() -> None:
    """List discovered plugins (metadata only — no activation).

    Uses the canonical search-path order (profile-local → global → bundled).
    Run with ``opencomputer -p <name> plugins`` to see that named profile's
    locally-installed plugins.
    """
    from opencomputer.plugins.discovery import standard_search_paths

    search_paths = standard_search_paths()

    candidates = plugin_registry.list_candidates(search_paths)
    if not candidates:
        from opencomputer.cli_ui.empty_state import render_empty_state

        render_empty_state(
            console=console,
            title="Plugins",
            when_populated=(
                "discovered plugin manifests with id, version, kind, and "
                "description — channel adapters, providers, tools, memory "
                "providers, and bundled extensions."
            ),
            why_empty=(
                "no plugins found in the standard search paths. A fresh "
                "OpenComputer install ships with several bundled plugins "
                "(telegram, anthropic-provider, coding-harness, etc.) — "
                "if you're seeing nothing, the install may be incomplete."
            ),
            next_steps=[
                "[bold]oc doctor[/bold] — diagnoses common install issues",
                "Searched paths:",
                *[f"  [dim]{p}[/dim]" for p in search_paths],
            ],
        )
        return
    for c in candidates:
        m = c.manifest
        console.print(f"[cyan]{m.id}[/cyan] v{m.version} — {m.description or '[no description]'}")
        console.print(f"[dim]  kind: {m.kind}  root: {c.root_dir}[/dim]")


@app.command()
def setup(
    new: bool = typer.Option(
        False,
        "--new",
        help=(
            "Use the Hermes-style section-driven wizard "
            "(opt-in while we port legacy features). Default: legacy."
        ),
    ),
    non_interactive: bool = typer.Option(
        False,
        "--non-interactive",
        help=(
            "Q2: skip all interactive prompts. Sections with existing "
            "config keep their values; unconfigured sections skip with "
            "a default-or-skip behavior. Useful for CI / headless. "
            "Implies --new (legacy wizard does not support this flag)."
        ),
    ),
) -> None:
    """Interactive first-run wizard — pick provider, enter key, test.

    Default: invokes the legacy procedural wizard at
    :func:`opencomputer.setup_wizard.run_setup`.

    With ``--new``: invokes the new section-driven wizard at
    :func:`opencomputer.cli_setup.wizard.run_setup`. The new wizard
    has the Hermes-style arrow-key UX but currently fewer LIVE
    sections than the legacy one — sub-projects M1, S2-S5 etc. close
    the gap. Once parity lands, the default flips to ``--new`` and
    the legacy wrapper is retired.

    With ``--non-interactive`` (implies ``--new``): all prompts are
    skipped — sections with existing config keep their values, fresh
    sections skip without prompting. Useful for CI / scripts.
    """
    if non_interactive or new:
        from opencomputer.cli_setup.wizard import run_setup as run_setup_new
        run_setup_new(non_interactive=non_interactive)
        return
    from opencomputer.setup_wizard import run_setup
    run_setup()


@app.command()
def doctor(
    fix: bool = typer.Option(False, "--fix", help="Invoke plugin-contributed repairs in place."),
) -> None:
    """Diagnose common config/env issues.

    With --fix, every plugin-registered HealthContribution is invoked with
    fix=True and is expected to repair state (e.g. migrate a legacy config
    shape, rewrite broken skill frontmatter) rather than merely report.
    """
    from opencomputer.doctor import run_doctor

    failures = run_doctor(fix=fix)
    if failures:
        raise typer.Exit(1)


@app.command()
def auth() -> None:
    """Show provider credential status — what's configured, what's missing.

    Hermes parity (``hermes auth status``). Read-only summary of every
    provider env var the active plugins declare, plus the proxy hint
    (``ANTHROPIC_BASE_URL``). Echoes only the last 4 characters of each
    set value — never the full token. Cleaner focused view than
    ``opencomputer doctor`` when you just want to answer "did I export
    the right key?".
    """
    candidates: list[tuple[str, str]] = []
    seen_env_vars: set[str] = set()

    def _add(env_var: str, label: str) -> None:
        if env_var and env_var not in seen_env_vars:
            seen_env_vars.add(env_var)
            candidates.append((env_var, label))

    _add("ANTHROPIC_API_KEY", "Anthropic (Claude)")
    _add("ANTHROPIC_BASE_URL", "Anthropic proxy URL (Claude Router etc.)")
    _add("OPENAI_API_KEY", "OpenAI (GPT)")
    try:
        from opencomputer.plugins.discovery import discover, standard_search_paths

        for cand in discover(standard_search_paths()):
            setup = cand.manifest.setup
            if setup is None:
                continue
            for prov in setup.providers:
                label = prov.label or prov.id
                for env_var in prov.env_vars:
                    _add(env_var, label)
    except Exception:  # noqa: BLE001
        pass

    console.print("\n[bold]Provider credentials[/bold]\n")
    for env_var, label in candidates:
        value = os.environ.get(env_var, "")
        if value:
            shown = _redact_for_auth(env_var, value)
            console.print(
                f"  [green]✓[/green] {env_var:<24} [dim]({label})[/dim]  {shown}"
            )
        else:
            console.print(
                f"  [yellow]·[/yellow] {env_var:<24} [dim]({label})[/dim]  not set"
            )
    console.print()


def _redact_for_auth(env_var: str, value: str) -> str:
    """Decide how a credential value is shown by ``opencomputer auth``.

    Two reviewer-driven safeguards over the naive last-4 echo:

    1. **Minimum length before tail-echo (8 chars).** A real Anthropic
       key is ~108 chars, OpenAI ~51, etc. — so 8 is a comfortable
       floor. Anything shorter is treated as "(set)" rather than
       echoing the entire value, which would otherwise leak the whole
       secret on a tiny test fixture or a misconfigured proxy key.
    2. **URL values: scheme://host only, no path / query.** A URL
       env var name (``*_URL``) usually points at a proxy or service
       endpoint and isn't sensitive — but a presigned URL or a URL
       with a token in the path / query string IS sensitive. We strip
       to ``scheme://host`` so the user can verify the host without
       leaking any token-bearing component.
    """
    if env_var.endswith("_URL"):
        from urllib.parse import urlparse

        try:
            parsed = urlparse(value)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
        except ValueError:
            pass
        return "(set)"

    if len(value) >= 8:
        return f"…{value[-4:]}"
    return "(set)"


@app.command(name="model")
def model_pick() -> None:
    """Interactive picker for default provider + model.

    Hermes-parity (2026-04-30). Walks through provider selection and
    model selection then persists choice to ``~/.opencomputer/<profile>/
    config.yaml``. Use ``oc models add`` for non-interactive registration.
    """
    from opencomputer.cli_model_picker import model_picker
    model_picker()


@app.command(name="login")
def login_cmd(
    provider: str = typer.Argument(
        ...,
        help="Provider name (anthropic / openai / groq / openrouter / google / etc.).",
    ),
) -> None:
    """Store an API key for ``provider`` in the active profile's ``.env``."""
    from opencomputer.cli_login import login as _login
    _login(provider)


@app.command(name="logout")
def logout_cmd(
    provider: str = typer.Argument(
        None,
        help="Provider whose stored credential to clear. "
             "If omitted, derives from the currently-active provider.",
    ),
) -> None:
    """Clear the stored API key for the given (or active) provider."""
    from opencomputer.cli_login import logout as _logout
    _logout(provider)


@app.command()
def skills() -> None:
    """List available skills."""
    from opencomputer.agent.memory import MemoryManager

    cfg = default_config()
    mem = MemoryManager(cfg.memory.declarative_path, cfg.memory.skills_path)
    found = mem.list_skills()
    if not found:
        from opencomputer.cli_ui.empty_state import render_empty_state

        render_empty_state(
            console=console,
            title="Skills",
            when_populated=(
                "named recipes the agent can invoke directly — each one a "
                "Markdown file with frontmatter (trigger, description, body)."
            ),
            why_empty=(
                f"no SKILL.md files at {cfg.memory.skills_path}. Skills "
                "ship via plugins (e.g. coding-harness, memory-honcho) or "
                "you can author your own."
            ),
            next_steps=[
                "[bold]oc plugins[/bold] — see installed plugins (most ship skills)",
                "Author a new skill: create `<skills_path>/<id>/SKILL.md` with YAML frontmatter",
            ],
        )
        return
    for s in found:
        console.print(f"[cyan]{s.name}[/cyan] — {s.description}")


# III.5 — subagent template management subcommand.
agents_app = typer.Typer(
    name="agents",
    help="Manage subagent templates (DelegateTool `agent` parameter).",
    no_args_is_help=True,
)
app.add_typer(agents_app, name="agents")


@agents_app.command("list")
def agents_list() -> None:
    """List discovered agent templates.

    III.5 — mirrors Claude Code's ``.md`` agent definitions
    (``sources/claude-code/plugins/<plugin>/agents/*.md``). Scanning
    order is bundled → plugin → profile/user, with later tiers
    overriding earlier entries by name (same precedence as skills).
    """
    from opencomputer.agent.agent_templates import discover_agents
    from opencomputer.plugins.discovery import standard_search_paths

    plugin_roots = standard_search_paths()
    templates = discover_agents(plugin_roots=plugin_roots)
    if not templates:
        console.print("[dim]no agent templates found[/dim]")
        return
    for name in sorted(templates):
        tpl = templates[name]
        tools_str = ", ".join(tpl.tools) if tpl.tools else "(inherit)"
        console.print(
            f"[cyan]{tpl.name}[/cyan] [dim]({tpl.source})[/dim] — {tpl.description}"
        )
        console.print(f"[dim]  tools: {tools_str}[/dim]")
        console.print(f"[dim]  source: {tpl.source_path}[/dim]")


config_app = typer.Typer(
    name="config", help="Manage OpenComputer config (~/.opencomputer/config.yaml)"
)
app.add_typer(config_app, name="config")


# Phase 11c — MCP server management subcommand
from opencomputer.cli_mcp import mcp_app  # noqa: E402

app.add_typer(mcp_app, name="mcp")

# Phase 10f.I — memory CLI subcommand group
from opencomputer.cli_memory import memory_app  # noqa: E402

app.add_typer(memory_app, name="memory")

# 2026-04-28 — `oc help tour` opt-in guided walkthrough
from opencomputer.cli_help import help_app  # noqa: E402

app.add_typer(help_app, name="help")

# Phase 14.M — named plugin-activation presets
from opencomputer.cli_preset import preset_app  # noqa: E402

app.add_typer(preset_app, name="preset")

# Phase 14.B — profile management CLI
from opencomputer.cli_profile import profile_app  # noqa: E402
from opencomputer.cli_profile_analyze import profile_analyze_app  # noqa: E402

profile_app.add_typer(profile_analyze_app, name="analyze")  # Plan 3 (2026-05-01)
app.add_typer(profile_app, name="profile")

# Phase 4 — multi-profile gateway routing rules
from opencomputer.cli_bindings import app as bindings_app  # noqa: E402

app.add_typer(bindings_app, name="bindings")

# Phase 14.E — plugin install/uninstall/where CLI
from opencomputer.cli_plugin import plugin_app  # noqa: E402

app.add_typer(plugin_app, name="plugin")

# Task II.3 — channel directory list CLI
from opencomputer.cli_channels import channels_app  # noqa: E402

app.add_typer(channels_app, name="channels")

# Sub-project F1 — consent grant/revoke/history/verify-chain
from opencomputer.cli_adapter import adapter_app  # noqa: E402
from opencomputer.cli_consent import consent_app  # noqa: E402
from opencomputer.cli_cost import cost_app  # noqa: E402
from opencomputer.cli_cron import cron_app  # noqa: E402
from opencomputer.cli_pair import pair_app  # noqa: E402
from opencomputer.cli_session import session_app  # noqa: E402
from opencomputer.cli_voice import voice_app  # noqa: E402
from opencomputer.cli_webhook import webhook_app  # noqa: E402

app.add_typer(adapter_app, name="adapter")
app.add_typer(consent_app, name="consent")

# ─── service (systemd-user, Linux deployment) ─────────────────────────
service_app = typer.Typer(help="Install/uninstall the systemd user service (Linux).")
app.add_typer(service_app, name="service")


@service_app.command("install")
def _service_install(
    profile: str = typer.Option("default", help="Which profile to run."),
    extra_args: str = typer.Option(
        # 'gateway' (NOT 'chat') is the right default for a service unit:
        # 'chat' is interactive and would exit immediately under systemd
        # (no stdin). 'gateway' is the long-running channel daemon.
        "gateway",
        help=(
            "Args after `opencomputer --headless --profile <p>`. "
            "Default: 'gateway' (long-running channel daemon). "
            "Note: systemd splits on whitespace and does NOT invoke a "
            "shell — args containing spaces are not supported."
        ),
    ),
) -> None:
    """Write and reload a systemd user unit. Run `systemctl --user enable --now opencomputer` after."""
    import shutil as _shutil

    from opencomputer import service as _service_mod

    exe = _shutil.which("opencomputer") or f"{sys.executable} -m opencomputer"
    path = _service_mod.install_systemd_unit(
        executable=exe,
        workdir=str(Path.home()),
        profile=profile,
        extra_args=extra_args,
    )
    typer.echo(f"installed: {path}")
    typer.echo("next: systemctl --user enable --now opencomputer")


@service_app.command("uninstall")
def _service_uninstall() -> None:
    """Stop, disable, and remove the systemd user unit."""
    from opencomputer import service as _service_mod
    path = _service_mod.uninstall_systemd_unit()
    typer.echo(f"removed: {path}" if path else "no unit installed")


@service_app.command("status")
def _service_status() -> None:
    """Report whether the unit is active."""
    from opencomputer import service as _service_mod
    typer.echo("active" if _service_mod.is_active() else "inactive")

app.add_typer(cost_app, name="cost")
app.add_typer(cron_app, name="cron")
app.add_typer(pair_app, name="pair")
app.add_typer(session_app, name="session")
app.add_typer(voice_app, name="voice")
app.add_typer(webhook_app, name="webhook")

# Hermes channel-port (PR 5.4) — Telegram DM Topics CLI
from opencomputer.cli_telegram import telegram_app  # noqa: E402

app.add_typer(telegram_app, name="telegram")

# Sub-project F1 2.B.4 — audit-log viewer (`audit show` / `audit verify`)
from opencomputer.cli_audit import audit_app  # noqa: E402

app.add_typer(audit_app, name="audit")

# Phase 3.G — prompt-injection instruction-detector CLI
from opencomputer.cli_security import security_app  # noqa: E402

app.add_typer(security_app, name="security")

# Phase 3.E — pluggable sandbox strategy CLI
from opencomputer.cli_sandbox import sandbox_app  # noqa: E402

app.add_typer(sandbox_app, name="sandbox")

# PR-1 — evolution self-improvement CLI
from opencomputer.evolution.entrypoint import evolution_app  # noqa: E402

app.add_typer(evolution_app, name="evolution")

# Tier-A item 9 — Skills Guard CLI (`opencomputer skill scan <path>`)
from opencomputer.cli_skills import skill_app  # noqa: E402

app.add_typer(skill_app, name="skill")

# Tier-A item 11 — per-tool insights CLI (`opencomputer insights`)
from opencomputer.cli_insights import insights_app  # noqa: E402

app.add_typer(insights_app, name="insights")

# Tier-B item 23 — detached task management CLI
from opencomputer.cli_task import task_app  # noqa: E402

app.add_typer(task_app, name="task")

# Phase 3.F — autonomous full-system-control mode toggle
from opencomputer.cli_system_control import system_control_app  # noqa: E402

app.add_typer(system_control_app, name="system-control")

# Phase 3.B — behavioral inference engine + motif store CLI
from opencomputer.cli_inference import inference_app  # noqa: E402

app.add_typer(inference_app, name="inference")

# Phase 3.C — user-model graph + context weighting (F4 layer)
from opencomputer.cli_user_model import user_model_app  # noqa: E402

app.add_typer(user_model_app, name="user-model")

# Round 2A P-11 — `models add` curated model-metadata registry
from opencomputer.cli_models import models_app  # noqa: E402

app.add_typer(models_app, name="models")

# V2.C-T3 — Layered Awareness controls (patterns + personas)
from opencomputer.cli_awareness import awareness_app  # noqa: E402

app.add_typer(awareness_app, name="awareness")

# Ambient foreground sensor — opt-in, hashed-app-id event publisher
from opencomputer.cli_ambient import app as ambient_app  # noqa: E402

app.add_typer(ambient_app, name="ambient")

# Auto-skill-evolution — `opencomputer skills {list,review,accept,reject,evolution}`
# Sibling to the singular `skill` namespace already mounted above.
from opencomputer.cli_skills import app as skills_app  # noqa: E402

app.add_typer(skills_app, name="skills")


@config_app.command("show")
def config_show() -> None:
    """Print current effective config (defaults + overrides from disk)."""
    import yaml

    from opencomputer.agent.config_store import _to_yaml_dict

    cfg = load_config()
    console.print(yaml.safe_dump(_to_yaml_dict(cfg), default_flow_style=False, sort_keys=False))


@config_app.command("get")
def config_get(key: str = typer.Argument(..., help="Dotted key, e.g. model.provider")) -> None:
    """Get a single config value by dotted key."""
    cfg = load_config()
    try:
        value = get_value(cfg, key)
    except KeyError as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        raise typer.Exit(1) from None
    console.print(str(value))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Dotted key, e.g. model.provider"),
    value: str = typer.Argument(..., help="New value"),
) -> None:
    """Set a config value and persist to ~/.opencomputer/config.yaml."""
    cfg = load_config()
    # Attempt to coerce numeric / bool / path values sensibly
    coerced: object = value
    if value.lower() in {"true", "false"}:
        coerced = value.lower() == "true"
    else:
        try:
            coerced = int(value)
        except ValueError:
            try:
                coerced = float(value)
            except ValueError:
                coerced = value
    try:
        new_cfg = set_value(cfg, key, coerced)
    except KeyError as e:
        console.print(f"[bold red]error:[/bold red] {e}")
        raise typer.Exit(1) from None
    save_config(new_cfg)
    console.print(f"[green]✓[/green] {key} = {coerced!r}")
    console.print(f"[dim]saved to {config_file_path()}[/dim]")


@config_app.command("path")
def config_path() -> None:
    """Print the path to the config file."""
    console.print(str(config_file_path()))


@config_app.command("edit")
def config_edit() -> None:
    """Open the active profile's config.yaml in $VISUAL / $EDITOR.

    Hermes parity (``hermes config edit`` — referenced from
    ``sources/hermes-agent-2026.4.23/hermes_cli/setup.py:2207``). Picks
    ``$VISUAL`` first (POSIX convention for the user's "real" editor),
    then ``$EDITOR``, then ``vi`` as a final fallback. Refuses with a
    pointer to ``opencomputer setup`` when no config exists yet — better
    than dropping the user into an empty buffer they have to remember
    every config key for.
    """
    import subprocess

    cfg_path = config_file_path()
    if not cfg_path.exists():
        console.print(
            f"[bold red]error:[/bold red] no config at [dim]{cfg_path}[/dim]\n"
            f"[dim]Run [bold]opencomputer setup[/bold] to create one.[/dim]"
        )
        raise typer.Exit(1)

    import shlex

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    # Reviewer fix #2: shlex.split so $EDITOR values like ``code -w``,
    # ``emacs -nw``, ``subl -w``, ``nvim -p`` work — the canonical POSIX
    # $EDITOR gotcha. Without this we'd pass the entire string as
    # argv[0] and FileNotFoundError on the missing literal binary.
    editor_argv = shlex.split(editor)
    if not editor_argv:
        editor_argv = ["vi"]
    try:
        from opencomputer.profiles import read_active_profile, scope_subprocess_env

        editor_env = scope_subprocess_env(
            os.environ.copy(), profile=read_active_profile()
        )
    except Exception:  # noqa: BLE001 — fail-soft: parent env if profile lookup fails
        editor_env = None
    try:
        result = subprocess.run(
            [*editor_argv, str(cfg_path)], check=False, env=editor_env
        )
    except FileNotFoundError as exc:
        console.print(
            f"[bold red]error:[/bold red] editor '{editor_argv[0]}' not found "
            f"({exc.strerror}).\n"
            f"[dim]Set $EDITOR to a command on your PATH.[/dim]"
        )
        raise typer.Exit(1) from None

    if result.returncode != 0:
        console.print(
            f"[yellow]![/yellow] editor exited with status {result.returncode}"
        )
        raise typer.Exit(result.returncode)


# III.3 — bundled settings variants. Mirrors sources/claude-code/examples/
# settings/README.md: three starter postures users copy and adjust.


def _variants_dir() -> Path:
    """Return the directory holding bundled variant YAMLs (III.3)."""
    return Path(__file__).parent / "settings_variants"


def _available_variants() -> list[str]:
    """Discover bundled variants by scanning ``*.yaml`` in :func:`_variants_dir`."""
    d = _variants_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def _variant_description(variant_path: Path) -> str:
    """Extract the first non-blank comment block from a variant YAML.

    Returns a single-line summary for the ``config variants`` listing.
    Fails open — unreadable / missing header yields an empty string so
    the command never crashes on a malformed variant.
    """
    try:
        lines: list[str] = []
        for raw in variant_path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped.startswith("#"):
                if lines:
                    break  # first non-comment line ends the header block
                continue
            body = stripped.lstrip("#").strip()
            # Skip the banner line ("OpenComputer Settings — LAX variant") —
            # it's redundant with the variant name we already print.
            if not body or body.lower().startswith("opencomputer settings"):
                continue
            lines.append(body)
            if len(lines) >= 2:
                break
        return " ".join(lines)
    except OSError:
        return ""


@config_app.command("variants")
def config_variants() -> None:
    """List the bundled settings variants (III.3).

    Each variant ships a starter ``config.yaml`` with a distinct security
    posture (see ``sources/claude-code/examples/settings/README.md`` for the
    inspiration). Use ``opencomputer config init --variant <name>`` to
    copy one into the active profile.
    """
    names = _available_variants()
    if not names:
        console.print("[yellow]no bundled variants found[/yellow]")
        return
    console.print("[bold]Bundled settings variants:[/bold]")
    for name in names:
        desc = _variant_description(_variants_dir() / f"{name}.yaml") or "(no description)"
        console.print(f"  [cyan]{name}[/cyan] — {desc}")
    console.print(
        "\n[dim]copy one into the active profile with "
        "[bold]opencomputer config init --variant <name>[/bold][/dim]"
    )


@config_app.command("init")
def config_init(
    variant: str = typer.Option(..., "--variant", help="lax | strict | sandbox"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing config.yaml"),
) -> None:
    """Initialize the active profile's config.yaml from a bundled variant.

    III.3 — pairs with Claude Code's
    ``sources/claude-code/examples/settings/README.md`` examples. The copied
    file is re-parsed via :func:`load_config` as a smoke test; a variant that
    fails to round-trip triggers a rollback so the user isn't left with a
    broken ``config.yaml``.
    """
    names = _available_variants()
    src = _variants_dir() / f"{variant}.yaml"
    if variant not in names or not src.is_file():
        available = ", ".join(names) if names else "(none)"
        console.print(
            f"[bold red]error:[/bold red] unknown variant {variant!r}. "
            f"Available: {available}"
        )
        raise typer.Exit(1)

    dst = config_file_path()
    backup: Path | None = None
    if dst.exists():
        if not force:
            console.print(
                f"[bold red]error:[/bold red] config.yaml already exists at {dst}, "
                "re-run with --force to overwrite"
            )
            raise typer.Exit(1)
        backup = dst.with_suffix(dst.suffix + ".bak")
        try:
            backup.write_bytes(dst.read_bytes())
        except OSError as e:
            console.print(f"[bold red]error:[/bold red] could not back up {dst}: {e}")
            raise typer.Exit(1) from None

    dst.parent.mkdir(parents=True, exist_ok=True)
    content = src.read_text(encoding="utf-8")
    dst.write_text(content, encoding="utf-8")

    # Sanity-check: the freshly copied file must parse. If it doesn't,
    # roll back (restore the backup or delete the new file) so the user is
    # never stranded with a broken config.
    try:
        load_config(dst)
    except Exception as e:  # noqa: BLE001 — we always want to roll back
        if backup is not None:
            try:
                dst.write_bytes(backup.read_bytes())
            except OSError:
                pass
        else:
            try:
                dst.unlink()
            except OSError:
                pass
        console.print(
            f"[bold red]error:[/bold red] variant {variant!r} failed to parse after copy: {e}"
        )
        raise typer.Exit(1) from None

    if backup is not None:
        # Keep the backup only when --force replaced an existing file, and
        # only as a one-shot safety net; we clean it up on success to avoid
        # accumulating .bak crumbs on repeated re-inits.
        try:
            backup.unlink()
        except OSError:
            pass

    console.print(f"[green]✓[/green] initialized config.yaml from variant [cyan]{variant}[/cyan]")
    console.print(f"[dim]  → {dst}[/dim]")


# Phase 11d: episodic memory recall + Anthropic batch runner.


@app.command()
def recall(
    query: str = typer.Argument(..., help="Search across episodic memory."),
    limit: int = typer.Option(10, "--limit", "-n"),
) -> None:
    """Search past turns by what happened — files touched, tools used, gist.

    Episodic memory is the third pillar (declarative + procedural + episodic).
    Each completed turn writes one event; this command retrieves them via FTS5.
    """
    from opencomputer.agent.state import SessionDB

    cfg = default_config()
    # Follow-up #25 — one-shot hint if Docker became available after setup.
    from opencomputer.cli_hints import maybe_print_docker_toggle_hint

    maybe_print_docker_toggle_hint(cfg)
    db = SessionDB(cfg.session.db_path)
    hits = db.search_episodic(query, limit=limit)
    if not hits:
        from opencomputer.cli_ui.empty_state import render_empty_state

        total = db.count_sessions() if hasattr(db, "count_sessions") else 0
        if total == 0:
            why = (
                "you haven't run any sessions yet — the episodic store "
                "starts populating after the first conversation."
            )
        else:
            why = (
                f"no episodic events match {query!r}. You have "
                f"{total} session(s) on record but none of them touched "
                "this topic. Try a broader query or related keywords."
            )
        render_empty_state(
            console=console,
            title="Episodic recall",
            when_populated=(
                "matching past turns from any session — the gist of what "
                "happened, files touched, tools used."
            ),
            why_empty=why,
            next_steps=[
                "[bold]oc sessions[/bold] — list recent sessions to find a topic",
                "[bold]oc search <text>[/bold] — full-text search across raw messages",
            ],
        )
        return
    for h in hits:
        tools = f" [dim]tools:[/dim] {h['tools_used']}" if h.get("tools_used") else ""
        files = f" [dim]files:[/dim] {h['file_paths']}" if h.get("file_paths") else ""
        console.print(
            f"[cyan]{h['session_id'][:8]}…/turn-{h['turn_index']}[/cyan]"
            f"  {h['summary']}{tools}{files}"
        )


@app.command()
def steer(
    prompt: str = typer.Argument(..., help="The mid-run nudge text to inject."),
    session_id: str = typer.Option(
        "", "--session-id", "-s",
        help="Target session id. Required when reaching a remote wire server "
        "or when the local registry holds multiple sessions.",
    ),
    wire_url: str = typer.Option(
        "", "--wire-url",
        help="Optional ws://host:port — if set, submit via wire RPC instead "
        "of writing to the in-process registry.",
    ),
) -> None:
    """Submit a mid-run /steer nudge for an active session.

    Round 2a P-2. Latest-wins: if a nudge is already pending for the
    target session, it is replaced (the wire server response surfaces
    a ``had_pending`` flag so you know your nudge overrode a previous
    one). The agent loop consumes the nudge between turns — after the
    current tool dispatch finishes, before the next LLM call.

    Two modes:

    * ``--wire-url ws://127.0.0.1:18789`` — submit via JSON-RPC to a
      running wire server (the standard case when the agent is hosted
      on a separate process).
    * No ``--wire-url`` — write directly into the in-process
      :data:`opencomputer.agent.steer.default_registry`. Useful for
      tests, scripts, or `opencomputer chat` running in a sibling
      thread.
    """
    if not prompt.strip():
        console.print("[bold red]error:[/bold red] prompt must be non-empty")
        raise typer.Exit(1)

    if wire_url:
        if not session_id:
            console.print(
                "[bold red]error:[/bold red] --session-id is required when "
                "--wire-url is set"
            )
            raise typer.Exit(1)
        # Fire a single steer.submit call against the wire server and
        # exit. We deliberately don't keep the connection open — the
        # nudge is one-shot.
        import json as _json
        import uuid as _uuid

        import websockets

        from opencomputer.gateway.protocol import METHOD_STEER_SUBMIT

        async def _submit() -> dict:
            async with websockets.connect(wire_url) as ws:
                await ws.send(
                    _json.dumps(
                        {
                            "type": "req",
                            "id": str(_uuid.uuid4()),
                            "method": METHOD_STEER_SUBMIT,
                            "params": {
                                "session_id": session_id,
                                "prompt": prompt,
                            },
                        }
                    )
                )
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                return _json.loads(raw)

        try:
            data = asyncio.run(_submit())
        except Exception as e:  # noqa: BLE001
            console.print(
                f"[bold red]error:[/bold red] wire submit failed: "
                f"{type(e).__name__}: {e}"
            )
            raise typer.Exit(1) from None

        if not data.get("ok"):
            console.print(
                f"[bold red]error:[/bold red] {data.get('error', 'unknown')}"
            )
            raise typer.Exit(1)

        payload = data.get("payload") or {}
        if payload.get("had_pending"):
            console.print(
                "[yellow]steer override:[/yellow] previous nudge discarded "
                f"for session {session_id}"
            )
        console.print(
            f"[green]steer queued[/green] for session "
            f"[cyan]{session_id}[/cyan] ({payload.get('queued_chars', 0)} chars)"
        )
        return

    # Standalone / in-process registry path. Useful in tests and when
    # the user runs `opencomputer chat` and `opencomputer steer` in
    # sibling threads inside the same process. The session_id is
    # optional but strongly recommended — without it, this is a no-op
    # (the registry is keyed by session_id).
    from opencomputer.agent.steer import default_registry as _steer_registry

    if not session_id:
        console.print(
            "[bold red]error:[/bold red] --session-id is required for the "
            "in-process path (otherwise the registry has no key to write to)"
        )
        raise typer.Exit(1)

    had_pending = _steer_registry.has_pending(session_id)
    _steer_registry.submit(session_id, prompt)
    if had_pending:
        console.print(
            "[yellow]steer override:[/yellow] previous nudge discarded "
            f"for session {session_id}"
        )
    console.print(
        f"[green]steer queued[/green] for session "
        f"[cyan]{session_id}[/cyan] ({len(prompt)} chars)"
    )


@app.command(name="acp")
def acp_serve() -> None:
    """Start the Agent Client Protocol server over stdio.

    OpenComputer becomes the agent backend for ACP-aware IDEs (Zed,
    VS Code with the ACP extension, Cursor, Claude Desktop).

    PR-D of ~/.claude/plans/replicated-purring-dewdrop.md.
    See docs/acp.md for IDE setup instructions.
    """
    import asyncio as _asyncio

    from opencomputer.acp import ACPServer

    server = ACPServer()
    _asyncio.run(server.serve_stdio())


@app.command()
def batch(
    input_path: str = typer.Argument(..., help="Path to JSONL with one prompt per line."),
    output_path: str = typer.Option(
        "batch-results.jsonl", "--output", "-o", help="Where to write results JSONL."
    ),
    poll_interval: float = typer.Option(
        30.0, "--poll-interval", help="Seconds between status polls."
    ),
) -> None:
    """Submit prompts to Anthropic's batch API; write results to JSONL.

    Input format (one JSON object per line):
        {"id": "req-1", "prompt": "...", "system": "...", "model": "..."}

    Only `prompt` is required. `id` defaults to req-N. `system` and `model`
    fall back to defaults. ANTHROPIC_API_KEY must be set.
    """
    from pathlib import Path as _Path

    from opencomputer.batch import run_batch_end_to_end

    in_path = _Path(input_path)
    out_path = _Path(output_path)
    if not in_path.exists():
        console.print(f"[bold red]error:[/bold red] input file not found: {in_path}")
        raise typer.Exit(1)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        from opencomputer.cli_ui.empty_state import render_failure_with_teach

        render_failure_with_teach(
            console=console,
            error="ANTHROPIC_API_KEY not set",
            feature_name="oc batch",
            feature_purpose=(
                "submits prompts to Anthropic's batch API and writes "
                "results to JSONL — uses the same API key as oc chat"
            ),
            fixes=[
                "export ANTHROPIC_API_KEY=sk-ant-...",
                "Or run [bold]oc auth[/bold] to see what credentials are configured",
                "Batch is Anthropic-only today — OpenAI batch isn't wired",
            ],
        )
        raise typer.Exit(1)

    def _on_status(status: str) -> None:
        console.print(f"[dim]batch status: {status}[/dim]")

    try:
        final_status, n = asyncio.run(
            run_batch_end_to_end(
                in_path,
                out_path,
                interval_s=poll_interval,
                on_status=_on_status,
            )
        )
    except Exception as e:  # noqa: BLE001
        console.print(f"[bold red]error:[/bold red] {type(e).__name__}: {e}")
        raise typer.Exit(1) from None
    console.print(f"[green]✓[/green] batch finished ({final_status}) — {n} result(s) → {out_path}")


def _apply_loose_env_perms_flag() -> None:
    """Intercept ``--allow-loose-env-perms`` from sys.argv (Round 2B P-16).

    Strips the flag from argv before Typer parses it (otherwise every
    subcommand would have to declare the option) and flips the
    process-wide flag consumed by
    :func:`opencomputer.security.env_loader.load_env_file`. The override
    deliberately requires explicit opt-in: a user who edits a ``.env``
    file with the wrong umask will see a clear refusal at first load,
    and can pass this flag to override after auditing the file.

    Safe to call multiple times. Re-derives argv from ``sys.argv`` and
    overwrites it in place.
    """
    argv = sys.argv
    if not argv:
        return
    new_argv: list[str] = [argv[0]]
    seen = False
    for arg in argv[1:]:
        if arg == "--allow-loose-env-perms":
            seen = True
            continue
        new_argv.append(arg)
    if seen:
        from opencomputer.security.env_loader import set_process_allow_loose_perms

        set_process_allow_loose_perms(True)
        sys.argv = new_argv


def main() -> None:
    # Profile routing runs here (not at import time) so tests and library
    # consumers can import this module without their argv being mutated.
    _apply_profile_override()
    _apply_loose_env_perms_flag()
    # Round 4 Item 5 — auto-load per-profile .env (with global fallback)
    # so users don't have to source it manually before every invocation.
    # Profile already resolved into OPENCOMPUTER_HOME by the override
    # above; we read the active profile name from there. Wrapped in
    # try/except so a malformed .env never crashes startup — env_loader
    # itself fail-closed on loose perms but we want the CLI to keep
    # working even on file-load weirdness.
    try:
        from opencomputer.profiles import read_active_profile
        from opencomputer.security.env_loader import load_for_profile

        load_for_profile(read_active_profile())
    except Exception as e:  # noqa: BLE001 — never crash startup on env load
        _log.debug("per-profile env load failed: %s", e)
    app()


if __name__ == "__main__":
    main()
