"""
Temporal-decay engine for user-model edges (Phase 3.D, F5 layer).

The engine walks the edges table and ages each edge's ``recency_weight``
using an exponential half-life formula keyed on :class:`plugin_sdk.user_model.EdgeKind`.
Per-kind half-lives come from :class:`plugin_sdk.decay.DecayConfig`; the
formula is

.. math::

    w(d) = \\max(\\text{min\\_recency\\_weight}, 0.5^{d / H_k})

where ``d`` is the age in days since ``edge.created_at`` and ``H_k`` is
the half-life for the edge kind. The floor makes older edges degrade
gracefully in the :class:`ContextRanker` score without being silently
excluded.

Phase 3.D MVP scope
-------------------

* Per-edge :attr:`plugin_sdk.user_model.Edge.decay_rate` is **not** honored
  in this pass — it will be wired in later if dogfood justifies a second
  knob. Today every edge of the same kind decays at the same rate. This
  keeps the formula auditable and the config surface minimal.
* Heavy work is explicitly *not* parallelised. The worst-case edge count
  for a personal profile is tens of thousands; a straight loop paginated
  by ``batch_size`` is fine and avoids threading hazards in the writer.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from opencomputer.user_model.store import UserModelStore
from plugin_sdk.decay import DecayConfig

if TYPE_CHECKING:
    from plugin_sdk.user_model import Edge

_log = logging.getLogger("opencomputer.user_model.decay")

_SECONDS_PER_DAY = 86400.0


class DecayEngine:
    """Apply per-edge-kind exponential decay to ``Edge.recency_weight``.

    Parameters
    ----------
    store:
        Destination graph store. ``None`` uses the default path
        (``<profile_home>/user_model/graph.sqlite``).
    config:
        Decay knobs. ``None`` uses :class:`DecayConfig` defaults.
    """

    def __init__(
        self,
        store: UserModelStore | None = None,
        config: DecayConfig | None = None,
    ) -> None:
        self.store = store if store is not None else UserModelStore()
        self.config = config if config is not None else DecayConfig()

    # ─── per-kind half-life lookup ─────────────────────────────────────

    def half_life_for(self, edge_kind: str) -> float:
        """Return the half-life in days for ``edge_kind``.

        Unknown kinds fall back to :attr:`DecayConfig.default_half_life_days`.
        This keeps the engine forward-compatible with any future edge kind
        (e.g. an LLM-proposed "supports" or "refutes") that lands before
        the config is updated.
        """
        cfg = self.config
        mapping = {
            "asserts": cfg.asserts_half_life_days,
            "contradicts": cfg.contradicts_half_life_days,
            "supersedes": cfg.supersedes_half_life_days,
            "derives_from": cfg.derives_from_half_life_days,
        }
        return float(mapping.get(edge_kind, cfg.default_half_life_days))

    # ─── per-edge weight formula ──────────────────────────────────────

    def compute_recency_weight(
        self,
        edge: Edge,
        *,
        now: float | None = None,
    ) -> float:
        """Return the decayed recency weight for ``edge``.

        Parameters
        ----------
        edge:
            Any :class:`plugin_sdk.user_model.Edge`. Only ``created_at``
            and ``kind`` are consulted.
        now:
            Override the reference time (unix epoch seconds) — useful for
            deterministic tests. ``None`` means "use ``time.time()``".

        Returns
        -------
        float
            Decayed weight in ``[min_recency_weight, 1.0]``.
        """
        reference = time.time() if now is None else float(now)
        age_seconds = max(0.0, reference - float(edge.created_at))
        age_days = age_seconds / _SECONDS_PER_DAY
        half_life = self.half_life_for(edge.kind)
        # Defensive guard: a zero / negative half-life would mean
        # "instant decay" — honour the floor rather than divide.
        weight = (
            self.config.min_recency_weight
            if half_life <= 0
            else 0.5 ** (age_days / half_life)
        )
        return max(float(self.config.min_recency_weight), float(weight))

    # ─── full-graph apply ─────────────────────────────────────────────

    def apply_decay(
        self,
        *,
        now: float | None = None,
        batch_size: int = 1000,
    ) -> int:
        """Walk every edge in the store and persist a fresh recency weight.

        Loads all edges once (via :meth:`UserModelStore.list_edges` with a
        generous upper bound) and updates them one by one. ``batch_size``
        controls how many updates are attempted per store round-trip; at
        the SQLite scale expected for a personal profile (tens of
        thousands of edges) the simpler single-pass model is more robust
        than offset pagination, which would skip/duplicate rows as the
        ``ORDER BY created_at DESC`` window shifts under concurrent
        writes. Returns the total number of edges whose weight was
        written.
        """
        reference = time.time() if now is None else float(now)
        # Upper bound chosen generously so a big personal-profile graph
        # still fits in one read. If this is ever too small, the
        # scheduler will still make forward progress on the next run —
        # we just process the ``limit`` newest edges each time.
        edges = self.store.list_edges(limit=max(batch_size * 1000, 1_000_000))
        updated = 0
        for edge in edges:
            weight = self.compute_recency_weight(edge, now=reference)
            try:
                self.store.update_edge_recency_weight(edge.edge_id, weight)
            except Exception as exc:  # noqa: BLE001 — decay must not fail the app
                _log.warning(
                    "decay: edge_id=%s update failed (non-fatal): %s",
                    edge.edge_id,
                    exc,
                )
                continue
            updated += 1
        return updated

    # ─── per-node convenience ─────────────────────────────────────────

    def apply_decay_for_node(
        self,
        node_id: str,
        *,
        now: float | None = None,
    ) -> int:
        """Apply decay only to edges incident to ``node_id``.

        Walks both outgoing (``from_node``) and incoming (``to_node``)
        edges. Useful after a targeted motif import that only touched
        one subgraph — avoids a full-graph scan. Returns the number
        of edges updated.
        """
        reference = time.time() if now is None else float(now)
        outgoing = self.store.list_edges(from_node=node_id, limit=10_000)
        incoming = self.store.list_edges(to_node=node_id, limit=10_000)
        # Dedup — an edge that starts and ends on the same node would
        # show up in both queries; avoid double work.
        seen: set[str] = set()
        edges: list[Edge] = []
        for edge in (*outgoing, *incoming):
            if edge.edge_id in seen:
                continue
            seen.add(edge.edge_id)
            edges.append(edge)
        updated = 0
        for edge in edges:
            weight = self.compute_recency_weight(edge, now=reference)
            try:
                self.store.update_edge_recency_weight(edge.edge_id, weight)
            except Exception as exc:  # noqa: BLE001 — decay must not fail the app
                _log.warning(
                    "decay: edge_id=%s update failed (non-fatal): %s",
                    edge.edge_id,
                    exc,
                )
                continue
            updated += 1
        return updated


__all__ = ["DecayEngine"]
