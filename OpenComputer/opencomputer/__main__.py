"""Module entry point — enables ``python -m opencomputer`` invocation.

Used by :func:`opencomputer.kanban.db._resolve_oc_executable` as the
last-resort spawn path when neither ``shutil.which("oc")`` nor a
sibling-of-``sys.executable`` script is reachable. The kanban
dispatcher hits this in environments where it was launched without
``~/.local/bin`` (or the venv's ``bin/``) on ``$PATH`` (e.g. systemd /
launchd daemons inheriting a stripped PATH).

Plain CLI users continue to invoke ``oc`` / ``opencomputer`` via the
project.scripts entry points; this module is a defensive fallback,
not a primary surface.
"""
from __future__ import annotations

from opencomputer.cli import main

if __name__ == "__main__":
    main()
