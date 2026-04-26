"""F4 ↔ Honcho hybrid bridge (Phase 4.B of catch-up plan).

Builds on Phase 4.A's ``edges.source`` provenance column. The bridge:

1. **Pushes** F4 motif-derived edges to Honcho as structured observations
   (one-way feed). Edges with ``source.startswith("honcho_")`` are
   filtered out — that's the cycle-prevention mechanism. Honcho's own
   synthesised claims must NOT feed back as new "observations" to be
   re-synthesised.

2. **Pulls** Honcho's dialectic synthesis on demand and materialises
   the result as low-confidence F4 edges tagged
   ``source="honcho_synthesis"``. Confidence is halved (max 0.5) so a
   honcho-derived edge never beats a motif-derived edge of the same
   raw claim strength.

The bridge is decoupled from the agent's main ``MemoryBridge`` —
that one handles ``(user, assistant)`` text sync. This one handles
*structured edge state*. Different concerns, different pipelines.

Real Honcho integration is deferred (needs Postgres + Deriver in CI);
this module ships with a minimal :class:`HonchoLike` protocol so it
can be exercised end-to-end with a mock. When the real provider lands,
implement the protocol on it and the bridge wires up unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from opencomputer.user_model.store import UserModelStore
from plugin_sdk.user_model import Edge

_log = logging.getLogger("opencomputer.user_model.honcho_bridge")


# ---------------------------------------------------------------------------
# Protocol — what we expect Honcho to provide
# ---------------------------------------------------------------------------


class HonchoSynthesis:
    """Result of a Honcho dialectic call.

    Plain class (not Protocol) so test fakes can subclass without
    importing the production Honcho SDK.
    """

    __slots__ = ("claim", "confidence")

    def __init__(self, claim: str, confidence: float) -> None:
        self.claim = claim
        self.confidence = confidence


class HonchoLike(Protocol):
    """The minimal surface :class:`F4HonchoBridge` needs from a provider.

    The production Honcho self-hosted provider implements this naturally
    (via the existing ``honcho_search`` / ``honcho_reasoning`` tools);
    tests use an in-memory fake.
    """

    async def observe_edge(self, edge: Edge) -> None:
        """Record a structured edge as a Honcho observation."""

    async def dialectic(self, query: str) -> HonchoSynthesis | None:
        """Ask Honcho for a synthesis claim. Return None if no answer."""


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


# Confidence ceiling for materialised honcho-synthesis edges. Even a
# 1.0 synthesis confidence lands as 0.5 in F4 — synthesis is always
# weaker evidence than direct user statements or motif-derived facts.
_HONCHO_CONFIDENCE_SCALE: float = 0.5

# Minimum threshold for a Honcho synthesis to materialise at all. Below
# this, the dialectic answer is treated as no-answer.
_MIN_SYNTHESIS_CONFIDENCE: float = 0.4


@dataclass
class F4HonchoBridge:
    """One-way F4 → Honcho feed + on-demand synthesis materialisation.

    Parameters
    ----------
    store:
        F4 graph store.
    honcho:
        Anything implementing :class:`HonchoLike`.
    confidence_scale:
        Multiplier applied to Honcho synthesis confidence before
        materialising. Default 0.5 — Honcho-derived facts should never
        outrank user-explicit or motif-derived ones.
    feed_limit:
        Cap on edges fed per ``feed_recent()`` call. Prevents a freshly
        connected Honcho instance from getting overwhelmed by a long
        history backlog. Default 200.
    """

    store: UserModelStore
    honcho: HonchoLike
    confidence_scale: float = _HONCHO_CONFIDENCE_SCALE
    feed_limit: int = 200
    _last_seen_edge_ids: set[str] = field(default_factory=set)

    # ─── F4 → Honcho one-way feed ────────────────────────────────────────

    async def feed_recent(self) -> int:
        """Push recent motif-derived edges to Honcho as observations.

        Returns the number of edges fed. Idempotent across calls — we
        track which edge ids have been pushed and skip them next time.
        Honcho-tagged edges (``source.startswith("honcho_")``) are
        always filtered out to prevent the synthesis-feeds-itself cycle.
        """
        edges = self.store.list_edges(limit=self.feed_limit)
        feedable = [
            e for e in edges
            if not e.source.startswith("honcho_")
            and e.edge_id not in self._last_seen_edge_ids
        ]
        if not feedable:
            return 0
        for edge in feedable:
            try:
                await self.honcho.observe_edge(edge)
                self._last_seen_edge_ids.add(edge.edge_id)
            except Exception as exc:  # noqa: BLE001
                _log.warning("honcho_bridge: observe_edge failed for %s: %s",
                             edge.edge_id, exc)
        return len(feedable)

    # ─── Honcho synthesis → F4 materialisation ───────────────────────────

    async def synthesize_and_materialize(
        self,
        query: str,
        *,
        anchor_node_id: str,
    ) -> Edge | None:
        """Ask Honcho for a synthesis, materialise it as a low-conf F4 edge.

        ``anchor_node_id`` is the F4 node the synthesis edge attaches to —
        typically the user's identity node, or the node that prompted
        the query. The synthesis claim becomes a *new* preference node;
        the edge is ``anchor → claim_node`` of kind ``asserts``.

        Returns the materialised :class:`Edge`, or ``None`` if the
        synthesis is below threshold or the call failed.
        """
        try:
            syn = await self.honcho.dialectic(query)
        except Exception as exc:  # noqa: BLE001
            _log.warning("honcho_bridge: dialectic failed: %s", exc)
            return None
        if syn is None or not syn.claim:
            return None
        if syn.confidence < _MIN_SYNTHESIS_CONFIDENCE:
            _log.debug("honcho_bridge: synthesis below threshold (%.2f < %.2f), skipping",
                       syn.confidence, _MIN_SYNTHESIS_CONFIDENCE)
            return None

        claim_node = self.store.upsert_node(
            kind="preference",
            value=syn.claim,
            confidence=min(1.0, syn.confidence),
        )
        materialised = Edge(
            kind="asserts",
            from_node=anchor_node_id,
            to_node=claim_node.node_id,
            salience=min(1.0, syn.confidence),
            confidence=min(1.0, syn.confidence) * self.confidence_scale,
            source_reliability=0.4,  # synthesis is less trustworthy
            evidence={"query": query, "honcho_confidence": syn.confidence},
            source="honcho_synthesis",
        )
        self.store.insert_edge(materialised)
        return materialised
