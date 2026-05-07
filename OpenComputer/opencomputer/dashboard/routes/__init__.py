"""Domain-split FastAPI routers for the v1 dashboard API.

Each module in this package exposes a ``router`` (``fastapi.APIRouter``)
that ``opencomputer/dashboard/server.py`` includes under the
``/api/v1`` prefix. Splitting by domain (vs. one 4000-LOC server file)
keeps modules focused and tests fast.

Adding a new domain: create ``routes/<domain>.py`` with a module-level
``router`` + the routes, then append it to ``ALL_ROUTERS`` below.
"""

from __future__ import annotations

from fastapi import APIRouter

from opencomputer.dashboard.routes import (  # noqa: F401  (re-exports)
    actions,
    analytics,
    cron,
    dashboard_meta,
    env,
    events,
    logs,
    models,
    oc_update,
    plugins,
    profiles,
    providers_oauth,
    sessions,
    skills,
    status,
    tools,
)
from opencomputer.dashboard.routes import (
    config as config_routes,
)

ALL_ROUTERS: list[APIRouter] = [
    status.router,
    sessions.router,
    logs.router,
    models.router,
    providers_oauth.router,
    profiles.router,
    skills.router,
    plugins.router,
    cron.router,
    config_routes.router,
    env.router,
    analytics.router,
    tools.router,
    dashboard_meta.router,
    oc_update.router,
    actions.router,
    events.router,
]

__all__ = ["ALL_ROUTERS"]
