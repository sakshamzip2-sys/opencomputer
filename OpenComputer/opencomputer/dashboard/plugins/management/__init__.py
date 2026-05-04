"""Plugins-management dashboard plugin (Wave 6.D).

Exposes a read-mostly view of every installed plugin so the dashboard
SPA can render a Plugins page. Mounted by the FastAPI host at
``/api/plugins/management/`` via the auto-discovery loop in
:mod:`opencomputer.dashboard.server`.

Hermes-equivalent: commit ``e2a490560 feat(dashboard): add Plugins page``.
This is the OC port of just the backend half — the SPA frontend bundle
is intentionally deferred (OC does not yet have a React build pipeline;
the existing kanban dashboard plugin ships a pre-built dist/ verbatim
from hermes).
"""
