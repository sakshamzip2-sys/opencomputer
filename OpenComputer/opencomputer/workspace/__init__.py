"""``oc workspace`` — Hermes Workspace integration.

Submodule layout::

    workspace/
    ├── __init__.py         ← this file (public API surface)
    ├── discovery.py        ← locate the hermes-workspace dir on disk
    ├── prerequisites.py    ← detect node / pnpm with version gates
    ├── builder.py          ← `pnpm install` + `pnpm build` with caching
    ├── launcher.py         ← spawn `node server-entry.js` + lifecycle
    └── lifecycle.py        ← coordinate dashboard + workspace boot order

The top-level CLI surface ``opencomputer.cli_workspace`` is the only
consumer of these modules. Plugin authors do NOT import from
``opencomputer.workspace`` — it is an internal package.
"""

from __future__ import annotations

from opencomputer.workspace.discovery import (
    IN_REPO_WORKSPACE_PATH,
    WorkspaceNotFoundError,
    discover_workspace_dir,
)
from opencomputer.workspace.prerequisites import (
    PrerequisiteStatus,
    check_prerequisites,
)

__all__ = [
    "IN_REPO_WORKSPACE_PATH",
    "PrerequisiteStatus",
    "WorkspaceNotFoundError",
    "check_prerequisites",
    "discover_workspace_dir",
]
