"""T69 — auth.json + Claude Code credentials discovery (re-export shim).

The implementation now lives at :mod:`plugin_sdk.auth_discovery` so
bundled extension plugins (which must not import from ``opencomputer.*``)
can consume it directly. This module re-exports the public surface for
``opencomputer.*`` callers that prefer the original import path.
"""

from __future__ import annotations

from plugin_sdk.auth_discovery import (
    discover_anthropic_credential,
    load_auth_json,
)

__all__ = [
    "discover_anthropic_credential",
    "load_auth_json",
]
