"""Tests for :class:`opencomputer.user_model.importer.MotifImporter`."""

from __future__ import annotations

from pathlib import Path

from opencomputer.inference.storage import MotifStore
from opencomputer.user_model.importer import MotifImporter
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.inference import Motif


def _fresh(tmp_path: Path) -> tuple[MotifImporter, UserModelStore, MotifStore]:
    """Build isolated store + motif-store + importer for one test."""
    user_store = UserModelStore(db_path=tmp_path / "graph.sqlite")
    motif_store = MotifStore(db_path=tmp_path / "motifs.sqlite")
    imp = MotifImporter(store=user_store, motif_store=motif_store)
    return imp, user_store, motif_store


def test_import_temporal_motif_creates_attribute_and_preference_nodes(
    tmp_path: Path,
) -> None:
    """Temporal motif → one attribute node + one preference node + one edge."""
    imp, store, motif_store = _fresh(tmp_path)
    motif = Motif(
        kind="temporal",
        confidence=0.7,
        support=8,
        summary="temporal-test",
        payload={
            "label": "Read",
            "hour": 9,
            "day_of_week": 1,  # Tuesday
            "count": 8,
        },
        evidence_event_ids=("e1",),
    )
    motif_store.insert(motif)

    n_added, e_added = imp.import_recent()
    assert n_added == 2
    assert e_added == 1

    attrs = store.list_nodes(kinds=["attribute"])
    prefs = store.list_nodes(kinds=["preference"])
    assert {n.value for n in attrs} == {"uses Read"}
    # "prefers Tuesday 09:00 for Read" — weekday name comes from mapping.
    assert any("Tuesday 09:00 for Read" in p.value for p in prefs)

    edges = store.list_edges()
    assert len(edges) == 1
    assert edges[0].kind == "asserts"
    assert edges[0].source_reliability == 0.6


def test_import_transition_motif_creates_two_attributes_and_derives_from_edge(
    tmp_path: Path,
) -> None:
    """Transition motif → two attributes + one ``derives_from`` edge."""
    imp, store, motif_store = _fresh(tmp_path)
    motif = Motif(
        kind="transition",
        confidence=0.8,
        support=5,
        summary="transition-test",
        payload={
            "prev": "Read",
            "curr": "Bash",
            "count": 5,
            "probability": 0.5,
        },
        evidence_event_ids=("e1", "e2"),
    )
    motif_store.insert(motif)

    n_added, e_added = imp.import_recent()
    assert n_added == 2
    assert e_added == 1

    attrs = {n.value for n in store.list_nodes(kinds=["attribute"])}
    assert attrs == {"runs Read", "runs Bash"}

    edges = store.list_edges()
    assert len(edges) == 1
    assert edges[0].kind == "derives_from"
    # curr `derives_from` prev → from_node is the "runs Bash" attr.
    curr_node = next(n for n in store.list_nodes() if n.value == "runs Bash")
    prev_node = next(n for n in store.list_nodes() if n.value == "runs Read")
    assert edges[0].from_node == curr_node.node_id
    assert edges[0].to_node == prev_node.node_id
    assert edges[0].evidence.get("probability") == 0.5


def test_import_implicit_goal_creates_goal_and_attribute_nodes(
    tmp_path: Path,
) -> None:
    """Implicit-goal motif → one goal + up to 3 attribute nodes + edges."""
    imp, store, motif_store = _fresh(tmp_path)
    motif = Motif(
        kind="implicit_goal",
        confidence=0.9,
        support=12,
        summary="impl-goal-test",
        session_id="s-abc",
        payload={
            "session_id": "s-abc",
            "top_tools": ["Read", "Bash", "Grep", "Write", "Glob"],
            "n_events": 12,
            "n_distinct_tools": 5,
        },
        evidence_event_ids=("e1", "e2", "e3"),
    )
    motif_store.insert(motif)

    n_added, e_added = imp.import_recent()
    # 1 goal + 3 attribute nodes = 4 new.
    assert n_added == 4
    # One edge per top-tool attribute attached to the goal.
    assert e_added == 3

    goals = store.list_nodes(kinds=["goal"])
    attrs = store.list_nodes(kinds=["attribute"])
    assert len(goals) == 1
    assert "Read-led" in goals[0].value
    assert {n.value for n in attrs} == {"uses Read", "uses Bash", "uses Grep"}

    edges = store.list_edges()
    assert len(edges) == 3
    # All edges are ``derives_from`` pointing from goal to each attribute.
    assert all(e.kind == "derives_from" for e in edges)
    assert all(e.from_node == goals[0].node_id for e in edges)


def test_import_idempotent_for_same_motif(tmp_path: Path) -> None:
    """Running the importer twice over the same motif doesn't double-create
    nodes, thanks to upsert on ``(kind, value)``."""
    imp, store, _ = _fresh(tmp_path)
    motif = Motif(
        kind="temporal",
        confidence=0.6,
        support=4,
        summary="",
        payload={"label": "Read", "hour": 10, "day_of_week": 2, "count": 4},
        evidence_event_ids=("e1",),
    )
    imp.motif_store.insert(motif)

    imp.import_recent()
    n1_count = store.count_nodes()
    e1_count = store.count_edges()

    # Re-run — same motif, upserts should keep nodes stable.
    imp.import_recent()
    n2_count = store.count_nodes()
    # Node count unchanged (upsert ignores duplicates).
    assert n1_count == n2_count
    # Edge count DOES grow — edges carry fresh UUIDs on each import.
    # Phase 3.D drift detection is responsible for folding duplicates.
    e2_count = store.count_edges()
    assert e2_count > e1_count


def test_import_returns_count_tuple(tmp_path: Path) -> None:
    """Return type is a ``(nodes_added, edges_added)`` tuple of ints."""
    imp, _, motif_store = _fresh(tmp_path)
    motif_store.insert(
        Motif(
            kind="temporal",
            confidence=0.5,
            support=3,
            summary="",
            payload={"label": "Write", "hour": 15, "day_of_week": 3, "count": 3},
            evidence_event_ids=(),
        )
    )
    result = imp.import_recent()
    assert isinstance(result, tuple)
    assert len(result) == 2
    assert all(isinstance(v, int) for v in result)


def test_import_skips_motif_with_empty_label(tmp_path: Path) -> None:
    """Temporal motif with missing label is skipped — no partial graph."""
    imp, store, motif_store = _fresh(tmp_path)
    motif_store.insert(
        Motif(
            kind="temporal",
            confidence=0.5,
            support=3,
            summary="",
            payload={"label": "", "hour": 9, "day_of_week": 1},
            evidence_event_ids=(),
        )
    )
    n_added, e_added = imp.import_recent()
    assert n_added == 0
    assert e_added == 0
    assert store.count_nodes() == 0


def test_import_filters_by_since(tmp_path: Path) -> None:
    """``since=`` is passed through to MotifStore.list."""
    imp, _, motif_store = _fresh(tmp_path)
    old = Motif(
        kind="temporal",
        confidence=0.5,
        support=3,
        summary="",
        payload={"label": "Read", "hour": 9, "day_of_week": 1},
        evidence_event_ids=(),
        created_at=1000.0,
    )
    new = Motif(
        kind="temporal",
        confidence=0.5,
        support=3,
        summary="",
        payload={"label": "Bash", "hour": 9, "day_of_week": 1},
        evidence_event_ids=(),
        created_at=9_999_999_999.0,
    )
    motif_store.insert(old)
    motif_store.insert(new)

    # Only the "new" motif should be imported.
    imp.import_recent(since=5_000_000_000.0)
    attrs = {n.value for n in imp.store.list_nodes(kinds=["attribute"])}
    assert "uses Bash" in attrs
    assert "uses Read" not in attrs
