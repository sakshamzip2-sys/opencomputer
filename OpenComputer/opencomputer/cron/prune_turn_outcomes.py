"""v0.5 Task D: data retention cron — prune turn_outcomes older than N days.

Default: 90 days. Configurable via ``feature_flags.json:
data_retention.turn_outcomes_days``. ``recall_citations`` cascades via
FK so we don't have to prune them separately.

Retention is conservative for v0.5 — Phase 1's LLM-judge has 30-day
training-data utility per spec; 90d covers that with margin. Bumping
the default later is one config edit.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def run_prune_turn_outcomes(*, db, flags) -> int:
    """Delete ``turn_outcomes`` rows older than the retention window.

    Returns count of rows deleted (0 if nothing to prune).
    """
    days = int(flags.read("data_retention.turn_outcomes_days", 90))
    cutoff = time.time() - days * 86400
    with db._connect() as conn:
        cur = conn.execute(
            "DELETE FROM turn_outcomes WHERE created_at < ?",
            (cutoff,),
        )
        n = cur.rowcount
    if n:
        logger.info(
            "prune_turn_outcomes: deleted %d rows older than %d days", n, days,
        )
    return n
