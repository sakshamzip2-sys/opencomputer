"""Bus listener — wires the structured agent log into ``default_bus``.

When 3.F is on, every :class:`SignalEvent` published to
:data:`opencomputer.ingestion.bus.default_bus` is mirrored as one JSON
line in ``agent.log`` via :class:`StructuredAgentLogger`. Each event's
``event_type`` becomes the ``kind`` discriminator; the rest of the
event fields are dumped as-is.

Not auto-attached at import time. Callers are:

- ``opencomputer system-control enable`` (CLI; the typical activation
  path).
- :class:`AgentLoop` startup, when ``config.system_control.enabled`` is
  True at construction time (best-effort — a bus listener is purely
  observational and never affects loop correctness).

The handle returned by :func:`attach_to_bus` is a
:class:`opencomputer.ingestion.bus.Subscription`; call ``.unsubscribe()``
to stop listening.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, is_dataclass
from typing import Any

from opencomputer.ingestion.bus import Subscription, get_default_bus
from opencomputer.system_control.logger import default_logger
from plugin_sdk.ingestion import SignalEvent

_log = logging.getLogger("opencomputer.system_control.bus_listener")

# Module-level handle so detach_from_bus() can find the active
# subscription without callers having to thread it through. A second
# attach() while one is already active is a no-op (idempotent enable).
_active_lock = threading.Lock()
_active_subscription: Subscription | None = None


def _event_to_log_kwargs(event: SignalEvent) -> dict[str, Any]:
    """Convert a SignalEvent's fields into ``log()`` kwargs.

    ``event_type`` becomes the ``kind`` discriminator (handled at the
    callsite); everything else is dumped as-is. Mappings are passed
    through; nested dataclasses are flattened via :func:`dataclasses.asdict`.
    """
    if not is_dataclass(event):
        return {"event_repr": repr(event)}
    payload = asdict(event)
    # ``kind`` is the StructuredAgentLogger's reserved name; rename
    # SignalEvent.event_type to that. We pop both so ``kind`` doesn't
    # collide downstream (the logger auto-uses the kwarg's caller name).
    payload.pop("event_type", None)
    return payload


def _make_handler() -> Any:
    """Build the subscription handler closure.

    Resolves :func:`default_logger` lazily on each call so a config
    reload (system-control turned off mid-process) skips writes
    cleanly.
    """

    def handler(event: SignalEvent) -> None:
        lg = default_logger()
        if lg is None:
            # System-control was flipped off after attach; quietly skip.
            return
        try:
            kwargs = _event_to_log_kwargs(event)
            lg.log(kind=event.event_type, **kwargs)
        except Exception as e:  # noqa: BLE001 — bus must never crash
            _log.warning("system-control bus handler error: %s", e, exc_info=True)

    return handler


def attach_to_bus() -> Subscription | None:
    """Subscribe the structured logger to ``default_bus`` for ALL events.

    Returns ``None`` when ``Config.system_control.enabled`` is ``False``
    (no work to do). Idempotent: a second attach() while one is already
    active returns the existing :class:`Subscription` handle.

    The returned handle's ``unsubscribe()`` method (or
    :func:`detach_from_bus` here for symmetry) removes the listener.
    """
    # Local import to avoid forming a circular dep with config_store.
    from opencomputer.agent.config_store import load_config

    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001 — defensive
        _log.warning("attach_to_bus: failed to load config: %s", e)
        return None

    if not cfg.system_control.enabled:
        return None

    global _active_subscription
    with _active_lock:
        if _active_subscription is not None:
            return _active_subscription
        sub = get_default_bus().subscribe(None, _make_handler())
        _active_subscription = sub
        _log.debug("system-control bus listener attached (sub=%s)", sub.id)
        return sub


def detach_from_bus() -> None:
    """Remove the active subscription, if any. Idempotent."""
    global _active_subscription
    with _active_lock:
        sub = _active_subscription
        _active_subscription = None
    if sub is not None:
        try:
            sub.unsubscribe()
            _log.debug("system-control bus listener detached (sub=%s)", sub.id)
        except Exception as e:  # noqa: BLE001 — defensive
            _log.warning("detach_from_bus: unsubscribe error: %s", e, exc_info=True)


def active_subscription() -> Subscription | None:
    """Return the currently-active subscription handle, or ``None``."""
    with _active_lock:
        return _active_subscription


__all__ = [
    "attach_to_bus",
    "detach_from_bus",
    "active_subscription",
]
