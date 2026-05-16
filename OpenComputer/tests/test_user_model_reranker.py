"""M3 T3.1 — tests for UserFactsReranker.

Each scoring term is exercised in isolation (hold the others equal),
then the composite + context-free fallback + custom weights.
"""
from __future__ import annotations

from opencomputer.user_model.reranker import (
    RerankWeights,
    ScoredFact,
    SessionContext,
    UserFactsReranker,
)
from plugin_sdk.user_model import Node, NodeKind

_NOW = 1_000_000_000.0


def _node(
    kind: NodeKind = "attribute",
    value: str = "x",
    confidence: float = 0.5,
    age_days: float = 0.0,
) -> Node:
    return Node(
        kind=kind, value=value, confidence=confidence,
        last_seen_at=_NOW - age_days * 86400.0,
    )


def test_score_returns_sorted_scored_facts() -> None:
    """Output is ScoredFact objects, highest composite score first."""
    r = UserFactsReranker()
    out = r.score(
        [_node(kind="attribute"), _node(kind="identity")],
        SessionContext(), now=_NOW,
    )
    assert all(isinstance(s, ScoredFact) for s in out)
    assert out[0].score >= out[1].score


def test_identity_outranks_attribute_all_else_equal() -> None:
    """Kind priority: identity beats attribute when other terms match."""
    r = UserFactsReranker()
    out = r.score(
        [_node(kind="attribute", value="a"),
         _node(kind="identity", value="b")],
        SessionContext(), now=_NOW,
    )
    assert out[0].node.kind == "identity"


def test_higher_confidence_ranks_higher() -> None:
    """Confidence term: same kind/recency, higher confidence wins."""
    r = UserFactsReranker()
    out = r.score(
        [_node(value="low", confidence=0.2),
         _node(value="high", confidence=0.95)],
        SessionContext(), now=_NOW,
    )
    assert out[0].node.value == "high"


def test_recent_fact_outranks_stale_fact() -> None:
    """Recency term: same kind/confidence, fresher last_seen_at wins."""
    r = UserFactsReranker()
    out = r.score(
        [_node(value="stale", age_days=200.0),
         _node(value="fresh", age_days=0.0)],
        SessionContext(), now=_NOW,
    )
    assert out[0].node.value == "fresh"


def test_bm25_boosts_session_relevant_fact() -> None:
    """BM25 term: the fact relevant to the conversation rises."""
    r = UserFactsReranker()
    ctx = SessionContext(recent_messages=("I am learning rust this week",))
    out = r.score(
        [_node(value="enjoys cooking pasta"),
         _node(value="learning rust programming")],
        ctx, now=_NOW,
    )
    assert out[0].node.value == "learning rust programming"


def test_context_free_mode_drops_bm25_term() -> None:
    """No session messages → BM25 term is 0 and weights renormalise."""
    r = UserFactsReranker()
    out = r.score([_node()], SessionContext(), now=_NOW)
    assert out[0].breakdown["bm25"] == 0.0
    # Composite still spans a sensible range (weights renormalised).
    assert 0.0 <= out[0].score <= 1.0


def test_breakdown_carries_all_four_terms() -> None:
    """Every ScoredFact exposes the per-term breakdown for `explain`."""
    r = UserFactsReranker()
    out = r.score(
        [_node()], SessionContext(recent_messages=("hello",)), now=_NOW,
    )
    assert set(out[0].breakdown) == {"kind", "confidence", "recency", "bm25"}


def test_composite_score_stays_in_unit_range() -> None:
    """The blended score is always in [0, 1] — with and without context."""
    r = UserFactsReranker()
    nodes = [_node(kind="identity", confidence=1.0),
             _node(kind="attribute", confidence=0.0, age_days=999.0)]
    for ctx in (SessionContext(), SessionContext(recent_messages=("x y",))):
        for s in r.score(nodes, ctx, now=_NOW):
            assert 0.0 <= s.score <= 1.0


def test_empty_node_list_returns_empty() -> None:
    """Scoring zero candidates is a clean empty result."""
    assert UserFactsReranker().score([], SessionContext(), now=_NOW) == []


def test_custom_weights_isolate_a_single_term() -> None:
    """Weights are honoured — a bm25-only weighting ranks purely by BM25."""
    r = UserFactsReranker(
        RerankWeights(kind=0.0, confidence=0.0, recency=0.0, bm25=1.0)
    )
    ctx = SessionContext(recent_messages=("python python python",))
    out = r.score(
        [_node(kind="identity", value="name nobody", confidence=1.0),
         _node(kind="attribute", value="writes python code")],
        ctx, now=_NOW,
    )
    # Despite the identity kind + max confidence, the python-matching
    # attribute wins because only BM25 is weighted.
    assert out[0].node.value == "writes python code"
    assert out[0].score > 0.0
