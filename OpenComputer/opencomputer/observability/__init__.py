"""Observability surface — logging, metrics, traces.

Round 2B P-4 introduced this package with :mod:`logging_config`, the
process-wide handler/filter/formatter wiring used by the CLI and the
gateway daemon. Future observability primitives (tracing, structured
metrics) belong here too so consumers have a single import path.
"""

from __future__ import annotations

from opencomputer.observability.logging_config import (
    RedactingFormatter,
    SessionContextFilter,
    configure,
    set_session_id,
)

__all__ = [
    "RedactingFormatter",
    "SessionContextFilter",
    "configure",
    "set_session_id",
]
