"""Web UI dashboard (Phase 8.A of catch-up plan).

Minimal browser frontend on top of the existing Wire server. Built with
the stdlib ``http.server`` to avoid pulling FastAPI/Starlette/Uvicorn
into the dependency set — Phase 8.A's job is to land a "open browser,
chat with agent" demo, not to ship production-grade middleware.

Layout::

    opencomputer/dashboard/
    ├── __init__.py         — re-exports
    ├── server.py           — DashboardServer (stdlib http.server)
    └── static/
        └── index.html      — vanilla HTML + JS, connects to wire ws

The HTML page connects to the existing Wire server (running on
``ws://127.0.0.1:18789``) via the JSON-RPC v2 protocol. No build step,
no node_modules. When the user wants polish (React/Tailwind/shadcn),
that's Phase 8.C — and it's a separate frontend project, not buried
inside the framework.

CLI entrypoint: ``opencomputer dashboard``. Default bind 127.0.0.1
only; non-localhost binding requires the ``dashboard.bind_external``
consent capability.
"""

from __future__ import annotations

from opencomputer.dashboard.server import DashboardServer, make_handler

__all__ = ["DashboardServer", "make_handler"]
