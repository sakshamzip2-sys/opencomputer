"""M2 T2.3 — MotifImporter is edge-idempotent.

Before this, every _import_* inserted a fresh-UUID edge, so the 5-minute
cron tick re-importing the same motifs grew the edge table without bound
(393 K edges for ~180 nodes on the dogfood profile). Edges now carry a
deterministic id derived from (kind, from, to, source), so re-importing
the same motif REPLACEs rather than duplicates.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.inference.storage import MotifStore
from opencomputer.user_model.importer import (
    MotifImporter,
    _deterministic_edge_id,
)
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.inference import Motif


def _fresh(tmp_path: Path) -> tuple[MotifImporter, UserModelStore, MotifStore]:
    user_store = UserModelStore(db_path=tmp_path / "graph.sqlite")
    motif_store = MotifStore(db_path=tmp_path / "motifs.sqlite")
    return MotifImporter(store=user_store, motif_store=motif_store), user_store, motif_store


def test_deterministic_edge_id_is_stable_and_distinct() -> None:
    """Same identity tuple → same id; any difference → different id."""
    a = _deterministic_edge_id("asserts", "n1", "n2", "motif_importer")
    b = _deterministic_edge_id("asserts", "n1", "n2", "motif_importer")
    assert a == b
    assert a != _deterministic_edge_id("asserts", "n1", "n3", "motif_importer")
    assert a != _deterministic_edge_id("derives_from", "n1", "n2", "motif_importer")
    assert a != _deterministic_edge_id("asserts", "n1", "n2", "honcho_synthesis")


def test_reimport_same_temporal_motif_does_not_grow_edges(tmp_path: Path) -> None:
    """Importing the same temporal motif twice yields one edge, not two."""
    imp, store, motif_store = _fresh(tmp_path)
    motif_store.insert(Motif(
        kind="temporal", confidence=0.7, support=8, summary="t",
        payload={"label": "Read", "hour": 9, "day_of_week": 1, "count": 8},
        evidence_event_ids=("e1",),
    ))
    imp.import_recent()
    imp.import_recent()  # the 5-minute cron tick, again
    assert store.count_edges() == 1
    assert store.count_nodes() == 2


def test_reimport_same_transition_motif_does_not_grow_edges(
    tmp_path: Path,
) -> None:
    """Transition motif re-import is idempotent on edges too."""
    imp, store, motif_store = _fresh(tmp_path)
    motif_store.insert(Motif(
        kind="transition", confidence=0.8, support=5, summary="x",
        payload={"prev": "Read", "curr": "Bash", "count": 5,
                 "probability": 0.9},
        evidence_event_ids=("e1",),
    ))
    imp.import_recent()
    imp.import_recent()
    imp.import_recent()
    assert store.count_edges() == 1


def test_distinct_motifs_keep_distinct_edges(tmp_path: Path) -> None:
    """Dedup must not over-collapse — two different motifs keep two edges."""
    imp, store, motif_store = _fresh(tmp_path)
    motif_store.insert(Motif(
        kind="temporal", confidence=0.7, support=8, summary="a",
        payload={"label": "Read", "hour": 9, "day_of_week": 1, "count": 8},
        evidence_event_ids=("e1",),
    ))
    motif_store.insert(Motif(
        kind="temporal", confidence=0.7, support=8, summary="b",
        payload={"label": "Write", "hour": 14, "day_of_week": 3, "count": 8},
        evidence_event_ids=("e2",),
    ))
    imp.import_recent()
    imp.import_recent()
    assert store.count_edges() == 2
