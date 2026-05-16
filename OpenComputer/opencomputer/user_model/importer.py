"""
Motif → user-model graph importer (Phase 3.C).

Translates 3.B :class:`plugin_sdk.inference.Motif` records into nodes +
edges via deterministic rules. Importer is idempotent: running
:meth:`MotifImporter.import_recent` twice over the same motif set
produces the same graph state, because every node is written via
``UserModelStore.upsert_node`` (find-by-``(kind, value)``) and every
edge is freshly inserted with a UUID — duplicate edges are cheap and
harmless until Phase 3.D's drift pass folds them.

``CONTRADICTS`` edges are **not** auto-emitted here. Drift detection
(Phase 3.D) and explicit user statements (future tool) own that edge
kind.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from opencomputer.inference.storage import MotifStore
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.user_model import Edge, NodeKindValidator

if TYPE_CHECKING:
    from plugin_sdk.inference import Motif

_log = logging.getLogger("opencomputer.user_model.importer")

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _weekday(day_of_week: int) -> str:
    """Return the human-readable weekday name for a ``datetime.weekday()``
    value. Out-of-range values get a safe ``"unknown"`` — importer
    should never crash on a malformed motif payload.
    """
    if 0 <= day_of_week < 7:
        return _WEEKDAY_NAMES[day_of_week]
    return "unknown"


def _deterministic_edge_id(
    kind: str, from_node: str, to_node: str, source: str
) -> str:
    """Return a stable ``edge_id`` for an importer-derived edge.

    Re-importing the same motif must not multiply edges: deriving the id
    from the edge's identity tuple makes
    :meth:`UserModelStore.insert_edge` (INSERT OR REPLACE keyed on
    ``edge_id``) idempotent. Without this, every 5-minute cron tick
    added a fresh-uuid edge — see ``docs/refs/oc-user-model-writers.md``
    §4. ``uuid5`` keeps the id in the same UUID shape as the rest of the
    schema while being a pure function of the inputs.
    """
    name = f"{kind}|{from_node}|{to_node}|{source}"
    return str(uuid.uuid5(uuid.NAMESPACE_OID, name))


class MotifImporter:
    """Convert 3.B motifs into user-model graph nodes + edges.

    Parameters
    ----------
    store:
        Destination graph store. ``None`` uses the default path
        (``<profile_home>/user_model/graph.sqlite``).
    motif_store:
        Source motif store. ``None`` uses the default path
        (``<profile_home>/inference/motifs.sqlite``).
    """

    def __init__(
        self,
        store: UserModelStore | None = None,
        motif_store: MotifStore | None = None,
    ) -> None:
        self.store = store if store is not None else UserModelStore()
        self.motif_store = motif_store if motif_store is not None else MotifStore()
        #: Write-boundary guard — keeps agent-internal machinery
        #: (turn_start, tool_call, agent_loop, …) out of the graph.
        self.validator = NodeKindValidator()
        #: Count of node writes rejected by the validator in the most
        #: recent :meth:`import_recent` call. Reset per run.
        self.rejections = 0

    def import_recent(
        self,
        *,
        since: float | None = None,
        limit: int = 100,
    ) -> tuple[int, int]:
        """Import up to ``limit`` motifs created after ``since``.

        Returns ``(nodes_added, edges_added)``. Node counts reflect
        *new* inserts only — re-asserted existing nodes count toward
        ``last_seen_at`` bumps but not the total. Edges count every
        insertion; duplicate-edge dedup is Phase 3.D's concern.

        Per-motif errors are logged and skipped — one malformed payload
        should not block a batch import.
        """
        self.rejections = 0
        motifs = self.motif_store.list(since=since, limit=limit)
        nodes_before = self.store.count_nodes()
        edges_added = 0
        for motif in motifs:
            try:
                edges_added += self._import_one(motif)
            except Exception as e:  # noqa: BLE001 — skip bad motif, keep batch
                _log.warning(
                    "motif import failed for %s (%s): %s",
                    motif.motif_id,
                    motif.kind,
                    e,
                )
        nodes_after = self.store.count_nodes()
        return (nodes_after - nodes_before, edges_added)

    # ─── write-boundary validation ────────────────────────────────────

    def _accept(self, kind: str, value: str) -> bool:
        """Validate a prospective node value before it is written.

        Returns ``True`` when the value may be written. On rejection
        (agent-internal noise — see :class:`NodeKindValidator`) nothing
        is written, the rejection is logged at WARNING, and
        :attr:`rejections` is incremented. A ``False`` result tells the
        caller to skip the rest of the motif so no partial node/edge
        state is materialised.
        """
        verdict = self.validator.check(kind, value)
        if verdict.valid:
            return True
        self.rejections += 1
        _log.warning(
            "motif import: rejected %s node %r — %s",
            kind,
            value,
            verdict.reason,
        )
        return False

    # ─── kind dispatch ────────────────────────────────────────────────

    def _import_one(self, motif: Motif) -> int:
        """Dispatch on motif kind. Returns the number of edges added."""
        if motif.kind == "temporal":
            return self._import_temporal(motif)
        if motif.kind == "transition":
            return self._import_transition(motif)
        if motif.kind == "implicit_goal":
            return self._import_implicit_goal(motif)
        _log.debug("unknown motif kind %r — skipping", motif.kind)
        return 0

    def _import_temporal(self, motif: Motif) -> int:
        """Temporal motif → attribute + preference + ``asserts`` edge.

        Payload shape::

            {"label": str, "hour": int, "day_of_week": int, "count": int}
        """
        payload: dict[str, Any] = dict(motif.payload)
        label = str(payload.get("label", ""))
        hour = int(payload.get("hour", 0))
        dow = int(payload.get("day_of_week", 0))
        if not label:
            return 0
        attr_value = f"uses {label}"
        pref_value = f"prefers {_weekday(dow)} {hour:02}:00 for {label}"
        # Validate both node values before writing either — a rejected
        # value means the motif is agent-internal noise; skip the whole
        # motif rather than materialise a partial attribute/preference.
        if not self._accept("attribute", attr_value):
            return 0
        if not self._accept("preference", pref_value):
            return 0
        attr = self.store.upsert_node(
            kind="attribute",
            value=attr_value,
            confidence=motif.confidence,
        )
        pref = self.store.upsert_node(
            kind="preference",
            value=pref_value,
            confidence=motif.confidence,
        )
        edge = Edge(
            edge_id=_deterministic_edge_id(
                "asserts", attr.node_id, pref.node_id, "motif_importer"
            ),
            kind="asserts",
            from_node=attr.node_id,
            to_node=pref.node_id,
            salience=min(1.0, motif.support / 20.0),
            confidence=motif.confidence,
            source_reliability=0.6,
            evidence={"motif_id": motif.motif_id, "kind": "temporal"},
            source="motif_importer",
        )
        self.store.insert_edge(edge)
        return 1

    def _import_transition(self, motif: Motif) -> int:
        """Transition motif → two attributes + ``derives_from`` edge.

        Payload shape::

            {"prev": str, "curr": str, "count": int, "probability": float}

        The edge orientation models "current step follows previous" —
        i.e. ``curr derives_from prev``. Salience scales with support.
        """
        payload: dict[str, Any] = dict(motif.payload)
        prev = str(payload.get("prev", ""))
        curr = str(payload.get("curr", ""))
        if not prev or not curr:
            return 0
        prev_value = f"runs {prev}"
        curr_value = f"runs {curr}"
        # Skip the whole motif if either endpoint is agent-internal noise.
        if not self._accept("attribute", prev_value):
            return 0
        if not self._accept("attribute", curr_value):
            return 0
        prev_node = self.store.upsert_node(
            kind="attribute",
            value=prev_value,
            confidence=motif.confidence,
        )
        curr_node = self.store.upsert_node(
            kind="attribute",
            value=curr_value,
            confidence=motif.confidence,
        )
        edge = Edge(
            edge_id=_deterministic_edge_id(
                "derives_from",
                curr_node.node_id,
                prev_node.node_id,
                "motif_importer",
            ),
            kind="derives_from",
            from_node=curr_node.node_id,
            to_node=prev_node.node_id,
            salience=min(1.0, motif.support / 10.0),
            confidence=motif.confidence,
            source_reliability=0.6,
            evidence={
                "motif_id": motif.motif_id,
                "kind": "transition",
                "probability": payload.get("probability"),
            },
            source="motif_importer",
        )
        self.store.insert_edge(edge)
        return 1

    def _import_implicit_goal(self, motif: Motif) -> int:
        """Implicit-goal motif → goal + per-top-tool attributes + edges.

        Payload shape::

            {
                "session_id": str,
                "top_tools": list[str],
                "n_events": int,
                "n_distinct_tools": int,
            }
        """
        payload: dict[str, Any] = dict(motif.payload)
        top_tools = payload.get("top_tools") or []
        if not top_tools:
            return 0
        n_distinct = int(payload.get("n_distinct_tools", len(top_tools)))
        goal_value = f"session goal: {top_tools[0]}-led ({n_distinct} tools)"
        if not self._accept("goal", goal_value):
            return 0
        goal = self.store.upsert_node(
            kind="goal",
            value=goal_value,
            confidence=motif.confidence,
        )
        edges_added = 0
        # Only the first three top tools contribute per-tool attribute
        # edges — higher ranks are too noisy to be useful.
        for i, tool in enumerate(top_tools[:3]):
            attr_value = f"uses {tool}"
            # A single noise tool is skipped on its own — the goal node
            # and the other tools still stand.
            if not self._accept("attribute", attr_value):
                continue
            attr = self.store.upsert_node(
                kind="attribute",
                value=attr_value,
                confidence=motif.confidence,
            )
            edge = Edge(
                edge_id=_deterministic_edge_id(
                    "derives_from",
                    goal.node_id,
                    attr.node_id,
                    "motif_importer",
                ),
                kind="derives_from",
                from_node=goal.node_id,
                to_node=attr.node_id,
                salience=0.4,
                confidence=motif.confidence,
                source_reliability=0.5,
                evidence={
                    "motif_id": motif.motif_id,
                    "kind": "implicit_goal",
                    "rank": i,
                },
                source="motif_importer",
            )
            self.store.insert_edge(edge)
            edges_added += 1
        return edges_added


__all__ = ["MotifImporter"]
