"""Backwards-compatible shim — the trace primitives now live in plugin_sdk.

The original module landed in ``opencomputer/observability/`` and was
moved to ``plugin_sdk/trace.py`` so plugins (memory-honcho,
skill-evolution, future third-party) can import the contextvar
primitives without breaking the
``tests/test_plugin_extension_boundary.py`` SDK-boundary rule (no
``from opencomputer.*`` inside ``extensions/``).

This shim is preserved so internal call sites that already imported
from this path keep working. New code should import directly from
``plugin_sdk.trace``.

See ``plugin_sdk/trace.py`` for the actual implementation.
"""

from __future__ import annotations

from plugin_sdk.trace import (
    get_trace_id,
    new_trace_id,
    reset_trace_id,
    set_trace_id,
    trace_scope,
)

__all__ = [
    "get_trace_id",
    "new_trace_id",
    "reset_trace_id",
    "set_trace_id",
    "trace_scope",
]
