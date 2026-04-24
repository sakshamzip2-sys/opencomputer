"""ProgressivePromoter — offers Tier 2→1 promotion after N clean runs."""
import sqlite3
import tempfile
from pathlib import Path

from opencomputer.agent.consent.progressive_promoter import ProgressivePromoter
from opencomputer.agent.state import apply_migrations


def _c():
    c = sqlite3.connect(
        Path(tempfile.mkdtemp()) / "t.db", check_same_thread=False,
    )
    apply_migrations(c)
    return c


def test_increments_counter_on_clean_run():
    c = _c()
    p = ProgressivePromoter(c, threshold_n=10)
    p.record_clean_run("x", None)
    p.record_clean_run("x", None)
    assert p.counter("x", None) == 2


def test_dirty_run_resets_counter():
    c = _c()
    p = ProgressivePromoter(c, threshold_n=10)
    p.record_clean_run("x", None)
    p.record_clean_run("x", None)
    p.record_dirty_run("x", None)
    assert p.counter("x", None) == 0


def test_offers_promotion_at_threshold():
    c = _c()
    p = ProgressivePromoter(c, threshold_n=10)
    for _ in range(9):
        p.record_clean_run("x", None)
    assert p.should_offer_promotion("x", None) is False
    p.record_clean_run("x", None)  # 10th
    assert p.should_offer_promotion("x", None) is True


def test_per_scope_isolation():
    c = _c()
    p = ProgressivePromoter(c, threshold_n=10)
    p.record_clean_run("x", "/a")
    p.record_clean_run("x", "/b")
    assert p.counter("x", "/a") == 1
    assert p.counter("x", "/b") == 1


def test_counter_zero_when_never_recorded():
    c = _c()
    p = ProgressivePromoter(c, threshold_n=10)
    assert p.counter("never-seen", None) == 0
