"""
Gateway daemon — runs channel adapters + optional WebSocket server.

Two modes:
    1. Channel mode: start configured channel adapters (Telegram, Discord, ...).
       Messages arrive via platform SDKs → Dispatch → AgentLoop → back out.
    2. Wire mode (optional): also start a WebSocket server on a local port,
       letting additional clients (TUI, web, mobile) use the same agent via
       the typed protocol.

Phase 2 focuses on channel mode. Wire mode is scaffolded but minimal.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opencomputer.agent.config import GatewayConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.gateway.dispatch import Dispatch
from opencomputer.gateway.outgoing_drainer import OutgoingDrainer
from opencomputer.gateway.outgoing_queue import OutgoingQueue
from plugin_sdk.channel_contract import BaseChannelAdapter

if TYPE_CHECKING:
    from opencomputer.gateway.agent_router import AgentRouter

logger = logging.getLogger("opencomputer.gateway.server")


def _check_not_root() -> None:
    """Refuse to start the gateway as root unless explicitly allowed.

    Mirrors Hermes ``HERMES_ALLOW_ROOT_GATEWAY`` semantics. The check
    is POSIX-only — on Windows ``os.geteuid`` doesn't exist and the
    call is a no-op.

    Set ``OPENCOMPUTER_ALLOW_ROOT_GATEWAY=1`` in the environment to
    override (e.g., container entrypoint where running as root is
    expected and the host has its own isolation).

    Called from :meth:`Gateway.start` BEFORE any network or
    channel-adapter work — a misconfigured root deployment exits
    cleanly with stderr explaining the override env var.
    """
    if not hasattr(os, "geteuid"):
        return  # Windows / non-POSIX — no concept of effective uid.
    if os.geteuid() != 0:
        return
    if os.environ.get("OPENCOMPUTER_ALLOW_ROOT_GATEWAY") == "1":
        return
    sys.stderr.write(
        "Refusing to start gateway as root. Run as a non-root user, "
        "or set OPENCOMPUTER_ALLOW_ROOT_GATEWAY=1 to override.\n"
    )
    sys.exit(2)


class Gateway:
    """The gateway daemon."""

    def __init__(
        self,
        loop: AgentLoop | None = None,
        config: GatewayConfig | None = None,
        *,
        router: AgentRouter | None = None,
        allowlist_gate: Any = None,
        reset_policy: Any = None,
        last_seen_path: Any = None,
    ) -> None:
        # Phase 2 multi-routing: accept either ``loop=`` (legacy single
        # loop) or ``router=`` (per-profile cache). Exactly one of the
        # two must be set.
        if router is not None and loop is not None:
            raise ValueError("Gateway: pass either loop or router, not both")
        if router is None and loop is None:
            raise ValueError("Gateway: pass either loop or router")
        # PR-1 (Task 1.6 follow-up): hold the gate / policy / last_seen
        # path until Dispatch is constructed below; Gateway forwards them
        # in the Dispatch ctor.
        self._allowlist_gate = allowlist_gate
        self._reset_policy = reset_policy
        self._last_seen_path = last_seen_path

        # ``self.loop`` preserves the legacy attribute access path that
        # tests and downstream code may read.
        self.loop = loop
        # Gateway-level config (PR #221 follow-up). Defaults preserve
        # legacy behavior — ``photo_burst_window=0.8``. Users can
        # override via ``gateway.photo_burst_window: 0.5`` in their
        # ``~/.opencomputer/<profile>/config.yaml``; the CLI's
        # ``opencomputer gateway`` entry point reads :class:`Config`
        # and threads ``cfg.gateway`` here.
        self._config: GatewayConfig = config or GatewayConfig()

        # Build the wrapped factory that registers the consent prompt
        # handler on each per-profile loop's ConsentGate (Pass-2 F7).
        # The Dispatch instance doesn't exist yet; we close over self
        # and read self.dispatch lazily at factory-fire time.
        if router is None:
            from opencomputer.agent.config import _home as _resolve_home
            from opencomputer.gateway.agent_loop_factory import (
                build_agent_loop_for_profile,
            )
            from opencomputer.gateway.agent_router import (
                AgentRouter as _AgentRouter,
            )

            def _wrapped_factory(pid: str, home: Path) -> AgentLoop:
                new_loop = build_agent_loop_for_profile(pid, home)
                # Register consent prompt handler on the per-profile
                # gate if Dispatch is already constructed (it is for
                # any profile resolved AFTER __init__ completes).
                gate = getattr(new_loop, "_consent_gate", None)
                if gate is None:
                    logger.debug(
                        "agent_loop_factory: profile_id=%s loop has no "
                        "_consent_gate; consent prompts disabled for this profile",
                        pid,
                    )
                elif not hasattr(gate, "set_prompt_handler"):
                    logger.warning(
                        "agent_loop_factory: profile_id=%s _consent_gate has no "
                        "set_prompt_handler attribute; consent prompts will not "
                        "be wired",
                        pid,
                    )
                elif getattr(self, "dispatch", None) is None:
                    logger.warning(
                        "agent_loop_factory: profile_id=%s built before Dispatch "
                        "was constructed; consent prompt handler NOT registered "
                        "(this should not happen during normal Gateway init)",
                        pid,
                    )
                else:
                    gate.set_prompt_handler(
                        self.dispatch._send_approval_prompt
                    )
                return new_loop

            def _resolve_profile_home(profile_id: str) -> Path:
                # Default profile -> ``_home()`` (the active
                # OPENCOMPUTER_HOME or default).
                # Per-profile -> ``~/.opencomputer/<profile_id>``.
                if profile_id == "default":
                    return _resolve_home()
                return Path.home() / ".opencomputer" / profile_id

            router = _AgentRouter(
                loop_factory=_wrapped_factory,
                profile_home_resolver=_resolve_profile_home,
            )
            # Seed the router with the existing single loop as
            # ``"default"`` so the legacy CLI path doesn't pay the
            # construction cost again. The seeded loop's gate gets the
            # consent prompt handler registered AFTER Dispatch is
            # constructed below.
            assert loop is not None  # guarded above
            router._loops["default"] = loop

        self._router = router

        # Phase 3 Task 3.3: load ~/.opencomputer/bindings.yaml and
        # construct the BindingResolver. If the file is missing, the
        # config defaults to ``BindingsConfig()`` (default-only routing,
        # which matches the legacy single-profile behaviour). A
        # malformed file is logged loudly but DOES NOT crash boot — we
        # fall back to default-only routing so the user can still chat
        # while they fix the YAML.
        from opencomputer.agent.bindings_config import (
            BindingsConfig,
            load_bindings,
        )
        from opencomputer.gateway.binding_resolver import BindingResolver

        bindings_path = Path.home() / ".opencomputer" / "bindings.yaml"
        try:
            bindings_cfg = load_bindings(bindings_path)
        except ValueError:
            logger.exception(
                "malformed bindings.yaml at %s — falling back to default-only "
                "routing (fix the file to enable multi-profile routing)",
                bindings_path,
            )
            bindings_cfg = BindingsConfig()
        self._resolver = BindingResolver(bindings_cfg)

        # Task I.9: wire the shared PluginAPI through to Dispatch so
        # plugins see a per-request scope via ``api.request_context``.
        # ``shared_api`` is set by ``PluginRegistry.load_all``; if the
        # caller built a Gateway before loading plugins, dispatch
        # silently falls back to the no-scope path.
        from opencomputer.plugins.registry import registry as plugin_registry

        self.dispatch = Dispatch(
            router=router,
            plugin_api=plugin_registry.shared_api,
            config={
                "photo_burst_window": self._config.photo_burst_window,
                # 2026-05-08: gate lifecycle reactions so the default-off
                # invariant is enforced at the dispatcher (see
                # GatewayConfig.lifecycle_reactions docstring).
                "lifecycle_reactions": self._config.lifecycle_reactions,
            },
            resolver=self._resolver,
            allowlist_gate=self._allowlist_gate,
            reset_policy=self._reset_policy,
            last_seen_path=self._last_seen_path,
        )
        # Pass-2 F7: now that Dispatch exists, register the consent
        # prompt handler on the seeded "default" loop's gate. The
        # wrapped factory will handle future per-profile loops
        # automatically because its closure reads self.dispatch lazily.
        if loop is not None:
            gate = getattr(loop, "_consent_gate", None)
            if gate is not None and hasattr(gate, "set_prompt_handler"):
                gate.set_prompt_handler(self.dispatch._send_approval_prompt)
        # PR #221 follow-up: bind the live Dispatch onto the shared
        # PluginAPI so plugin-side helpers (e.g. Discord ``/reset``)
        # can reach the per-chat session-lock map without importing
        # ``opencomputer.gateway.dispatch``. Idempotent — re-binding
        # in tests that construct a Gateway twice is harmless. ``None``
        # before plugins are loaded; that's the wire / CLI path which
        # never runs Discord interactions.
        if plugin_registry.shared_api is not None:
            plugin_registry.shared_api._bind_dispatch(self.dispatch)
            # Wave 6.E.7 — bind the seeded loop's consent gate to the
            # shared PluginAPI so plugins (e.g. matrix) can install a
            # custom prompt handler via ``api.set_consent_prompt_handler``
            # at register() time. Idempotent.
            seeded_loop = router._loops.get("default") if router is not None else None
            seeded_gate = getattr(seeded_loop, "_consent_gate", None) if seeded_loop else None
            if seeded_gate is not None:
                plugin_registry.shared_api._consent_gate = seeded_gate
        self._adapters: list[BaseChannelAdapter] = []
        self._drainer: OutgoingDrainer | None = None
        self._drainer_task: asyncio.Task[None] | None = None
        # T8 — ambient foreground sensor daemon (opt-in). Started in ``start()``
        # only when ``<profile_home>/ambient/state.json`` has ``enabled=True``.
        self._ambient_daemon: Any | None = None
        # Auto-skill-evolution subscriber (T8 of the skill-evolution series).
        # Subscribes to ``session_end`` on the F2 bus and stages SKILL.md
        # candidates for user review. Opt-in via ``oc skills evolution on``.
        self._evolution_subscriber: Any | None = None
        # Hermes channel-port (PR 2 Task 2.3 / amendment §A.5):
        # fatal-error supervisor. Ticks every 60s in ``start()`` to
        # check ``adapter.has_fatal_error()``; reconnects retryable
        # adapters and logs ERROR for non-retryable. Stop event lets
        # ``stop()`` wake the loop promptly without waiting up to 60s.
        self._fatal_supervisor_task: asyncio.Task[None] | None = None
        self._fatal_supervisor_stop: asyncio.Event = asyncio.Event()
        # Wave 6.E.1 / 6.B-β — kanban dispatcher loop. Spawns sibling
        # worker agents for kanban tasks while the gateway is running.
        # Started in ``start()`` only when
        # ``cfg.kanban.dispatch_in_gateway is true`` (default).
        self._kanban_dispatcher: Any | None = None
        self._kanban_dispatcher_task: asyncio.Task[None] | None = None
        # 2026-05-10 — Cron scheduler co-tenant. The cron loop has always
        # supported running inside the gateway daemon (per
        # ``opencomputer/cron/scheduler.py``'s docstring on
        # ``run_scheduler_loop``) but no production caller actually
        # started it here. Result: users with ``oc service install`` (the
        # launchd gateway) had cron jobs registered but NEVER executed,
        # because nothing was ticking the scheduler. The user audit
        # 2026-05-10 captured this as "19 jobs registered, 0 ever ran".
        # Starting it here means a single launchd service ticks
        # both gateway dispatch + cron jobs. Opt-out via
        # ``cron.start_in_gateway: false`` for users who prefer running
        # ``oc cron daemon`` separately.
        self._cron_scheduler_task: asyncio.Task[None] | None = None

    def register_adapter(self, adapter: BaseChannelAdapter) -> None:
        """Register a channel adapter (usually from a loaded plugin)."""
        adapter.set_message_handler(self.dispatch.handle_message)
        self._adapters.append(adapter)
        # Give Dispatch a handle so it can send typing indicators back out.
        self.dispatch.register_adapter(adapter.platform.value, adapter)

    # ── PR-1 follow-up: late-bind setters for AllowlistGate + ResetPolicy ──
    # The cli_gateway._run_foreground constructs these AFTER Gateway() was
    # built (during the daemon-bootstrap dance). Setter methods let the
    # production path wire the gates without rebuilding Gateway, and tests
    # can swap fakes in cleanly.

    def set_allowlist_gate(self, gate: Any) -> None:
        """Bind/replace the AllowlistGate forwarded to Dispatch."""
        self._allowlist_gate = gate
        self.dispatch._allowlist_gate = gate

    def set_reset_policy(
        self,
        policy: Any,
        *,
        last_seen_path: Any = None,
    ) -> None:
        """Bind/replace the ResetPolicyChecker (and optional last_seen path)
        forwarded to Dispatch. If ``last_seen_path`` is provided, replays the
        on-disk state into the dispatcher's caches.
        """
        self._reset_policy = policy
        self.dispatch._reset_policy = policy
        if last_seen_path is not None:
            self._last_seen_path = last_seen_path
            self.dispatch._last_seen_path = last_seen_path
            # Re-load — Dispatch's __init__ already loaded what was on disk
            # at construction time; this picks up any changes since.
            try:
                self.dispatch._load_last_seen()
            except Exception:  # noqa: BLE001 — never block at this seam
                logger.warning(
                    "gateway: re-loading last_seen.json from %s failed",
                    last_seen_path,
                    exc_info=True,
                )

    def _run_channel_ownership_preflight(self) -> None:
        """Refuse to start if a non-OC channel handler is running on this box.

        Per the 2026-05-08 directive (``user_oc_owns_all_channels.md``):
        OpenComputer is the SOLE channel handler. If a competing process
        (Claude Code's ``claude --channels plugin:telegram`` bun bridge,
        Hermes daemon, rival ``oc gateway``, ...) is detected, behavior
        depends on ``cfg.gateway.takeover_on_start``:

        * ``False`` (default): raise :class:`ChannelOwnershipConflict`
          with the offending PIDs + cmdlines + remediation steps.
          Gateway boot aborts; launchd's KeepAlive=dict means the
          service stays stopped until operator intervention.
        * ``True``: terminate the competitors (SIGTERM with grace, then
          SIGKILL), append to audit log, and proceed.

        The audit log lives at
        ``<profile_home>/audit/competitor-takeover.jsonl`` and is
        append-only. Tests exercise both modes; see
        ``tests/test_gateway_preflight.py``.
        """
        from opencomputer.agent.config import _home
        from opencomputer.gateway.preflight import (
            default_audit_path,
            run_preflight,
        )

        cfg_gateway = getattr(self, "_config", None)
        if cfg_gateway is None:
            # Defensive: Gateway built without a config (test scaffolding
            # via ``Gateway.__new__``) skips preflight rather than raising.
            return

        takeover_enabled = bool(getattr(cfg_gateway, "takeover_on_start", False))
        grace = float(getattr(cfg_gateway, "takeover_grace_seconds", 5.0))
        audit_path = default_audit_path(_home())

        run_preflight(
            takeover_on_start=takeover_enabled,
            grace_seconds=grace,
            audit_log=audit_path,
        )

    async def start(self) -> None:
        """Connect all adapters. Returns once they're all running.

        Phase 0 (2026-05-08, ``user_oc_owns_all_channels.md`` directive):
        run channel-ownership preflight before connecting any adapter.
        OpenComputer is the sole channel handler; if competitors are
        running and ``cfg.gateway.takeover_on_start`` is False, raise
        :class:`ChannelOwnershipConflict` so the operator sees a loud
        refusal rather than a silent reply blackhole.

        After preflight passes, capture the running asyncio loop and
        wire the /background completion notifier so worker threads can
        schedule ``adapter.send()`` via ``run_coroutine_threadsafe``.
        """
        # Security-first: refuse to start as root unless explicitly
        # allowed (Hermes parity — HERMES_ALLOW_ROOT_GATEWAY → OC's
        # OPENCOMPUTER_ALLOW_ROOT_GATEWAY). Fires before any other
        # work touches the host or network.
        _check_not_root()

        # Security-first: refuse to boot if another channel handler
        # owns the same chat surfaces. Must happen before any other
        # work touches the network.
        self._run_channel_ownership_preflight()

        # Capture the gateway's main asyncio loop and register the
        # /background completion notifier on the registry. Done after
        # preflight (the directive above) but before adapter connect()
        # so an early background-job completion (e.g. test mode)
        # routes correctly. Failure-isolated — a missing background
        # registry must never block gateway boot.
        try:
            from opencomputer.agent.background_jobs import (
                get_default_registry as _bg_get_registry,
            )

            self.dispatch.bind_main_loop(asyncio.get_running_loop())
            _bg_get_registry().set_completion_notifier(
                self.dispatch.background_completion_notifier
            )
        except Exception:  # noqa: BLE001 — never block gateway boot on this
            logger.debug("gateway: /background notifier wire failed", exc_info=True)

        logger.info("gateway: starting %d adapters", len(self._adapters))
        results = await asyncio.gather(
            *(a.connect() for a in self._adapters), return_exceptions=True
        )
        for adapter, res in zip(self._adapters, results, strict=False):
            if isinstance(res, Exception):
                logger.error(
                    "gateway: adapter %s failed to connect: %s", adapter.platform, res
                )
                # Surface as fatal-retryable so the periodic supervisor
                # (60s tick) reconnects. Without this, an exception during
                # boot parks the adapter dead with no recovery — see
                # 2026-05-08 incident where Telegram polling-slot conflict
                # silently disabled all replies for hours.
                try:
                    adapter._set_fatal_error(
                        "connect_raised_exception",
                        f"connect() raised: {res!r}",
                        retryable=True,
                    )
                except Exception:  # noqa: BLE001 — adapter API contract failure
                    logger.exception(
                        "gateway: adapter %s does not implement _set_fatal_error",
                        adapter.platform,
                    )
            elif res is False:
                logger.error(
                    "gateway: adapter %s returned False from connect()",
                    adapter.platform,
                )
                # Same recovery path as exception branch — without this,
                # connect()=False silently parks the adapter forever.
                try:
                    adapter._set_fatal_error(
                        "connect_returned_false",
                        "connect() returned False at startup — see adapter logs",
                        retryable=True,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "gateway: adapter %s does not implement _set_fatal_error",
                        adapter.platform,
                    )

        # Tier-A item 14 — start the outgoing-message drainer so the
        # MCP write tools (``messages_send``) can route through the
        # gateway's live adapters. Always running while the gateway is
        # up — even with zero adapters paired, expiring stale rows on
        # boot is still useful.
        await self._start_outgoing_drainer()

        # T8 — ambient foreground sensor daemon. Only starts if the user
        # opted in via ``oc ambient on``; failure to start is logged but
        # never blocks the gateway.
        await self._start_ambient_daemon()

        # Auto-skill-evolution subscriber. Same opt-in / failure-isolated
        # contract as the ambient daemon — never crashes gateway boot.
        await self._start_evolution_subscriber()

        # social-traces post-task subscriber (Phase 9 production
        # wiring). Resolves provider + cost guard same way
        # _start_evolution_subscriber does. Opt-in via
        # ``oc traces enable``; failure isolated.
        await self._start_traces_subscriber()

        # Wave 6.E.1 — kanban dispatcher loop. Reads
        # ``cfg.kanban.dispatch_in_gateway`` (default true) and starts
        # a periodic ``dispatch_once`` invoker that spawns sibling
        # worker agents on every ``kanban_create`` event.
        await self._start_kanban_dispatcher_loop()

        # Wave 6.E.6 — register the /kanban slash command so users can
        # read + write the board mid-chat. ``bypass_running_guard``
        # makes it accessible even when the agent is mid-turn (lock
        # bypass handled in Dispatch._maybe_bypass_running_guard).
        try:
            from opencomputer.kanban.slash_command import (
                register_kanban_slash_commands,
            )
            from opencomputer.plugins.registry import registry as _plugin_registry
            register_kanban_slash_commands(_plugin_registry)
        except Exception:  # noqa: BLE001
            logger.exception(
                "gateway: failed to register /kanban slash command (ignored)"
            )

        # Hermes channel-port (PR 2 Task 2.3): start the fatal-error
        # supervisor so adapters that flag themselves with
        # ``_set_fatal_error`` get auto-reconnected (retryable) or
        # ERROR-logged (non-retryable). Always-on; runs at 60s cadence.
        self._fatal_supervisor_stop.clear()
        self._fatal_supervisor_task = asyncio.create_task(
            self._check_fatal_errors_periodic(),
            name="gateway-fatal-error-supervisor",
        )

        # 2026-05-08 — Hermes Doc-2 parity: discover ~/.opencomputer/hooks
        # filesystem hooks + fire ``gateway:startup``. This is the third
        # hook surface (alongside plugin hooks + shell hooks). Failure
        # isolated — a broken HOOK.yaml/handler.py never blocks boot.
        try:
            from opencomputer.gateway.boot_md import register_boot_md_hook
            from opencomputer.gateway.event_hooks import (
                GATEWAY_STARTUP as _GW_STARTUP,
            )
            from opencomputer.gateway.event_hooks import (
                engine as _gw_hooks_engine,
            )

            _gw_hooks_engine.reload()
            register_boot_md_hook(_gw_hooks_engine)
            await _gw_hooks_engine.fire(
                _GW_STARTUP,
                {
                    "platforms": [a.platform.value for a in self._adapters],
                },
            )
        except Exception:  # noqa: BLE001 — boot must not fail on hooks
            logger.warning(
                "gateway hooks discovery / fire failed", exc_info=True,
            )

        # Startup ping (the OpenClaw "back online" magic message).
        # Opt-in via gateway.startup_ping_chats. Fires once after every
        # adapter has had a chance to connect. Fail-open — a flaky
        # channel must never wedge gateway boot.
        await self._fire_startup_pings()

        # 2026-05-10 — co-tenant cron scheduler. Default ON via
        # ``cron.start_in_gateway`` so a single launchd gateway service
        # ticks both message dispatch + cron jobs. Without this, users
        # with the launchd gateway saw "19 jobs registered, 0 ever ran"
        # because nothing was ticking the scheduler in the same process.
        # Opt-out for users who run a separate ``oc cron daemon``.
        try:
            cron_cfg = getattr(self._config, "cron", None)
            if cron_cfg is None or getattr(
                cron_cfg, "start_in_gateway", True
            ):
                from opencomputer.cron.scheduler import run_scheduler_loop

                interval = (
                    getattr(cron_cfg, "gateway_tick_interval_s", 60)
                    if cron_cfg
                    else 60
                )
                self._cron_scheduler_task = asyncio.create_task(
                    run_scheduler_loop(interval_s=int(interval)),
                    name="gateway-cron-scheduler",
                )
                logger.info(
                    "gateway: co-tenant cron scheduler started "
                    "(interval=%ds; opt-out via cron.start_in_gateway=false)",
                    int(interval),
                )
            else:
                logger.info(
                    "gateway: cron.start_in_gateway=false — cron jobs "
                    "will not tick from this process. Run `oc cron daemon` "
                    "separately."
                )
        except Exception:  # noqa: BLE001 — cron must never wedge gateway boot
            logger.warning(
                "gateway: cron scheduler co-tenant start failed; "
                "cron jobs will not tick from this process",
                exc_info=True,
            )

    async def _fire_startup_pings(self) -> None:
        """Send the configured startup-ping message to each (platform, chat).

        Skipped silently when ``gateway.startup_ping_chats`` is empty.
        Each per-chat send is wrapped in its own try/except so one bad
        chat ID doesn't drop the others. This is the OpenClaw-style
        "boot → first heartbeat ping" feature: a bot identity that
        outlived a multi-month shutdown gets a confirmation message
        on boot.
        """
        chats = getattr(self._config, "startup_ping_chats", ())
        if not chats:
            return
        message = getattr(
            self._config, "startup_ping_message",
            "OpenComputer back online",
        )
        adapters_by_platform = {a.platform.value: a for a in self._adapters}
        for entry in chats:
            try:
                platform, chat_id = entry
            except (TypeError, ValueError):
                logger.warning(
                    "gateway: startup_ping_chats entry malformed (expected "
                    "(platform, chat_id) tuple), got %r", entry,
                )
                continue
            adapter = adapters_by_platform.get(str(platform))
            if adapter is None:
                logger.info(
                    "gateway: startup ping skipped — no adapter registered "
                    "for platform=%r (chat=%r)", platform, chat_id,
                )
                continue
            try:
                await adapter.send(str(chat_id), message)
                logger.info(
                    "gateway: startup ping sent to %s/%s", platform, chat_id,
                )
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.warning(
                    "gateway: startup ping to %s/%s failed (continuing): %s",
                    platform, chat_id, exc,
                )

    async def _start_outgoing_drainer(self) -> None:
        from opencomputer.agent.config import _home
        from opencomputer.plugins.registry import registry as plugin_registry

        adapters_by_platform = {
            a.platform.value: a for a in self._adapters
        }
        queue = OutgoingQueue(_home() / "sessions.db")
        # Hermes channel-port PR 2 / amendment §A.3: thread the live
        # queue into PluginAPI so webhook-style plugins can enqueue
        # outbound messages via ``api.outgoing_queue.enqueue(...)``
        # without importing opencomputer.* directly. Binding here (vs
        # construction) because the queue's SQLite path is per-profile
        # and only resolved after config init.
        plugin_registry.outgoing_queue = queue
        if plugin_registry.shared_api is not None:
            plugin_registry.shared_api._bind_outgoing_queue(queue)
        self._drainer = OutgoingDrainer(queue, adapters_by_platform)
        await self._drainer.expire_stale_on_boot()
        self._drainer_task = asyncio.create_task(
            self._drainer.run_forever(),
            name="gateway-outgoing-drainer",
        )

    async def _start_kanban_dispatcher_loop(self) -> None:
        """Start the kanban dispatcher loop iff ``cfg.kanban.dispatch_in_gateway``.

        Hermes deprecated the standalone ``kanban daemon`` in favor of
        an embedded gateway loop; we ship the same shape. Failure to
        load config or to start the loop is logged but never blocks
        gateway boot.
        """
        from opencomputer.gateway.kanban_dispatcher import (
            KanbanDispatcherLoop,
            read_kanban_dispatch_config,
        )

        try:
            import yaml as _yaml

            from opencomputer.agent.config import _home
            cfg_path = _home() / "config.yaml"
            raw_cfg: dict[str, Any] = {}
            if cfg_path.exists():
                raw_cfg = _yaml.safe_load(cfg_path.read_text()) or {}
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "kanban dispatcher loop: config read failed (%s); using defaults",
                exc,
            )
            raw_cfg = {}

        enabled, interval, max_spawn = read_kanban_dispatch_config(raw_cfg)
        if not enabled:
            logger.info(
                "kanban dispatcher loop disabled (cfg.kanban.dispatch_in_gateway=false); "
                "use ``oc kanban dispatch`` externally to dispatch tasks."
            )
            return

        self._kanban_dispatcher = KanbanDispatcherLoop(
            interval_seconds=interval,
            max_spawn=max_spawn,
        )
        self._kanban_dispatcher_task = asyncio.create_task(
            self._kanban_dispatcher.run_forever(),
            name="gateway-kanban-dispatcher",
        )

    async def _start_ambient_daemon(self) -> None:
        """Start the ambient foreground sensor daemon iff the user opted in.

        The daemon lives at ``extensions/ambient-sensors/`` (hyphenated dir).
        Python module names need underscores, so we reuse the synthetic
        ``extensions.ambient_sensors`` alias helper from
        :mod:`opencomputer.cli_ambient`. Any failure here is logged and
        swallowed — the gateway must keep working even if the ambient
        plugin is broken or absent.
        """
        try:
            from opencomputer.agent.config import _home
            from opencomputer.cli_ambient import _ensure_ambient_sensors_alias
            from opencomputer.ingestion.bus import default_bus

            _ensure_ambient_sensors_alias()
            from extensions.ambient_sensors.daemon import (  # type: ignore[import-not-found]
                ForegroundSensorDaemon,
            )
            from extensions.ambient_sensors.pause_state import (  # type: ignore[import-not-found]
                load_state,
            )

            state = load_state(_home() / "ambient" / "state.json")
            if not state.enabled:
                logger.debug("ambient sensor opt-out (state.enabled=False) — skipping daemon")
                return

            self._ambient_daemon = ForegroundSensorDaemon(
                bus=default_bus,
                profile_home_factory=_home,
            )
            self._ambient_daemon.start()
            logger.info("ambient sensor daemon started (state.enabled=True)")
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to start ambient daemon — gateway continues without it"
            )

    async def _start_evolution_subscriber(self) -> None:
        """Start the auto-skill-evolution F2 subscriber iff the user opted in.

        The plugin lives at ``extensions/skill-evolution/`` (hyphenated).
        We reuse the synthetic ``extensions.skill_evolution`` alias helper
        from :mod:`opencomputer.cli_skills` so the production import path
        matches what the test suite uses. Any failure is logged and
        swallowed — the gateway must keep working even if the plugin is
        broken or absent.

        Wiring:
        - Provider is resolved the same way :mod:`opencomputer.agent.title_generator`
          and :mod:`opencomputer.agent.recall_synthesizer` do — via the
          configured ``cfg.model.provider`` lookup against the live plugin
          registry. That lets the LLM judge + extractor inherit the user's
          API config without new setup.
        - Cost guard is the per-profile default
          (:func:`opencomputer.cost_guard.get_default_guard`), shared with
          the rest of the agent so its budget caps apply transitively.
        - SessionDB is constructed lazily per call via the factory so the
          subscriber never holds a connection between events.
        """
        try:
            from opencomputer.agent.config import _home, default_config
            from opencomputer.agent.state import SessionDB
            from opencomputer.cli_skills import _ensure_skill_evolution_alias
            from opencomputer.cost_guard import get_default_guard
            from opencomputer.ingestion.bus import default_bus
            from opencomputer.plugins.registry import registry as plugin_registry

            _ensure_skill_evolution_alias()
            from extensions.skill_evolution.subscriber import (  # type: ignore[import-not-found]
                EvolutionSubscriber,
                _is_enabled,
            )

            if not _is_enabled(_home()):
                logger.debug(
                    "skill-evolution opt-out (state.enabled=False) — skipping subscriber"
                )
                return

            # Resolve the user's configured provider — same pattern used by
            # title_generator / recall_synthesizer. If the provider isn't
            # registered, we can't run the LLM judge, so we skip rather
            # than start a half-working subscriber.
            cfg = default_config()
            provider_cls = plugin_registry.providers.get(cfg.model.provider)
            if provider_cls is None:
                logger.warning(
                    "skill-evolution: provider %r not registered — skipping subscriber",
                    cfg.model.provider,
                )
                return
            provider = provider_cls() if isinstance(provider_cls, type) else provider_cls

            cfg_for_db = cfg  # captured by the lambda below

            evo_subscriber = EvolutionSubscriber(
                bus=default_bus,
                profile_home_factory=_home,
                session_db_factory=lambda: SessionDB(cfg_for_db.session.db_path),
                provider=provider,
                cost_guard=get_default_guard(),
            )
            evo_subscriber.start()
            self._evolution_subscriber = evo_subscriber
            logger.info("skill-evolution subscriber started (state.enabled=True)")
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to start skill-evolution subscriber — gateway continues without it"
            )

    async def _start_traces_subscriber(self) -> None:
        """Start the social-traces post-task subscriber iff the user opted in.

        Mirrors :meth:`_start_evolution_subscriber`. Resolves the
        configured provider against the live plugin registry and the
        per-profile default cost guard, then calls the plugin's
        ``wire_subscriber`` entry point. The plugin holds the
        subscriber in a module-level singleton so :meth:`stop` can
        find it via ``stop_subscriber``.

        Opt-in via ``oc traces enable``. Failure isolated — the
        gateway must keep working even if the plugin is broken or
        absent. Only the LLM-driven post-task path needs this; the
        pre-task BEFORE_TASK hook works regardless because
        ``register()`` registers it unconditionally.
        """
        try:
            from opencomputer.agent.config import _home, default_config
            from opencomputer.cli_traces import _ensure_alias
            from opencomputer.cost_guard import get_default_guard
            from opencomputer.plugins.registry import registry as plugin_registry

            _ensure_alias()
            from extensions.social_traces.plugin import (
                wire_subscriber,  # type: ignore[import-not-found]
            )
            from extensions.social_traces.state import is_enabled  # type: ignore[import-not-found]

            if not is_enabled(_home()):
                logger.debug(
                    "social-traces opt-out (state.enabled=False) — "
                    "skipping subscriber"
                )
                return

            cfg = default_config()
            provider_cls = plugin_registry.providers.get(cfg.model.provider)
            if provider_cls is None:
                logger.warning(
                    "social-traces: provider %r not registered — "
                    "skipping subscriber",
                    cfg.model.provider,
                )
                return
            provider = (
                provider_cls() if isinstance(provider_cls, type) else provider_cls
            )

            try:
                from opencomputer import __version__ as _oc_version
            except Exception:  # noqa: BLE001
                _oc_version = ""

            wire_subscriber(
                provider=provider,
                cost_guard=get_default_guard(),
                harness_version=f"opencomputer/{_oc_version}",
            )
            logger.info("social-traces subscriber started (state.enabled=True)")
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to start social-traces subscriber — gateway continues without it"
            )

    async def _tick_fatal_error_supervisor(self) -> None:
        """One supervisor pass — public-ish so tests can drive a single tick.

        Hermes channel-port (PR 2 Task 2.3 + amendment §A.5). Iterates
        adapters; for each fatally-flagged one:

        * ``retryable=True``  → disconnect, ``clear_fatal_error()``, connect.
        * ``retryable=False`` → ERROR-log only; the adapter stays disconnected.

        Per amendment §A.5, uses ``adapter.clear_fatal_error()`` rather
        than mutating private fields directly.
        """
        for adapter in list(self._adapters):
            if not adapter.has_fatal_error():
                continue
            code = adapter._fatal_error_code
            retryable = adapter._fatal_error_retryable
            if retryable:
                logger.warning(
                    "fatal-error supervisor: reconnecting adapter %s (code=%s)",
                    adapter.platform,
                    code,
                )
                try:
                    await adapter.disconnect()
                    adapter.clear_fatal_error()
                    await adapter.connect()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "fatal-error supervisor: reconnect failed for %s",
                        adapter.platform,
                    )
            else:
                logger.error(
                    "fatal-error supervisor: %s non-retryable code=%s msg=%s",
                    adapter.platform,
                    code,
                    adapter._fatal_error_message,
                )

    async def _check_fatal_errors_periodic(self, *, interval: float = 60.0) -> None:
        """Periodic fatal-error sweep. Runs until ``_fatal_supervisor_stop`` fires.

        Hermes channel-port (PR 2 Task 2.3). Default cadence 60s; tests
        can pass a small interval to drive multiple iterations quickly.
        """
        while not self._fatal_supervisor_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._fatal_supervisor_stop.wait(), timeout=interval
                )
                # Stop event fired — exit cleanly.
                return
            except TimeoutError:
                pass
            await self._tick_fatal_error_supervisor()

    async def stop(self) -> None:
        logger.info("gateway: stopping")
        # Hermes channel-port (PR 2 Task 2.3): stop supervisor BEFORE we
        # disconnect adapters so the loop doesn't race a final reconnect.
        if self._fatal_supervisor_task is not None:
            self._fatal_supervisor_stop.set()
            try:
                await asyncio.wait_for(
                    self._fatal_supervisor_task, timeout=2.0
                )
            except (TimeoutError, asyncio.CancelledError):
                self._fatal_supervisor_task.cancel()
            self._fatal_supervisor_task = None
        if self._evolution_subscriber is not None:
            try:
                self._evolution_subscriber.stop()
            except Exception:  # noqa: BLE001
                logger.exception("skill-evolution subscriber stop failed (ignored)")
            self._evolution_subscriber = None
        # social-traces subscriber (Phase 9). Held by the plugin's
        # module-level singleton, not on Gateway directly, so we
        # call ``stop_subscriber`` on the plugin module rather than
        # mutating an attribute here. Failure-isolated.
        try:
            from opencomputer.cli_traces import _ensure_alias as _ensure_st_alias

            _ensure_st_alias()
            from extensions.social_traces.plugin import stop_subscriber as _stop_st

            _stop_st()
        except Exception:  # noqa: BLE001
            logger.exception("social-traces subscriber stop failed (ignored)")
        if self._ambient_daemon is not None:
            try:
                await self._ambient_daemon.stop()
            except Exception:  # noqa: BLE001
                logger.exception("ambient daemon stop failed (ignored)")
            self._ambient_daemon = None
        if self._drainer is not None:
            self._drainer.stop()
        if self._drainer_task is not None:
            try:
                await asyncio.wait_for(self._drainer_task, timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                self._drainer_task.cancel()
        # Wave 6.E.1 — kanban dispatcher loop. Stop *before* the
        # adapters disconnect so we never spawn a worker against an
        # adapter that's mid-teardown. dispatch_once is itself
        # idempotent so a half-completed tick is safe to abandon.
        if self._kanban_dispatcher is not None:
            try:
                await self._kanban_dispatcher.stop()
            except Exception:  # noqa: BLE001
                logger.exception("kanban dispatcher stop signal failed (ignored)")
        if self._kanban_dispatcher_task is not None:
            try:
                await asyncio.wait_for(self._kanban_dispatcher_task, timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                self._kanban_dispatcher_task.cancel()
            self._kanban_dispatcher_task = None
            self._kanban_dispatcher = None
        # 2026-05-10 — co-tenant cron scheduler. Cancel cleanly so the
        # gateway shutdown isn't held up by the cron loop's asyncio.sleep.
        if self._cron_scheduler_task is not None:
            self._cron_scheduler_task.cancel()
            try:
                await asyncio.wait_for(self._cron_scheduler_task, timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._cron_scheduler_task = None
        await asyncio.gather(
            *(a.disconnect() for a in self._adapters), return_exceptions=True
        )

    async def serve_forever(self) -> None:
        """Connect adapters and block until interrupted.

        Beyond the historic "sleep forever" body, this loop now drives:

        1. **Drain-flag polling** — when ``oc gateway restart
           --drain-timeout=N`` writes ``<profile>/gateway/drain.flag``,
           we stop accepting new dispatches (set
           ``self.dispatch._drain_active = True``) and wait for in-flight
           runs to complete before exiting cleanly.
        2. **Pairing-code expired-sweep** — every 60 seconds we ask the
           dispatcher's pairing store (if attached) to drop expired
           codes. The PairingCodeStore handles its own locking; we just
           tick.
        """
        await self.start()
        # Resolve the drain-flag location the same way the CLI writes it.
        drain_flag_path: Path | None = None
        try:
            from opencomputer.agent.config_store import config_file_path

            drain_flag_path = config_file_path().parent / "gateway" / "drain.flag"
        except Exception:  # noqa: BLE001 — drain is opt-in
            drain_flag_path = None

        last_sweep = 0.0
        try:
            while True:
                # Drain-flag check (CLI restart writes it; we honor it).
                if drain_flag_path is not None and drain_flag_path.exists():
                    logger.info("gateway: drain flag detected at %s", drain_flag_path)
                    # Tell Dispatch to refuse new arrivals.
                    self.dispatch._drain_active = True  # noqa: SLF001
                    # Wait up to the timeout written in the flag (default 30).
                    timeout_s = 30
                    try:
                        timeout_s = max(1, int(drain_flag_path.read_text().strip() or "30"))
                    except (OSError, ValueError):
                        pass
                    deadline = asyncio.get_running_loop().time() + timeout_s
                    while asyncio.get_running_loop().time() < deadline:
                        inflight = getattr(self.dispatch, "_inflight_count", 0)
                        if inflight <= 0:
                            break
                        await asyncio.sleep(0.5)
                    # Clean the flag so the next start is fresh.
                    try:
                        drain_flag_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    logger.info("gateway: drain complete; exiting serve_forever")
                    return

                # Pairing-code expired-sweep (60s tick).
                now = asyncio.get_running_loop().time()
                if now - last_sweep >= 60.0:
                    last_sweep = now
                    gate = getattr(self, "_allowlist_gate", None)
                    if gate is not None and hasattr(gate, "pairing_store"):
                        try:
                            removed = gate.pairing_store.expired_sweep_all()
                            if removed:
                                logger.info(
                                    "gateway: expired-sweep dropped %d pairing codes",
                                    removed,
                                )
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "gateway: pairing expired-sweep failed",
                                exc_info=True,
                            )

                # Sleep — short enough that drain-flag responsiveness is good.
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    @property
    def adapters(self) -> list[BaseChannelAdapter]:
        return list(self._adapters)


__all__ = ["Gateway"]
