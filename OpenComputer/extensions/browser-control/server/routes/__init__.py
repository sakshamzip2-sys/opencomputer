"""HTTP route registration.

Each module exports a ``register(app, ctx)`` function that mounts its
routes onto the FastAPI app. The grand total is ~46 routes across the
five route modules, matching OpenClaw's surface (deep dive §7).
"""

from __future__ import annotations

from typing import Any

from .agent import register as register_agent
from .basic import register as register_basic
from .observe import register as register_observe
from .storage import register as register_storage
from .tabs import register as register_tabs

__all__ = ["register_all"]


def register_all(app: Any, ctx: Any) -> None:
    """Mount every router onto ``app`` in a stable order."""
    register_basic(app, ctx)
    register_tabs(app, ctx)
    register_agent(app, ctx)
    register_storage(app, ctx)
    register_observe(app, ctx)
