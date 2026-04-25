"""
Drift detector over motif distributions (Phase 3.D, F5 layer).

Compares the recent-window distribution of motifs against the lifetime
distribution using a symmetrized KL divergence and emits a
:class:`plugin_sdk.decay.DriftReport` when the total KL clears the
configured threshold.

Label bucketing
---------------

Each motif contributes a coarse label of the form
``"{motif.kind}/{first_token_of_summary}"``. The first token is picked
on whitespace; motifs whose summaries share the same first token (e.g.
``"Read"`` as the first word of a transition-chain summary) collapse
into the same bucket. This keeps the label cardinality manageable on
real traces where each motif summary is a one-liner, and matches the
privacy posture — labels carry structure, not free text.

Smoothing
---------

Raw counts are Laplace-smoothed with ``epsilon = smoothing_epsilon``
before normalising to probabilities. This guarantees every label has a
non-zero probability in both distributions, so the symmetrized KL
terms stay finite even when a label appears only in the recent (or
only in the lifetime) window.
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any

from opencomputer.inference.storage import MotifStore
from plugin_sdk.decay import DriftConfig, DriftReport

if TYPE_CHECKING:
    from opencomputer.user_model.drift_store import DriftStore

_log = logging.getLogger("opencomputer.user_model.drift")

_SECONDS_PER_DAY = 86400.0


def _label_for_summary(kind: str, summary: str) -> str:
    """Return the ``"{kind}/{first_token}"`` bucket for a motif summary.

    Empty / whitespace-only summaries fall back to ``"{kind}/_"``. The
    underscore keeps the delimiter scheme uniform so a consumer can
    split on ``"/"`` safely.
    """
    first = summary.strip().split(None, 1)[0] if summary.strip() else "_"
    return f"{kind}/{first}"


class DriftDetector:
    """Symmetrized-KL drift detector over the motif distribution.

    Parameters
    ----------
    motif_store:
        Source motif store. ``None`` uses the default path
        (``<profile_home>/inference/motifs.sqlite``).
    config:
        Drift knobs. ``None`` uses :class:`DriftConfig` defaults.
    drift_store:
        Optional :class:`DriftStore` — when provided, :meth:`detect`
        persists every report on completion. ``None`` makes
        :meth:`detect` read-only (useful for ``--dry-run`` CLI paths).
    """

    def __init__(
        self,
        motif_store: MotifStore | None = None,
        config: DriftConfig | None = None,
        drift_store: DriftStore | None = None,
    ) -> None:
        self.motif_store = motif_store if motif_store is not None else MotifStore()
        self.config = config if config is not None else DriftConfig()
        self.drift_store = drift_store

    # ─── distribution collection ──────────────────────────────────────

    def collect_distributions(
        self,
        *,
        now: float | None = None,
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Return ``(recent_dist, lifetime_dist)``.

        Both distributions are keyed on
        ``"{motif.kind}/{first_summary_token}"``. The recent window is
        motifs with ``created_at >= now - recent_window_days * 86400``;
        lifetime is every motif. Labels whose lifetime count falls below
        :attr:`DriftConfig.min_lifetime_count` are dropped from BOTH
        dictionaries — they're too sparse to contribute meaningful KL.
        """
        reference = time.time() if now is None else float(now)
        window_seconds = self.config.recent_window_days * _SECONDS_PER_DAY
        since = reference - window_seconds
        # Pull lifetime first so we can decide which labels are dense
        # enough to survive the sparse-label filter.
        lifetime_motifs = self.motif_store.list(limit=1_000_000)
        lifetime: dict[str, int] = {}
        for m in lifetime_motifs:
            label = _label_for_summary(m.kind, m.summary)
            lifetime[label] = lifetime.get(label, 0) + 1
        # Drop labels that fall below the minimum lifetime count.
        dense = {
            label: count
            for label, count in lifetime.items()
            if count >= self.config.min_lifetime_count
        }
        recent: dict[str, int] = {}
        for m in lifetime_motifs:
            if m.created_at < since:
                continue
            label = _label_for_summary(m.kind, m.summary)
            if label not in dense:
                continue
            recent[label] = recent.get(label, 0) + 1
        return recent, dense

    # ─── KL computation ───────────────────────────────────────────────

    def compute_kl(
        self,
        recent: dict[str, int],
        lifetime: dict[str, int],
    ) -> tuple[float, dict[str, float]]:
        """Return ``(total_kl, per_kind_kl)`` over the union of labels.

        Uses Laplace smoothing with :attr:`DriftConfig.smoothing_epsilon`:
        every label gets ``count + epsilon`` before normalising. The
        symmetrized term per label is

        .. math::

            kl(\\text{label}) = p \\log \\frac{p}{q} + q \\log \\frac{q}{p}

        where ``p`` / ``q`` are the smoothed probabilities in the recent
        and lifetime distributions respectively. The per-kind dict keys
        are the motif kind prefix (``"temporal"``, ``"transition"`` …),
        i.e. the substring before the first ``"/"``.
        """
        eps = self.config.smoothing_epsilon
        labels = set(recent) | set(lifetime)
        if not labels:
            return 0.0, {}
        # Smoothed totals — add ``epsilon`` per label to keep the
        # normalizing sums consistent with the per-label smoothing.
        recent_total = sum(recent.get(lbl, 0) + eps for lbl in labels)
        lifetime_total = sum(lifetime.get(lbl, 0) + eps for lbl in labels)
        total_kl = 0.0
        per_kind: dict[str, float] = {}
        for label in labels:
            p = (recent.get(label, 0) + eps) / recent_total
            q = (lifetime.get(label, 0) + eps) / lifetime_total
            term = 0.0
            if p > 0 and q > 0:
                term = p * math.log(p / q) + q * math.log(q / p)
            # Symmetrized KL is nonneg by construction, but float noise
            # on identical distributions can go slightly negative.
            term = max(0.0, term)
            total_kl += term
            kind_prefix = label.split("/", 1)[0] if "/" in label else label
            per_kind[kind_prefix] = per_kind.get(kind_prefix, 0.0) + term
        return total_kl, per_kind

    # ─── top-changes ranking ──────────────────────────────────────────

    def _top_changes(
        self,
        recent: dict[str, int],
        lifetime: dict[str, int],
    ) -> tuple[dict[str, Any], ...]:
        """Return the ``top_changes_count`` labels with the biggest delta.

        Delta is measured as ``|p - q|`` on the smoothed probabilities —
        same quantities as the KL sum, so the ranking matches the KL
        contributors. ``delta_ratio`` is ``p / q`` (or ``0.0`` when ``q``
        is exactly zero after smoothing, which should not happen but we
        guard for float edge cases).
        """
        eps = self.config.smoothing_epsilon
        labels = set(recent) | set(lifetime)
        if not labels:
            return ()
        recent_total = sum(recent.get(lbl, 0) + eps for lbl in labels)
        lifetime_total = sum(lifetime.get(lbl, 0) + eps for lbl in labels)
        scored: list[tuple[float, dict[str, Any]]] = []
        for label in labels:
            rc = recent.get(label, 0)
            lc = lifetime.get(label, 0)
            p = (rc + eps) / recent_total
            q = (lc + eps) / lifetime_total
            delta = abs(p - q)
            ratio = (p / q) if q > 0 else 0.0
            scored.append(
                (
                    delta,
                    {
                        "label": label,
                        "recent_count": rc,
                        "lifetime_count": lc,
                        "delta_ratio": float(ratio),
                    },
                )
            )
        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[: self.config.top_changes_count]
        return tuple(entry for _, entry in top)

    # ─── full pass ────────────────────────────────────────────────────

    def detect(self, *, now: float | None = None) -> DriftReport:
        """Run one full drift pass.

        Collects distributions, computes KL, ranks top changes, builds a
        :class:`DriftReport`, and (if a :class:`DriftStore` is attached)
        persists the report before returning it.
        """
        reference = time.time() if now is None else float(now)
        recent, lifetime = self.collect_distributions(now=reference)
        total_kl, per_kind = self.compute_kl(recent, lifetime)
        top = self._top_changes(recent, lifetime)
        window_seconds = self.config.recent_window_days * _SECONDS_PER_DAY
        significant = total_kl > self.config.kl_significance_threshold
        report = DriftReport(
            created_at=reference,
            window_seconds=window_seconds,
            total_kl_divergence=float(total_kl),
            per_kind_drift=dict(per_kind),
            recent_distribution=dict(recent),
            lifetime_distribution=dict(lifetime),
            top_changes=top,
            significant=significant,
        )
        if self.drift_store is not None:
            try:
                self.drift_store.insert(report)
            except Exception as exc:  # noqa: BLE001 — drift report storage is advisory
                _log.warning(
                    "drift: failed to persist report_id=%s (non-fatal): %s",
                    report.report_id,
                    exc,
                )
        return report


__all__ = ["DriftDetector"]
