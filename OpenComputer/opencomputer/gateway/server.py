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
from typing import Any

from opencomputer.agent.config import GatewayConfig
from opencomputer.agent.loop import AgentLoop
from opencomputer.gateway.dispatch import Dispatch
from opencomputer.gateway.outgoing_drainer import OutgoingDrainer
from opencomputer.gateway.outgoing_queue import OutgoingQueue
from plugin_sdk.channel_contract import BaseChannelAdapter

logger = logging.getLogger("opencomputer.gateway.server")


class Gateway:
    """The gateway daemon."""

    def __init__(
        self,
        loop: AgentLoop,
        config: GatewayConfig | None = None,
    ) -> None:
        self.loop = loop
        # Gateway-level config (PR #221 follow-up). Defaults preserve
        # legacy behavior — ``photo_burst_window=0.8``. Users can
        # override via ``gateway.photo_burst_window: 0.5`` in their
        # ``~/.opencomputer/<profile>/config.yaml``; the CLI's
        # ``opencomputer gateway`` entry point reads :class:`Config`
        # and threads ``cfg.gateway`` here.
        self._config: GatewayConfig = config or GatewayConfig()
        # Task I.9: wire the shared PluginAPI through to Dispatch so
        # plugins see a per-request scope via ``api.request_context``.
        # ``shared_api`` is set by ``PluginRegistry.load_all``; if the
        # caller built a Gateway before loading plugins, dispatch
        # silently falls back to the no-scope path.
        from opencomputer.plugins.registry import registry as plugin_registry

        self.dispatch = Dispatch(
            loop,
            plugin_api=plugin_registry.shared_api,
            config={"photo_burst_window": self._config.photo_burst_window},
        )
        # PR #221 follow-up: bind the live Dispatch onto the shared
        # PluginAPI so plugin-side helpers (e.g. Discord ``/reset``)
        # can reach the per-chat session-lock map without importing
        # ``opencomputer.gateway.dispatch``. Idempotent — re-binding
        # in tests that construct a Gateway twice is harmless. ``None``
        # before plugins are loaded; that's the wire / CLI path which
        # never runs Discord interactions.
        if plugin_registry.shared_api is not None:
            plugin_registry.shared_api._bind_dispatch(self.dispatch)
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

    def register_adapter(self, adapter: BaseChannelAdapter) -> None:
        """Register a channel adapter (usually from a loaded plugin)."""
        adapter.set_message_handler(self.dispatch.handle_message)
        self._adapters.append(adapter)
        # Give Dispatch a handle so it can send typing indicators back out.
        self.dispatch.register_adapter(adapter.platform.value, adapter)

    async def start(self) -> None:
        """Connect all adapters. Returns once they're all running."""
        logger.info("gateway: starting %d adapters", len(self._adapters))
        results = await asyncio.gather(
            *(a.connect() for a in self._adapters), return_exceptions=True
        )
        for adapter, res in zip(self._adapters, results, strict=False):
            if isinstance(res, Exception):
                logger.error(
                    "gateway: adapter %s failed to connect: %s", adapter.platform, res
                )
            elif res is False:
                logger.error("gateway: adapter %s returned False from connect()", adapter.platform)

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

        # Hermes channel-port (PR 2 Task 2.3): start the fatal-error
        # supervisor so adapters that flag themselves with
        # ``_set_fatal_error`` get auto-reconnected (retryable) or
        # ERROR-logged (non-retryable). Always-on; runs at 60s cadence.
        self._fatal_supervisor_stop.clear()
        self._fatal_supervisor_task = asyncio.create_task(
            self._check_fatal_errors_periodic(),
            name="gateway-fatal-error-supervisor",
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
        await asyncio.gather(
            *(a.disconnect() for a in self._adapters), return_exceptions=True
        )

    async def serve_forever(self) -> None:
        """Connect adapters and block until interrupted."""
        await self.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    @property
    def adapters(self) -> list[BaseChannelAdapter]:
        return list(self._adapters)


__all__ = ["Gateway"]
