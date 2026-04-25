"""
Temporal decay + drift primitives — the public vocabulary for Phase 3.D (F5 layer).

This module is the SDK contract for the decay / drift stack shipped in
:mod:`opencomputer.user_model.decay`, :mod:`opencomputer.user_model.drift`,
and :mod:`opencomputer.user_model.drift_store`. Plugins and downstream
consumers read ``DriftReport`` values without reaching into
``opencomputer/*`` internals.

Model
-----

* :class:`DecayConfig` — per-edge-kind half-life knobs for the
  exponential recency-weight formula ``weight = 0.5 ** (age / half_life)``.
  The default half-lives reflect the intended semantics of each edge kind:
  semantic relationships (``supersedes``) persist longest; contradictions
  (``contradicts``) fade moderately; re-asserted facts (``asserts``) hold
  a middle default; provenance edges (``derives_from``) fade sooner.
* :class:`DriftConfig` — sliding-window + KL-divergence knobs for drift
  detection over the motif distribution.
* :class:`DriftReport` — snapshot of a single drift-detection run. Carries
  per-kind KL terms, a ``top_changes`` ranking for UI, and a ``significant``
  flag callers can threshold on.

Stability contract
------------------

This module is part of the public SDK. Once the field set is shipped,
renaming / removing / re-typing a field is a **breaking change**. Adding
optional fields with safe defaults is fine.

Privacy posture
---------------

Mirrors the Phase 3.B / 3.C stance — reports carry counts, coarse
``kind/label`` buckets, and KL terms, never raw user content.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class DecayConfig:
    """Per-edge-kind half-life knobs for the exponential recency-weight decay.

    The recency-weight of an edge at age ``d`` days is

    .. math::

        w(d) = \\max(\\text{min\\_recency\\_weight}, 0.5^{d / H_k})

    where :math:`H_k` is the half-life for the edge kind :math:`k`. Edges
    whose weights hit the floor stay on the graph — the floor makes the
    :class:`ContextRanker` formula degrade gracefully for very old edges
    without dropping them entirely.

    Attributes
    ----------
    asserts_half_life_days:
        Half-life for ``asserts`` edges — the most common kind. Re-asserted
        facts age slowly so durable preferences persist across weeks.
        Default 30 days.
    contradicts_half_life_days:
        Half-life for ``contradicts`` edges. A contradiction is newer
        evidence that an older assertion no longer holds; it ages
        moderately fast so stale conflicts don't dominate rankings.
        Default 14 days.
    supersedes_half_life_days:
        Half-life for ``supersedes`` edges. These capture semantic
        relationships (preference revisions) and should persist. Default
        60 days.
    derives_from_half_life_days:
        Half-life for ``derives_from`` edges. Provenance links fade
        faster than the facts they derive. Default 21 days.
    min_recency_weight:
        Floor applied after the decay formula. Prevents very old edges
        from reaching exactly zero weight (which would silently drop
        them from :class:`ContextRanker` results). Default 0.05.
    default_half_life_days:
        Fallback half-life for unknown edge kinds — a future edge kind
        that lands before this config is updated still gets a sensible
        decay. Default 30 days.
    """

    asserts_half_life_days: float = 30.0
    contradicts_half_life_days: float = 14.0
    supersedes_half_life_days: float = 60.0
    derives_from_half_life_days: float = 21.0
    min_recency_weight: float = 0.05
    default_half_life_days: float = 30.0


@dataclass(frozen=True, slots=True)
class DriftConfig:
    """Knobs for the drift-detection pass.

    Drift is measured as the symmetrized KL divergence between the
    recent-window distribution of motif ``kind/label`` buckets and the
    lifetime distribution. Labels too sparse in the lifetime history
    are skipped because their KL terms are dominated by the smoothing
    epsilon and amount to noise.

    Attributes
    ----------
    recent_window_days:
        Size of the sliding recent window in days. Default 7.0 (one
        week — matches the user-facing "the last week" narrative).
    min_lifetime_count:
        Skip any label with fewer than this many lifetime occurrences.
        Keeps the KL score focused on labels with enough history to be
        meaningful. Default 5.
    kl_significance_threshold:
        Total symmetrized KL above this value sets
        :attr:`DriftReport.significant` to ``True``. Default 0.5 — a
        conservative cut-off that fires on plausible regime shifts
        without tripping on normal week-to-week variation.
    top_changes_count:
        Number of largest per-label deltas included in
        :attr:`DriftReport.top_changes`. Default 5.
    smoothing_epsilon:
        Laplace-smoothing epsilon added to every raw count before
        normalizing to probabilities. Default 0.01 — large enough to
        avoid ``log(0)`` blow-ups without materially perturbing
        well-supported labels.
    """

    recent_window_days: float = 7.0
    min_lifetime_count: int = 5
    kl_significance_threshold: float = 0.5
    top_changes_count: int = 5
    smoothing_epsilon: float = 0.01


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Snapshot of one drift-detection run.

    Attributes
    ----------
    report_id:
        Stable UUID4 per report, auto-generated on construction. Primary
        key in :class:`opencomputer.user_model.drift_store.DriftStore`.
    created_at:
        Unix epoch seconds at which the detection ran.
    window_seconds:
        The recent-window size in seconds, captured from
        :attr:`DriftConfig.recent_window_days` at detection time.
    total_kl_divergence:
        Sum of symmetrized KL terms across every scored label.
    per_kind_drift:
        KL contribution grouped by motif kind prefix (``"temporal"``,
        ``"transition"``, ``"implicit_goal"``, etc.). Lets a consumer
        localize drift to a specific extractor.
    recent_distribution:
        Raw counts keyed by ``"{kind}/{label}"`` for the recent window.
    lifetime_distribution:
        Raw counts keyed by ``"{kind}/{label}"`` across all motifs.
    top_changes:
        Tuple of the ``top_changes_count`` biggest label deltas, each a
        mapping with keys ``label``, ``recent_count``, ``lifetime_count``,
        and ``delta_ratio`` (``recent_p / lifetime_p``, with smoothing).
        Ranked by the absolute probability delta descending.
    significant:
        ``True`` iff ``total_kl_divergence`` exceeds
        :attr:`DriftConfig.kl_significance_threshold`.
    """

    window_seconds: float = 0.0
    total_kl_divergence: float = 0.0
    per_kind_drift: Mapping[str, float] = field(default_factory=dict)
    recent_distribution: Mapping[str, int] = field(default_factory=dict)
    lifetime_distribution: Mapping[str, int] = field(default_factory=dict)
    top_changes: tuple[Mapping[str, Any], ...] = ()
    significant: bool = False
    report_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)


__all__ = [
    "DecayConfig",
    "DriftConfig",
    "DriftReport",
]
