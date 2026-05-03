"""v0.5 Task A: daily Telegram digest of pending_approval policy_changes.

Replaces v0's per-event ping in Phase A with a once-per-day batched DM.
Reduces noise on busy days (daily_change_budget = 3 means up to 3 pings
per day in v0; this collapses them into one).

Behavior:
  - Once per local-day, at the configured ``digest_hour_local`` (default
    9am), collects every pending_approval row, formats a single message
    with /policy-approve hints inline, sends ONE Telegram.
  - If digest_mode is off (``feature_flags.policy_engine.digest_mode =
    false``), this cron is a no-op and the per-event notifier handles
    pings.
  - Idempotent — re-running the same day is a no-op (tracked via
    ``policy_engine.digest_last_run_day`` flag).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

logger = logging.getLogger(__name__)

_LAST_RUN_KEY = "policy_engine.digest_last_run_day"


def run_policy_digest(
    *,
    db,
    flags,
    send_fn: Callable[[str], Awaitable[None]] | None = None,
) -> int:
    """Returns count of recommendations included in today's digest, or
    0 if not yet time to fire today / digest_mode disabled / nothing
    pending."""
    if not flags.read("policy_engine.digest_mode", True):
        return 0

    today = datetime.now().strftime("%Y-%m-%d")
    last_day = flags.read(_LAST_RUN_KEY, "")
    if last_day == today:
        return 0

    target_hour = int(flags.read("policy_engine.digest_hour_local", 9))
    if datetime.now().hour < target_hour:
        return 0

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT id, knob_kind, target_id, reason, "
            "recommendation_engine_version FROM policy_changes "
            "WHERE status = 'pending_approval' "
            "ORDER BY ts_drafted DESC"
        ).fetchall()

    if not rows:
        flags.write(_LAST_RUN_KEY, today)  # mark today as run even if empty
        return 0

    lines = [f"🤖 Policy engine digest — {len(rows)} pending recommendations:\n"]
    for r in rows:
        cid = r["id"]
        lines.append(
            f"  • {cid[:8]}  {r['knob_kind']} → {r['target_id']}\n"
            f"    engine:  {r['recommendation_engine_version']}\n"
            f"    reason:  {r['reason']}\n"
            f"    approve: /policy-approve {cid[:8]}\n"
        )
    lines.append(
        "Drafts auto-discard after 7 days. Use /policy-changes for full audit."
    )
    digest_text = "\n".join(lines)

    if send_fn is not None:
        _spawn_send(send_fn, digest_text)

    flags.write(_LAST_RUN_KEY, today)
    return len(rows)


def _spawn_send(send_fn, text: str) -> None:
    """Schedule the async send. Same pattern as policy_notifier — works
    inside an event loop or via asyncio.run when not."""
    import asyncio

    async def _safe():
        try:
            await send_fn(text)
        except Exception as e:  # noqa: BLE001
            logger.warning("policy_digest send failed: %s", e)

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_safe())
    except RuntimeError:
        try:
            asyncio.run(_safe())
        except Exception as e:  # noqa: BLE001
            logger.warning("policy_digest asyncio.run failed: %s", e)
