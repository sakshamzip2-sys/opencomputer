"""Per-platform session-reset policies (Hermes-spec parity).

The dispatcher consults :class:`ResetPolicyChecker` immediately before
the deterministic ``(platform, chat_id) → session_id`` lookup. When the
checker says reset, the dispatcher archives + drops the existing session
row so the next message lands in a fresh session.

Modes:
- ``off``    — never reset (the user opts out of automatic resets)
- ``idle``   — reset when ``(now - last_seen) >= idle_minutes * 60``
- ``daily``  — reset when ``now`` crosses today's ``daily_at_hour`` boundary
              since ``last_seen``
- ``both``   — reset on either condition (the safest default)

Per-platform overrides via ``ResetPolicyConfig.by_platform`` so the user
can set a tighter idle threshold for Slack channels (e.g., 1 hour) than
for personal Telegram (e.g., 24h).

Plan: docs/superpowers/plans/2026-05-08-messaging-gateway-parity.md (T1.3)
Spec: docs/superpowers/specs/2026-05-08-messaging-gateway-parity-design.md (§5.3)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Literal

ResetMode = Literal["off", "daily", "idle", "both"]


@dataclass(frozen=True, slots=True)
class ResetPolicy:
    """One reset policy — applies to a single platform (or as the default)."""

    mode: ResetMode = "both"
    daily_at_hour: int = 4  # 0–23 local time; default 4 a.m.
    idle_minutes: int = 1440  # 24h


@dataclass(frozen=True, slots=True)
class ResetPolicyConfig:
    """Composite of a default policy + per-platform overrides."""

    default: ResetPolicy = field(default_factory=ResetPolicy)
    by_platform: dict[str, ResetPolicy] = field(default_factory=dict)


class ResetPolicyChecker:
    """Cheap pure check used by Dispatch.handle_message before session lookup.

    All time math is fed through ``now_fn`` so tests can pin the wall clock.
    """

    def __init__(
        self,
        cfg: ResetPolicyConfig,
        *,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._cfg = cfg
        self._now_fn = now_fn

    def policy_for(self, platform: str) -> ResetPolicy:
        """Return the per-platform override when present, else the default."""
        return self._cfg.by_platform.get(platform, self._cfg.default)

    def should_reset(
        self, platform: str, chat_id: str, last_seen: float
    ) -> tuple[bool, str]:
        """Decide whether the next message should drop into a fresh session.

        ``chat_id`` is unused today but accepted so future per-chat overrides
        (e.g., always-reset on a special admin channel) can land without
        signature churn.
        """
        del chat_id  # reserved for future per-chat overrides.

        policy = self.policy_for(platform)
        if policy.mode == "off":
            return (False, "off")

        now = self._now_fn()

        if policy.mode in ("idle", "both"):
            if (now - last_seen) >= policy.idle_minutes * 60:
                return (True, f"idle:{policy.idle_minutes}m")

        if policy.mode in ("daily", "both"):
            if self._crossed_daily_boundary(last_seen, now, policy.daily_at_hour):
                return (True, f"daily:{policy.daily_at_hour}")

        return (False, policy.mode)

    @staticmethod
    def _crossed_daily_boundary(
        last_seen: float, now: float, hour: int
    ) -> bool:
        """Return True iff ``now`` is past today's ``hour`` boundary AND
        ``last_seen`` was before it.

        The boundary uses **UTC** so behavior is identical regardless of the
        host's timezone (the daemon may move between hosts; the user's
        Telegram client may not match server-local). Users who want
        boundary semantics aligned to their own clock should set
        ``daily_at_hour`` to their preferred UTC hour offset.
        """
        from datetime import timezone

        last_dt = datetime.fromtimestamp(last_seen, tz=timezone.utc)
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        boundary = now_dt.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now_dt < boundary:
            # Today's boundary is in the future — use yesterday's.
            boundary = boundary - timedelta(days=1)
        return last_dt < boundary <= now_dt


__all__ = [
    "ResetMode",
    "ResetPolicy",
    "ResetPolicyConfig",
    "ResetPolicyChecker",
]
