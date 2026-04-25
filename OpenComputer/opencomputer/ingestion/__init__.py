"""
Ingestion — the typed-event pub/sub surface (Phase 3.A, F2).

Publishers emit :class:`plugin_sdk.ingestion.SignalEvent` values to the
shared :data:`default_bus`; subscribers fan out by event type. The bus
is in-memory only at this stage — see the TODO in
:mod:`opencomputer.ingestion.bus` for the Phase 3.D persistence plan.

Re-exports the common public surface so callers can write::

    from opencomputer.ingestion import default_bus, get_default_bus
"""

from opencomputer.ingestion.bus import (
    BackpressurePolicy,
    Subscription,
    TypedEventBus,
    default_bus,
    get_default_bus,
    reset_default_bus,
)

__all__ = [
    "BackpressurePolicy",
    "Subscription",
    "TypedEventBus",
    "default_bus",
    "get_default_bus",
    "reset_default_bus",
]
