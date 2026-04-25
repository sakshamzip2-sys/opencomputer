"""User-model graph package — Phase 3.C (F4 layer).

The user-model graph sits between 3.B behavioral motifs and the agent
context-assembly path. It holds stable, typed entities (:class:`Node`)
and typed assertions (:class:`Edge`) that the ranker consults when
building per-turn context.

Public re-exports for callers that want a single import:

* :class:`UserModelStore` — SQLite + FTS5 backing store.
* :class:`MotifImporter` — converts 3.B motifs into nodes + edges.
* :class:`ContextRanker` — four-factor scoring + top-K selection.

Third-party plugins should import the dataclass vocabulary from
``plugin_sdk.user_model``, not from here; the ``opencomputer/*`` package
is internal and may change without warning.
"""

from opencomputer.user_model.context import ContextRanker
from opencomputer.user_model.decay import DecayEngine
from opencomputer.user_model.drift import DriftDetector
from opencomputer.user_model.drift_store import DriftStore
from opencomputer.user_model.importer import MotifImporter
from opencomputer.user_model.scheduler import DecayDriftScheduler
from opencomputer.user_model.store import UserModelStore

__all__ = [
    "ContextRanker",
    "DecayDriftScheduler",
    "DecayEngine",
    "DriftDetector",
    "DriftStore",
    "MotifImporter",
    "UserModelStore",
]
