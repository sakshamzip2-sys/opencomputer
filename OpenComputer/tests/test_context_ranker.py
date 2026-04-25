"""Tests for :class:`opencomputer.user_model.context.ContextRanker`."""

from __future__ import annotations

from pathlib import Path

from opencomputer.user_model.context import ContextRanker
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.user_model import Edge, UserModelQuery


def _store(tmp_path: Path) -> UserModelStore:
    return UserModelStore(db_path=tmp_path / "graph.sqlite")


def _wire_edge(
    store: UserModelStore,
    *,
    from_value: str,
    to_value: str,
    salience: float = 0.5,
    confidence: float = 0.5,
    recency_weight: float = 1.0,
    source_reliability: float = 0.5,
    kind: str = "asserts",
) -> None:
    """Upsert two attribute nodes and connect with an edge of given weights."""
    a = store.upsert_node(kind="attribute", value=from_value)
    b = store.upsert_node(kind="attribute", value=to_value)
    store.insert_edge(
        Edge(
            kind=kind,  # type: ignore[arg-type]
            from_node=a.node_id,
            to_node=b.node_id,
            salience=salience,
            confidence=confidence,
            recency_weight=recency_weight,
            source_reliability=source_reliability,
        )
    )


def test_rank_returns_top_k(tmp_path: Path) -> None:
    """``top_k`` bounds the output size."""
    store = _store(tmp_path)
    for i in range(10):
        _wire_edge(
            store,
            from_value=f"source-{i}",
            to_value=f"target-{i}",
            salience=0.9,
            confidence=0.9,
            source_reliability=0.9,
        )
    ranker = ContextRanker(store=store)
    snap = ranker.rank(UserModelQuery(top_k=5))
    assert len(snap.nodes) == 5
    assert not snap.truncated


def test_rank_respects_token_budget(tmp_path: Path) -> None:
    """``token_budget`` truncates the output before ``top_k``."""
    store = _store(tmp_path)
    # Each node value is ~20 chars → ~5 tokens under the /4 approx.
    for i in range(10):
        _wire_edge(
            store,
            from_value=f"attribute-source-{i}",
            to_value=f"attribute-target-{i}",
            salience=0.9,
            confidence=0.9,
            source_reliability=0.9,
        )
    ranker = ContextRanker(store=store)
    # Budget of 15 tokens → ~3 nodes under the /4 approximation.
    snap = ranker.rank(UserModelQuery(top_k=20, token_budget=15))
    assert len(snap.nodes) < 20
    assert snap.truncated


def test_rank_filters_by_kinds(tmp_path: Path) -> None:
    """``kinds=`` limits the candidate set."""
    store = _store(tmp_path)
    g = store.upsert_node(kind="goal", value="goal-1")
    p = store.upsert_node(kind="preference", value="pref-1")
    a = store.upsert_node(kind="attribute", value="attr-1")
    # Wire edges so each node scores.
    b = store.upsert_node(kind="attribute", value="attr-2")
    for n in (g, p, a):
        store.insert_edge(
            Edge(
                kind="asserts",
                from_node=n.node_id,
                to_node=b.node_id,
                salience=0.9,
                confidence=0.9,
                source_reliability=0.9,
            )
        )
    ranker = ContextRanker(store=store)
    snap = ranker.rank(UserModelQuery(kinds=("goal", "preference"), top_k=10))
    kinds_out = {n.kind for n in snap.nodes}
    assert kinds_out <= {"goal", "preference"}
    assert "attribute" not in kinds_out


def test_rank_uses_fts_when_text_provided(tmp_path: Path) -> None:
    """``text=`` routes candidates through the FTS5 search path."""
    store = _store(tmp_path)
    _wire_edge(
        store,
        from_value="uses Python",
        to_value="prefers Python on Tuesday",
        salience=0.9,
        confidence=0.9,
        source_reliability=0.9,
    )
    _wire_edge(
        store,
        from_value="uses JavaScript",
        to_value="prefers JS on Thursday",
        salience=0.9,
        confidence=0.9,
        source_reliability=0.9,
    )
    ranker = ContextRanker(store=store)
    snap = ranker.rank(UserModelQuery(text="Python", top_k=10))
    values = [n.value for n in snap.nodes]
    assert all("Python" in v for v in values)
    assert not any("JavaScript" in v for v in values)


def test_score_combines_all_four_factors(tmp_path: Path) -> None:
    """Edge score uses all four factors multiplicatively.

    Build three candidates with controlled factors and check the
    ranker returns them in the expected order: higher product first.
    """
    store = _store(tmp_path)
    sink = store.upsert_node(kind="attribute", value="sink")

    # high: 0.9 × 0.9 × 1.0 × 0.9 = 0.729
    high = store.upsert_node(kind="attribute", value="high-edge-candidate")
    store.insert_edge(
        Edge(
            kind="asserts",
            from_node=high.node_id,
            to_node=sink.node_id,
            salience=0.9,
            confidence=0.9,
            recency_weight=1.0,
            source_reliability=0.9,
        )
    )
    # mid: 0.5 × 0.5 × 0.8 × 0.6 = 0.120
    mid = store.upsert_node(kind="attribute", value="mid-edge-candidate")
    store.insert_edge(
        Edge(
            kind="asserts",
            from_node=mid.node_id,
            to_node=sink.node_id,
            salience=0.5,
            confidence=0.5,
            recency_weight=0.8,
            source_reliability=0.6,
        )
    )
    # low: 0.2 × 0.2 × 0.3 × 0.2 = 0.0024
    low = store.upsert_node(kind="attribute", value="low-edge-candidate")
    store.insert_edge(
        Edge(
            kind="asserts",
            from_node=low.node_id,
            to_node=sink.node_id,
            salience=0.2,
            confidence=0.2,
            recency_weight=0.3,
            source_reliability=0.2,
        )
    )

    ranker = ContextRanker(store=store)
    snap = ranker.rank(UserModelQuery(kinds=("attribute",), top_k=10))
    # Strip the sink (it has only *incoming* edges — same score as any
    # of its incident edges, so it's comparable but not our focus).
    scored_values = [n.value for n in snap.nodes if n.value != "sink"]
    assert scored_values.index("high-edge-candidate") < scored_values.index(
        "mid-edge-candidate"
    )
    assert scored_values.index("mid-edge-candidate") < scored_values.index(
        "low-edge-candidate"
    )


def test_rank_returns_snapshot_with_incident_edges(tmp_path: Path) -> None:
    """Snapshot ``edges`` field includes incident edges of selected nodes."""
    store = _store(tmp_path)
    _wire_edge(
        store,
        from_value="a-val",
        to_value="b-val",
        salience=0.8,
        confidence=0.8,
        source_reliability=0.8,
    )
    ranker = ContextRanker(store=store)
    snap = ranker.rank(UserModelQuery(top_k=10))
    assert len(snap.nodes) >= 1
    assert len(snap.edges) >= 1
    # total_score is non-zero when at least one edge-backed node was selected.
    assert snap.total_score > 0.0


def test_rank_orphan_node_gets_base_score(tmp_path: Path) -> None:
    """Nodes with no incident edges still land in the output at a low score."""
    store = _store(tmp_path)
    store.upsert_node(kind="attribute", value="orphan", confidence=0.7)
    ranker = ContextRanker(store=store)
    snap = ranker.rank(UserModelQuery(top_k=10))
    assert len(snap.nodes) == 1
    assert snap.nodes[0].value == "orphan"
    # Orphan score = 0.7 * 0.5 = 0.35 > 0.
    assert snap.total_score > 0.0


def test_rank_empty_store_returns_empty_snapshot(tmp_path: Path) -> None:
    """No candidates → empty snapshot with zero total_score."""
    store = _store(tmp_path)
    ranker = ContextRanker(store=store)
    snap = ranker.rank(UserModelQuery(top_k=10))
    assert snap.nodes == ()
    assert snap.edges == ()
    assert snap.total_score == 0.0
    assert not snap.truncated
