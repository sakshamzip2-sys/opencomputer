"""Post-task SessionEndEvent subscriber — Phase 2 stub.

Real distillation + submission lands in Phase 5-7. This stub mirrors the
``EvolutionSubscriber`` shape (see ``extensions/skill-evolution/subscriber.py``)
so the lifecycle hooks (``start`` / ``stop`` / heartbeat) are wired and
testable from day one.

Phase 2 contract:

* Subscribes to ``SessionEndEvent`` on the
  :class:`opencomputer.ingestion.bus.TypedEventBus`.
* On every event arrival: read enabled flag, write heartbeat. Nothing
  else.
* Phase 5 layers on the decision tree (``trace_used is None`` → emit;
  ``trace_used set`` → run novelty judge first).
* Phase 6-7 add the LLM judge + redactor + distiller + submission.

Failure isolation: per-event work is fire-and-forget; per-stage stages
are wrapped in try/except. A bus subscriber must NEVER raise into the
publish path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opencomputer.hooks.runner import fire_and_forget
from plugin_sdk.ingestion import SessionEndEvent

from . import state

_log = logging.getLogger("opencomputer.social_traces.subscriber")


class TraceEmissionSubscriber:
    """Subscribes to ``session_end`` on the F2 bus.

    Construction is side-effect-free — call :meth:`start` to attach,
    :meth:`stop` to detach. Idempotent on both ends.

    Phase 2 stub: no extraction pipeline is wired yet. ``_handle_event``
    just heartbeats so operators can confirm the bus subscription is
    live before the heavy LLM machinery exists. Adding the real
    pipeline is a Phase 5-7 concern.
    """

    def __init__(
        self,
        *,
        bus: Any,
        profile_home_factory: Callable[[], Path],
    ) -> None:
        self._bus = bus
        self._profile_home_factory = profile_home_factory
        self._subscription: Any = None

    # ─── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Subscribe to ``session_end`` events. Idempotent."""
        if self._subscription is not None:
            return
        self._subscription = self._bus.subscribe("session_end", self._handle_event)

    def stop(self) -> None:
        """Unsubscribe from the bus. Idempotent."""
        sub = self._subscription
        self._subscription = None
        if sub is None:
            return
        try:
            sub.unsubscribe()
        except Exception:  # noqa: BLE001 — never raise from stop
            _log.warning(
                "social-traces: subscription.unsubscribe raised (continuing)",
                exc_info=True,
            )

    # ─── handlers ──────────────────────────────────────────────────

    async def _handle_event(self, event: SessionEndEvent) -> None:
        """Fast bus-facing handler. Heartbeat, then offload heavy work."""
        try:
            profile_home = self._profile_home_factory()
        except Exception:  # noqa: BLE001 — bad factory must not poison the bus
            _log.warning(
                "social-traces: profile_home_factory raised", exc_info=True
            )
            return

        if not state.is_enabled(profile_home):
            return

        state.write_heartbeat(profile_home)

        # Phase 5+ will offload the real pipeline here:
        #
        #     fire_and_forget(self._run_pipeline(event))
        #
        # For Phase 2 the heartbeat is the entire job. We import
        # ``fire_and_forget`` already to keep the future wiring obvious
        # and to make sure the import path stays unbroken if a stub
        # subscriber gets shipped to a user who turns the flag on.
        _ = fire_and_forget  # silence "unused" — load-bearing future hook


__all__ = ["TraceEmissionSubscriber"]
