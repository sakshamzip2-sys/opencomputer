"""Dashboard — FastAPI host for the SPA + plugin routers + PTY (Wave 6.D).

Layout::

    opencomputer/dashboard/
    ├── __init__.py         — re-exports
    ├── server.py           — DashboardServer (FastAPI + uvicorn)
    ├── pty_bridge.py       — PtyBridge (POSIX-only, hermes-port)
    ├── plugins/            — auto-mounted at /api/plugins/<name>/
    │   └── kanban/         — example plugin (db + dashboard UI)
    └── static/
        └── index.html      — SPA shell (vanilla HTML + JS or built bundle)

Wave 6.D migration (2026-05-04): the original stdlib ``http.server``
implementation has been replaced by FastAPI + uvicorn. FastAPI is a
hard dep already (W2b), so this no longer adds anything.

CLI entrypoint: ``oc dashboard``. Default bind 127.0.0.1 only;
non-localhost binding requires the ``dashboard.bind_external`` consent
capability.
"""

from __future__ import annotations

from opencomputer.dashboard.server import DashboardServer, build_app

__all__ = ["DashboardServer", "build_app"]
