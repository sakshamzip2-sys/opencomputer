"""Tests for :class:`opencomputer.user_model.drift.DriftDetector`.

Phase 3.D — symmetrized KL divergence over motif distributions. Tests
pin the behaviour described in the plan:

* sparse-label filtering — lifetime count below threshold → skip
* KL zero when recent == lifetime
* KL positive when distributions diverge
* ``significant`` flag threshold
* ``top_changes`` ordering by probability delta
* auto-persistence via an injected :class:`DriftStore`
"""

from __future__ import annotations

import time
from pathlib import Path

from opencomputer.inference.storage import MotifStore
from opencomputer.user_model.drift import DriftDetector
from opencomputer.user_model.drift_store import DriftStore
from plugin_sdk.decay import DriftConfig
from plugin_sdk.inference import Motif


def _motif_store(tmp_path: Path) -> MotifStore:
    return MotifStore(db_path=tmp_path / "motifs.sqlite")


def _drift_store(tmp_path: Path) -> DriftStore:
    return DriftStore(db_path=tmp_path / "drift.sqlite")


def _seed_motif(
    store: MotifStore,
    *,
    kind: str = "temporal",
    summary: str = "Read high",
    age_days: float = 0.0,
    now: float | None = None,
) -> Motif:
    now_ts = time.time() if now is None else now
    m = Motif(
        kind=kind,  # type: ignore[arg-type]
        summary=summary,
        created_at=now_ts - age_days * 86400.0,
    )
    store.insert(m)
    return m


def test_collect_distributions_skips_sparse_lifetime(tmp_path: Path) -> None:
    """Labels with lifetime count < min_lifetime_count drop from BOTH dists."""
    ms = _motif_store(tmp_path)
    now = time.time()
    # Dense label — 6 lifetime entries, all within the recent window.
    for _ in range(6):
        _seed_motif(ms, kind="temporal", summary="Read busy", age_days=1.0, now=now)
    # Sparse label — only 2 lifetime entries.
    for _ in range(2):
        _seed_motif(ms, kind="temporal", summary="Write rare", age_days=1.0, now=now)

    config = DriftConfig(recent_window_days=7.0, min_lifetime_count=5)
    detector = DriftDetector(motif_store=ms, config=config)
    recent, lifetime = detector.collect_distributions(now=now)
    # Only the dense label survives.
    assert set(lifetime.keys()) == {"temporal/Read"}
    assert lifetime["temporal/Read"] == 6
    assert set(recent.keys()) == {"temporal/Read"}


def test_compute_kl_zero_when_distributions_identical(tmp_path: Path) -> None:
    """Identical recent + lifetime distributions → KL ≈ 0."""
    ms = _motif_store(tmp_path)
    config = DriftConfig(min_lifetime_count=1)
    detector = DriftDetector(motif_store=ms, config=config)
    recent = {"temporal/Read": 10, "transition/Edit": 5}
    lifetime = {"temporal/Read": 10, "transition/Edit": 5}
    total_kl, per_kind = detector.compute_kl(recent, lifetime)
    assert total_kl < 1e-9
    for v in per_kind.values():
        assert v < 1e-9


def test_compute_kl_positive_when_recent_diverges(tmp_path: Path) -> None:
    """A skewed recent distribution produces positive KL."""
    ms = _motif_store(tmp_path)
    config = DriftConfig(min_lifetime_count=1)
    detector = DriftDetector(motif_store=ms, config=config)
    # Lifetime is evenly split; recent is almost entirely one label.
    recent = {"temporal/Read": 100, "transition/Edit": 1}
    lifetime = {"temporal/Read": 50, "transition/Edit": 50}
    total_kl, per_kind = detector.compute_kl(recent, lifetime)
    assert total_kl > 0.0
    # Both kinds should contribute.
    assert set(per_kind.keys()) == {"temporal", "transition"}
    assert all(v >= 0.0 for v in per_kind.values())


def test_detect_returns_significant_when_above_threshold(tmp_path: Path) -> None:
    """`significant` fires when total KL exceeds the threshold."""
    ms = _motif_store(tmp_path)
    now = time.time()
    # Lifetime: mostly ``Read`` motifs, long ago.
    for _ in range(20):
        _seed_motif(ms, kind="temporal", summary="Read high", age_days=60.0, now=now)
    # Recent: mostly ``Edit`` motifs — big distribution shift.
    for _ in range(20):
        _seed_motif(ms, kind="transition", summary="Edit busy", age_days=1.0, now=now)
    config = DriftConfig(
        recent_window_days=7.0,
        min_lifetime_count=5,
        kl_significance_threshold=0.1,
    )
    detector = DriftDetector(motif_store=ms, config=config)
    report = detector.detect(now=now)
    assert report.total_kl_divergence > 0.1
    assert report.significant is True


def test_detect_returns_top_changes_sorted_by_delta(tmp_path: Path) -> None:
    """``top_changes`` is sorted by ``|p - q|`` descending."""
    ms = _motif_store(tmp_path)
    now = time.time()
    # Three dense labels with very different drift magnitudes.
    # Goal: p(recent) vs p(lifetime) differences should order: BigShift >> Mid >> Small.
    # Label BigShift: dominates the lifetime (lots of old data); recent window has
    # almost none of it. Biggest absolute delta.
    for _ in range(100):
        _seed_motif(
            ms, kind="temporal", summary="BigShift go", age_days=60.0, now=now
        )
    for _ in range(1):
        _seed_motif(ms, kind="temporal", summary="BigShift go", age_days=1.0, now=now)
    # Label Mid: moderate drift.
    for _ in range(20):
        _seed_motif(ms, kind="temporal", summary="Mid go", age_days=60.0, now=now)
    for _ in range(10):
        _seed_motif(ms, kind="temporal", summary="Mid go", age_days=1.0, now=now)
    # Label Small: nearly stable.
    for _ in range(5):
        _seed_motif(ms, kind="temporal", summary="Small go", age_days=60.0, now=now)
    for _ in range(1):
        _seed_motif(ms, kind="temporal", summary="Small go", age_days=1.0, now=now)

    config = DriftConfig(
        recent_window_days=7.0,
        min_lifetime_count=5,
        top_changes_count=3,
    )
    detector = DriftDetector(motif_store=ms, config=config)
    report = detector.detect(now=now)
    # Exactly 3 top changes.
    assert len(report.top_changes) == 3
    # The biggest shift (BigShift) should show up first.
    labels_in_order = [c["label"] for c in report.top_changes]
    assert labels_in_order[0] == "temporal/BigShift"


def test_detect_persists_report_when_store_provided(tmp_path: Path) -> None:
    """A :class:`DriftStore` injected into the detector receives the report."""
    ms = _motif_store(tmp_path)
    ds = _drift_store(tmp_path)
    # Seed enough motifs so the report isn't trivially empty.
    now = time.time()
    for _ in range(10):
        _seed_motif(ms, kind="temporal", summary="Read go", age_days=1.0, now=now)
    detector = DriftDetector(
        motif_store=ms,
        config=DriftConfig(min_lifetime_count=1),
        drift_store=ds,
    )
    report = detector.detect(now=now)
    # The persisted record round-trips.
    fetched = ds.get(report.report_id)
    assert fetched is not None
    assert fetched.report_id == report.report_id
    assert abs(fetched.total_kl_divergence - report.total_kl_divergence) < 1e-9
