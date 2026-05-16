"""
User-model graph primitives — public vocabulary for Phase 3.C (F4 layer).

This module is the SDK contract for the user-model graph shipped in
:mod:`opencomputer.user_model`. The graph is a lightweight semantic
layer over behavioral motifs (Phase 3.B) plus — eventually — explicit
user statements and drift signals (Phase 3.D). Plugins and the agent
context-assembly path read nodes + edges through this module without
reaching into ``opencomputer/*`` internals.

Model
-----

* **Nodes** are stable entities the agent tracks about the user:
  identity facts, attributes ("prefers Python"), relationships,
  goals, and preferences. Each node carries a node-level confidence
  aggregated from incoming edges.
* **Edges** are typed assertions between nodes. Four edge kinds
  capture the lifecycle:

  - ``asserts`` — A asserts B (most common; motif → preference, etc.)
  - ``contradicts`` — A contradicts B (Phase 3.D drift signal)
  - ``supersedes`` — A supersedes B (preference revision)
  - ``derives_from`` — A was derived from B (provenance edge)

Per-edge metadata drives the :class:`ContextRanker` scoring formula:
``salience × confidence × recency_weight × source_reliability``.

Stability contract
------------------

This module is part of the public SDK. Once the field set is shipped,
renaming/removing/re-typing a field is a **breaking change**. Adding
optional fields with safe defaults is fine; new ``kind`` literal values
require a version bump on consumer code that matches on the discriminator.

Privacy posture
---------------

Mirrors the bus + motif stances: values carry labels, preferences,
and goal strings — never raw message bodies. Evidence chains point
back to motif ids and event ids for traceability, not to content.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

#: Five node kinds covering the user-model taxonomy. Extending this
#: tuple is a **breaking change** — consumers dispatch on the literal.
NodeKind = Literal["identity", "attribute", "relationship", "goal", "preference"]

#: Four edge kinds covering assertion lifecycle. Extending this tuple
#: is a **breaking change** — downstream scorers depend on the set.
EdgeKind = Literal["asserts", "contradicts", "supersedes", "derives_from"]


@dataclass(frozen=True, slots=True)
class Node:
    """A stable user-model entity.

    Attributes
    ----------
    node_id:
        Stable UUID4 per node, auto-generated on construction. Primary
        key in :class:`opencomputer.user_model.store.UserModelStore`.
    kind:
        Discriminator literal — one of :data:`NodeKind`.
    value:
        The actual content, human-readable. Examples by kind:

        * ``attribute``: ``"prefers Python over JS"``
        * ``goal``: ``"learn Rust by Q3"``
        * ``preference``: ``"prefers Tuesday 09:00 for Read"``

        Stored as-is and used as the FTS5 indexed column. Do not embed
        structured data — use ``metadata`` for that.
    created_at:
        Unix epoch seconds at which the node was first materialised.
    last_seen_at:
        Unix epoch seconds for the most recent edge asserting this
        node. Defaults to ``created_at``. Bumped by
        :meth:`UserModelStore.upsert_node` on repeat assertions.
    confidence:
        Node-level prior in ``[0.0, 1.0]``. Updated by aggregation of
        incoming edges; defaults to 0.5 for freshly-inserted nodes.
    metadata:
        Extensible JSON-serialisable map. Reserved today for plugin
        use + future edge aggregation hints. Not used by the
        :class:`ContextRanker` score formula.
    """

    kind: NodeKind = "attribute"
    value: str = ""
    confidence: float = 0.5
    node_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Edge:
    """A typed assertion between two nodes.

    Attributes
    ----------
    edge_id:
        Stable UUID4 per edge, auto-generated on construction.
    kind:
        Discriminator literal — one of :data:`EdgeKind`.
    from_node:
        ``node_id`` of the source node.
    to_node:
        ``node_id`` of the target node.
    salience:
        Range ``[0.0, 1.0]``. How important this edge is in the ranked
        context. Motif-derived edges scale salience by support count;
        explicit user statements land at 1.0.
    confidence:
        Range ``[0.0, 1.0]``. How sure we are about the assertion.
        Propagates from the source motif / user statement.
    recency_weight:
        Range ``[0.0, 1.0]``. Recently-asserted edges weighted higher.
        Starts at 1.0 and is decayed by Phase 3.D background task via
        :meth:`UserModelStore.update_edge_recency_weight`.
    source_reliability:
        Range ``[0.0, 1.0]``. Provenance trust:

        * explicit user statement → 1.0
        * motif-derived (temporal, transition) → 0.6
        * implicit-goal motif → 0.5
        * scraped / external → 0.4
    decay_rate:
        Per-day exponential decay rate applied by Phase 3.D. Default
        0.01/day — an edge loses ~1% of its weight per day absent
        re-assertion.
    created_at:
        Unix epoch seconds at which the edge was materialised.
    evidence:
        JSON-serialisable provenance map. Canonical shape:
        ``{"motif_id": "...", "event_ids": [...]}``. Kept small —
        persisted as a JSON blob in SQLite.
    """

    kind: EdgeKind = "asserts"
    from_node: str = ""
    to_node: str = ""
    salience: float = 0.5
    confidence: float = 0.5
    recency_weight: float = 1.0
    source_reliability: float = 0.5
    decay_rate: float = 0.01
    edge_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    evidence: Mapping[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    """Provenance tag for cycle prevention in the F4↔Honcho hybrid (Phase 4
    of the catch-up plan). Common values:

    * ``"motif_importer"`` — derived deterministically from a Phase 3.B motif.
    * ``"honcho_synthesis"`` — materialised back from a Honcho dialectic
      claim. The motif importer skips edges with a ``"honcho_"`` prefix
      to break the synthesis-feeds-itself loop.
    * ``"user_explicit"`` — the user said it directly in chat.
    * ``"unknown"`` (default) — pre-Phase-4 edges and tests that don't care.
    """


@dataclass(frozen=True, slots=True)
class UserModelQuery:
    """Ranking query passed to :class:`ContextRanker.rank`.

    Attributes
    ----------
    kinds:
        Optional filter — restrict candidates to this subset of
        :data:`NodeKind`. ``None`` means "all kinds".
    text:
        Optional FTS5 query against ``Node.value``. When set, the
        ranker uses full-text search to build the candidate set;
        otherwise falls back to kind-filtered list.
    top_k:
        Maximum number of nodes to return. Actual output may be
        smaller if ``token_budget`` cuts us short.
    token_budget:
        Optional character budget (approximating tokens via
        ``len(value) / 4``). The ranker returns until the budget is
        exhausted and flags the snapshot as ``truncated=True``.
    """

    kinds: tuple[NodeKind, ...] | None = None
    text: str | None = None
    top_k: int = 20
    token_budget: int | None = None


@dataclass(frozen=True, slots=True)
class UserModelSnapshot:
    """The output of :meth:`ContextRanker.rank` — nodes + incident edges.

    Attributes
    ----------
    nodes:
        Ordered tuple of :class:`Node` values, most salient first.
    edges:
        Tuple of :class:`Edge` values — the union of all incident
        edges for the selected nodes (both incoming and outgoing).
        Useful for downstream consumers that want to render "why was
        this node selected" traces.
    total_score:
        Sum of per-node scores chosen. Useful for threshold-based
        consumers ("only inject if total_score > X").
    truncated:
        ``True`` if ``token_budget`` cut off the selection before
        reaching ``top_k``. A caller that sees this should either
        raise the budget or accept the coverage gap.
    """

    nodes: tuple[Node, ...] = ()
    edges: tuple[Edge, ...] = ()
    total_score: float = 0.0
    truncated: bool = False


# ---------------------------------------------------------------------------
# Write-boundary validation (awareness-cleanup Milestone 2)
# ---------------------------------------------------------------------------

#: Resolved once — the acceptable node kinds, derived from the
#: :data:`NodeKind` literal so the validator and the type never drift.
_VALID_NODE_KINDS: frozenset[str] = frozenset(get_args(NodeKind))

#: Case-insensitive substrings that mark a node value as agent-internal
#: machinery rather than a fact about the user. The behavioral-inference
#: engine mints motifs over the agent's own event lifecycle; the motif
#: importer then faithfully converts them into nodes. A value embedding
#: one of these is not user behaviour. Each token is distinctive enough
#: for a plain substring test — ``cron`` is deliberately excluded as too
#: ambiguous a substring. See ``docs/refs/oc-user-model-writers.md``.
AGENT_INTERNAL_TOKENS: tuple[str, ...] = (
    "agent_loop",
    "ambient-sensors",
    "gateway.dispatch",
    "tool_call/",
    "turn_start/",
    "turn_completed/",
    "session_start/",
    "session_end/",
    "foreground_app/",
)


@dataclass(frozen=True, slots=True)
class NodeValidation:
    """Verdict from :meth:`NodeKindValidator.check`.

    Attributes
    ----------
    valid:
        ``True`` if the prospective node write is acceptable.
    reason:
        Human-readable explanation when ``valid`` is ``False`` — suitable
        for an audit-log line. Empty string when ``valid``.
    """

    valid: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class NodeKindValidator:
    """Validate a prospective user-model node write against the taxonomy.

    Stateless and pure — it inspects only the ``(kind, value)`` pair, so
    it is safe to call from any writer. The *caller* decides what to do
    with a negative verdict: core writers (the motif importer) reject and
    log; plugin / user-explicit writers may warn only.

    The check is intentionally minimal — it catches the failure mode that
    actually pollutes the graph (agent-internal machinery imported as
    user behaviour) without inventing speculative per-kind format rules.
    See ``docs/refs/oc-user-model-writers.md`` §3.

    Attributes
    ----------
    agent_internal_tokens:
        Case-insensitive substrings that disqualify a value. Defaults to
        :data:`AGENT_INTERNAL_TOKENS`; pass a custom tuple to replace it.
    """

    agent_internal_tokens: tuple[str, ...] = AGENT_INTERNAL_TOKENS

    def check(self, kind: str, value: str) -> NodeValidation:
        """Return a :class:`NodeValidation` verdict for one node write.

        A write is rejected when ``kind`` is outside :data:`NodeKind`,
        ``value`` is empty / whitespace-only, or ``value`` embeds an
        agent-internal token.
        """
        if kind not in _VALID_NODE_KINDS:
            return NodeValidation(
                False,
                f"unknown node kind {kind!r} — not in the NodeKind taxonomy",
            )
        cleaned = (value or "").strip()
        if not cleaned:
            return NodeValidation(False, "empty node value")
        lowered = cleaned.lower()
        for token in self.agent_internal_tokens:
            if token.lower() in lowered:
                return NodeValidation(
                    False,
                    f"agent-internal label {token!r} — machinery, not a "
                    "fact about the user",
                )
        return NodeValidation(True)


__all__ = [
    "NodeKind",
    "EdgeKind",
    "Node",
    "Edge",
    "UserModelQuery",
    "UserModelSnapshot",
    "NodeValidation",
    "NodeKindValidator",
    "AGENT_INTERNAL_TOKENS",
]
