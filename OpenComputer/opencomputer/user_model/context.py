"""
Context weighting — rank nodes by their incident-edge score (Phase 3.C).

The ranker turns a :class:`UserModelQuery` into a
:class:`UserModelSnapshot` ordered by the four-factor score::

    score = salience × confidence × recency_weight × source_reliability

One score per *incident edge*; the node's score is the max across its
edges. Nodes with no incident edges fall back to ``node.confidence * 0.5``
— low enough that orphan nodes lose to any edge-backed candidate but
not zero (so a freshly-inserted node is still reachable to a future
explicit query).

Output is capped at :attr:`UserModelQuery.top_k` OR truncated by
:attr:`UserModelQuery.token_budget` (approximated as ``len(value) / 4``
characters per token), whichever fires first.
"""

from __future__ import annotations

from opencomputer.user_model.store import UserModelStore
from plugin_sdk.user_model import Edge, Node, UserModelQuery, UserModelSnapshot


def _edge_score(edge: Edge) -> float:
    """Four-factor score for a single edge — clamped to ``[0.0, 1.0]``.

    Each factor is already a ``[0.0, 1.0]`` value; the product is
    therefore in the same range. We clamp defensively for the case
    where a corrupt row carries out-of-range values (e.g. a plugin
    writes ``salience=2.0`` and we don't want one bad row to dominate).
    """
    raw = (
        edge.salience
        * edge.confidence
        * edge.recency_weight
        * edge.source_reliability
    )
    return max(0.0, min(1.0, raw))


def _token_approx(value: str) -> int:
    """Cheap token estimate — 4 chars ≈ 1 token. Used only for budgeting."""
    return max(1, len(value) // 4)


class ContextRanker:
    """Rank candidate nodes for context injection.

    Parameters
    ----------
    store:
        Source graph store. ``None`` uses the default path.
    """

    #: Multiplier applied to ``node.confidence`` for nodes without
    #: incident edges. Values above 1.0 would let orphan nodes beat
    #: edge-backed ones, which defeats the purpose of the ranker.
    _ORPHAN_CONFIDENCE_FACTOR = 0.5

    #: Candidate multiplier — we over-fetch by this factor so the
    #: scoring pass has enough to pick from before top-K cutoff.
    _CANDIDATE_MULTIPLIER = 3

    def __init__(self, store: UserModelStore | None = None) -> None:
        self.store = store if store is not None else UserModelStore()

    def rank(self, query: UserModelQuery) -> UserModelSnapshot:
        """Return the ranked :class:`UserModelSnapshot` for ``query``."""
        candidates = self._fetch_candidates(query)
        # Score each candidate and attach its incident edges for the
        # snapshot. Incident includes both incoming and outgoing edges
        # — the consumer may want to display "why" traces either way.
        scored: list[tuple[float, Node, tuple[Edge, ...]]] = []
        for node in candidates:
            incident = self._incident_edges(node.node_id)
            if incident:
                score = max(_edge_score(e) for e in incident)
            else:
                # Orphan fallback — low but non-zero so freshly-inserted
                # nodes are reachable.
                score = node.confidence * self._ORPHAN_CONFIDENCE_FACTOR
            scored.append((score, node, incident))

        # Sort descending by score, tie-break by last_seen_at so recent
        # re-assertions bubble up over stale siblings.
        scored.sort(key=lambda t: (t[0], t[1].last_seen_at), reverse=True)

        selected_nodes: list[Node] = []
        selected_edges: list[Edge] = []
        seen_edge_ids: set[str] = set()
        total_score = 0.0
        token_used = 0
        truncated = False

        for score, node, incident in scored:
            if len(selected_nodes) >= query.top_k:
                # We stopped because of top_k, not because of budget —
                # that's not "truncated" in the sense the caller cares
                # about (it's what they asked for).
                break
            if query.token_budget is not None:
                cost = _token_approx(node.value)
                if token_used + cost > query.token_budget:
                    truncated = True
                    break
                token_used += cost
            selected_nodes.append(node)
            total_score += score
            for e in incident:
                if e.edge_id not in seen_edge_ids:
                    selected_edges.append(e)
                    seen_edge_ids.add(e.edge_id)

        return UserModelSnapshot(
            nodes=tuple(selected_nodes),
            edges=tuple(selected_edges),
            total_score=total_score,
            truncated=truncated,
        )

    # ─── helpers ──────────────────────────────────────────────────────

    def _fetch_candidates(self, query: UserModelQuery) -> list[Node]:
        """Build the candidate set honoring FTS / kinds / defaults.

        Over-fetches by :attr:`_CANDIDATE_MULTIPLIER` so the scoring
        pass has room to discriminate before top-K cutoff.
        """
        target = max(query.top_k * self._CANDIDATE_MULTIPLIER, query.top_k)
        if query.text:
            return self.store.search_nodes_fts(query.text, limit=target)
        if query.kinds:
            return self.store.list_nodes(kinds=list(query.kinds), limit=target)
        return self.store.list_nodes(limit=target)

    def _incident_edges(self, node_id: str) -> tuple[Edge, ...]:
        """Return the union of incoming + outgoing edges for ``node_id``.

        Dedup by ``edge_id`` protects against the (unusual but legal)
        case of a self-loop — one edge should appear once.
        """
        outgoing = self.store.list_edges(from_node=node_id)
        incoming = self.store.list_edges(to_node=node_id)
        seen: dict[str, Edge] = {}
        for e in (*outgoing, *incoming):
            seen.setdefault(e.edge_id, e)
        return tuple(seen.values())


__all__ = ["ContextRanker"]
