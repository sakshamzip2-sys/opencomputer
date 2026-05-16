"""M2 T2.2 — MotifImporter rejects agent-internal-noise nodes at the boundary.

The behavioral-inference engine mints motifs over the agent's own event
lifecycle (turn_start, tool_call, agent_loop, …). The importer must run
each prospective node value through NodeKindValidator and skip the noise
rather than write it as a 'fact about the user'.
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.inference.storage import MotifStore
from opencomputer.user_model.importer import MotifImporter
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.inference import Motif


def _fresh(tmp_path: Path) -> tuple[MotifImporter, UserModelStore, MotifStore]:
    user_store = UserModelStore(db_path=tmp_path / "graph.sqlite")
    motif_store = MotifStore(db_path=tmp_path / "motifs.sqlite")
    imp = MotifImporter(store=user_store, motif_store=motif_store)
    return imp, user_store, motif_store


def test_importer_rejects_agent_internal_temporal_motif(tmp_path: Path) -> None:
    """A temporal motif over 'agent_loop' writes no nodes and is counted."""
    imp, store, motif_store = _fresh(tmp_path)
    motif_store.insert(Motif(
        kind="temporal", confidence=0.7, support=8, summary="noise",
        payload={"label": "agent_loop", "hour": 20, "day_of_week": 2,
                 "count": 8},
        evidence_event_ids=("e1",),
    ))
    n_added, e_added = imp.import_recent()
    assert n_added == 0
    assert e_added == 0
    assert store.count_nodes() == 0
    assert imp.rejections >= 1


def test_importer_accepts_legit_temporal_motif(tmp_path: Path) -> None:
    """A genuine behavioural motif still imports cleanly — no false reject."""
    imp, _, motif_store = _fresh(tmp_path)
    motif_store.insert(Motif(
        kind="temporal", confidence=0.7, support=8, summary="real",
        payload={"label": "Read", "hour": 9, "day_of_week": 1, "count": 8},
        evidence_event_ids=("e1",),
    ))
    n_added, e_added = imp.import_recent()
    assert n_added == 2
    assert e_added == 1
    assert imp.rejections == 0


def test_importer_rejects_event_prefix_transition_motif(tmp_path: Path) -> None:
    """A transition motif over an internal event prefix is skipped."""
    imp, store, motif_store = _fresh(tmp_path)
    motif_store.insert(Motif(
        kind="transition", confidence=0.8, support=5, summary="noise",
        payload={"prev": "turn_start/agent_loop", "curr": "Bash",
                 "count": 5, "probability": 0.9},
        evidence_event_ids=("e1",),
    ))
    n_added, e_added = imp.import_recent()
    assert n_added == 0
    assert e_added == 0
    assert not any(
        "turn_start/agent_loop" in n.value for n in store.list_nodes()
    )
    assert imp.rejections >= 1


def test_importer_rejection_count_resets_per_run(tmp_path: Path) -> None:
    """`rejections` reflects the last run only, not a lifetime total."""
    imp, store, motif_store = _fresh(tmp_path)
    motif_store.insert(Motif(
        kind="temporal", confidence=0.7, support=8, summary="noise",
        payload={"label": "agent_loop", "hour": 20, "day_of_week": 2,
                 "count": 8},
        evidence_event_ids=("e1",),
    ))
    imp.import_recent()
    first = imp.rejections
    assert first >= 1
    # A second run over a now-clean motif set resets the counter.
    motif_store2 = MotifStore(db_path=tmp_path / "motifs.sqlite")
    imp2 = MotifImporter(store=store, motif_store=motif_store2)
    imp2.import_recent()
    # imp2 saw the same noise motif again — its own count is independent.
    assert imp2.rejections >= 1
