"""Quick state-snapshot subsystem (Hermes Tier 2.A port).

Archives critical state files (``sessions.db``, ``config.yaml``, ``.env``,
plus a few well-known JSON state files) to a timestamped directory under
``<profile_home>/state-snapshots/``. Auto-prunes to a fixed-keep window.

Public surface:

- :func:`create_snapshot` — make a new snapshot, optional label
- :func:`list_snapshots` — most-recent-first
- :func:`restore_snapshot` — overwrite current state from a snapshot
- :func:`prune_snapshots` — remove oldest beyond keep cap

Used by the ``/snapshot create|restore|prune`` slash command (CLI chat
loop) and reachable from any caller that has the profile home.
"""

from opencomputer.snapshot.quick import (
    DEFAULT_KEEP,
    QUICK_STATE_FILES,
    SNAPSHOTS_DIR,
    create_snapshot,
    list_snapshots,
    prune_snapshots,
    restore_snapshot,
    snapshot_root,
)

__all__ = [
    "DEFAULT_KEEP",
    "QUICK_STATE_FILES",
    "SNAPSHOTS_DIR",
    "create_snapshot",
    "list_snapshots",
    "prune_snapshots",
    "restore_snapshot",
    "snapshot_root",
]
