"""Cost guard implementation — per-provider daily + monthly budget tracking.

Storage shape (JSON at ``<profile_home>/cost_guard.json``):

```json
{
  "version": 1,
  "limits": {
    "openai":    {"daily": 5.0, "monthly": 50.0},
    "anthropic": {"daily": 10.0, "monthly": 100.0}
  },
  "usage": {
    "openai": {
      "2026-04-25": [{"ts": 1700000000.0, "operation": "tts", "cost": 0.015}, ...],
      "2026-04-24": [...]
    }
  }
}
```

Daily entries past 90 days are auto-pruned on each save to keep the file
bounded. Monthly totals are computed by summing the relevant day entries.

Atomic writes via tmp + os.replace (cron-jobs / webhook-tokens pattern).
File mode 0600 since usage data implicitly reveals which APIs are in use.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opencomputer.agent.config import _home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """Result of :meth:`CostGuard.check_budget`.

    Attributes:
        allowed: True when the projected call fits within remaining budget.
        reason: Human-readable explanation. Always present on allowed=False;
            informational on allowed=True (e.g. "82% of daily used").
        daily_used: Current day's spend in USD.
        daily_limit: Daily cap or ``None`` if unlimited.
        monthly_used: Current month's spend in USD.
        monthly_limit: Monthly cap or ``None`` if unlimited.
    """

    allowed: bool
    reason: str
    daily_used: float
    daily_limit: float | None
    monthly_used: float
    monthly_limit: float | None


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Per-provider usage summary returned by :meth:`CostGuard.current_usage`."""

    provider: str
    daily_used: float
    monthly_used: float
    daily_limit: float | None
    monthly_limit: float | None
    operations_today: dict[str, float] = field(default_factory=dict)
    """Per-operation breakdown for the current day (e.g. ``{"tts": 0.045}``)."""


class BudgetExceeded(RuntimeError):  # noqa: N818 — public name without "Error" suffix preserves API readability
    """Raised when a check_budget call would exceed the configured limit.

    Callers may catch this to fall back gracefully (e.g. text-only when
    voice budget is exhausted), but should NOT swallow it silently — the
    intent of the guard is to block runaway spend, not paper over it.
    """


# ---------------------------------------------------------------------------
# CostGuard — main API
# ---------------------------------------------------------------------------


_RETENTION_DAYS = 90
"""Daily entries older than this are pruned on save."""

_DEFAULT_FILE_VERSION = 1


class CostGuard:
    """Per-provider budget tracker.

    Construct with an explicit storage path for tests; production callers
    use :func:`get_default_guard` which resolves to ``<profile_home>/cost_guard.json``.

    Thread-safe via an in-process lock.
    """

    def __init__(self, *, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._lock = threading.Lock()
        # Cached state — reloaded under the lock on each operation that needs
        # the freshest read; not held across sleeps.
        self._state: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_limit(
        self,
        provider: str,
        *,
        daily: float | None = None,
        monthly: float | None = None,
    ) -> None:
        """Set / clear daily and monthly USD limits for one provider.

        ``None`` clears the corresponding limit (unlimited). Pass a float
        (e.g. ``5.0``) to set a cap of $5.00.
        """
        provider = _normalise_provider(provider)
        with self._lock:
            state = self._load()
            limits = state["limits"].setdefault(provider, {})
            if daily is not None:
                limits["daily"] = float(daily)
            elif "daily" in limits:
                del limits["daily"]
            if monthly is not None:
                limits["monthly"] = float(monthly)
            elif "monthly" in limits:
                del limits["monthly"]
            # Drop the provider entry entirely if both limits cleared.
            if not limits:
                state["limits"].pop(provider, None)
            self._save(state)

    def record_usage(
        self,
        provider: str,
        *,
        cost_usd: float,
        operation: str = "",
    ) -> None:
        """Append a cost entry to the current day's bucket for ``provider``.

        ``operation`` is a free-form short label (e.g. ``"tts"``,
        ``"completion"``, ``"whisper"``) that surfaces in
        :attr:`ProviderUsage.operations_today`.
        """
        if cost_usd < 0:
            raise ValueError(f"cost_usd must be >= 0, got {cost_usd!r}")
        provider = _normalise_provider(provider)
        with self._lock:
            state = self._load()
            usage = state["usage"].setdefault(provider, {})
            today = _today_key()
            day_entries = usage.setdefault(today, [])
            day_entries.append(
                {
                    "ts": time.time(),
                    "operation": str(operation or "")[:64],
                    "cost": float(cost_usd),
                }
            )
            self._prune_old_days(state)
            self._save(state)

    def check_budget(
        self,
        provider: str,
        *,
        projected_cost_usd: float = 0.0,
    ) -> BudgetDecision:
        """Decide whether a call is in budget. Pure read — does not record.

        Use :meth:`record_usage` afterward when the call actually completes.

        ``projected_cost_usd`` is added to the current daily/monthly totals
        for the comparison so callers can pre-flight expensive operations.
        """
        if projected_cost_usd < 0:
            raise ValueError(f"projected_cost_usd must be >= 0, got {projected_cost_usd!r}")
        provider = _normalise_provider(provider)
        with self._lock:
            state = self._load()
            limits = state["limits"].get(provider, {})
            daily_used = self._daily_total(state, provider)
            monthly_used = self._monthly_total(state, provider)
            daily_limit = limits.get("daily")
            monthly_limit = limits.get("monthly")

        projected_daily = daily_used + projected_cost_usd
        projected_monthly = monthly_used + projected_cost_usd

        if daily_limit is not None and projected_daily > daily_limit:
            return BudgetDecision(
                allowed=False,
                reason=(
                    f"daily limit exceeded for {provider}: "
                    f"{projected_daily:.4f} USD > {daily_limit:.4f} USD"
                ),
                daily_used=daily_used,
                daily_limit=daily_limit,
                monthly_used=monthly_used,
                monthly_limit=monthly_limit,
            )

        if monthly_limit is not None and projected_monthly > monthly_limit:
            return BudgetDecision(
                allowed=False,
                reason=(
                    f"monthly limit exceeded for {provider}: "
                    f"{projected_monthly:.4f} USD > {monthly_limit:.4f} USD"
                ),
                daily_used=daily_used,
                daily_limit=daily_limit,
                monthly_used=monthly_used,
                monthly_limit=monthly_limit,
            )

        # Allowed; surface utilisation as informational context.
        reason_parts = []
        if daily_limit is not None and daily_limit > 0:
            reason_parts.append(f"{projected_daily / daily_limit * 100:.0f}% of daily")
        if monthly_limit is not None and monthly_limit > 0:
            reason_parts.append(f"{projected_monthly / monthly_limit * 100:.0f}% of monthly")
        reason = (
            "within budget — " + ", ".join(reason_parts)
            if reason_parts
            else "within budget (no limits set)"
        )
        return BudgetDecision(
            allowed=True,
            reason=reason,
            daily_used=daily_used,
            daily_limit=daily_limit,
            monthly_used=monthly_used,
            monthly_limit=monthly_limit,
        )

    def current_usage(self, provider: str | None = None) -> list[ProviderUsage]:
        """Return current usage for one or all providers.

        ``provider=None`` returns one entry per known provider (one with
        recorded usage or a configured limit). Otherwise returns a single
        entry for the named provider (still as a list for uniform handling).
        """
        with self._lock:
            state = self._load()
            providers: list[str] = (
                [provider] if provider else _all_providers(state)
            )
            return [self._summarise(state, p) for p in providers]

    def reset(self, provider: str | None = None) -> None:
        """Clear recorded usage. Limits are NOT reset.

        ``provider=None`` clears all providers' usage. Useful for testing
        and for the ``opencomputer cost reset`` CLI.
        """
        with self._lock:
            state = self._load()
            if provider is None:
                state["usage"] = {}
            else:
                state["usage"].pop(_normalise_provider(provider), None)
            self._save(state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _summarise(self, state: dict[str, Any], provider: str) -> ProviderUsage:
        limits = state["limits"].get(provider, {})
        ops_today: dict[str, float] = defaultdict(float)
        for entry in state["usage"].get(provider, {}).get(_today_key(), []):
            ops_today[entry.get("operation") or "(unlabelled)"] += float(entry["cost"])
        return ProviderUsage(
            provider=provider,
            daily_used=self._daily_total(state, provider),
            monthly_used=self._monthly_total(state, provider),
            daily_limit=limits.get("daily"),
            monthly_limit=limits.get("monthly"),
            operations_today=dict(ops_today),
        )

    @staticmethod
    def _daily_total(state: dict[str, Any], provider: str) -> float:
        day = state["usage"].get(provider, {}).get(_today_key(), [])
        return float(sum(e["cost"] for e in day))

    @staticmethod
    def _monthly_total(state: dict[str, Any], provider: str) -> float:
        prefix = _today_key()[:7]  # "YYYY-MM"
        usage = state["usage"].get(provider, {})
        total = 0.0
        for day, entries in usage.items():
            if day.startswith(prefix):
                total += sum(e["cost"] for e in entries)
        return total

    @staticmethod
    def _prune_old_days(state: dict[str, Any]) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=_RETENTION_DAYS)).strftime(
            "%Y-%m-%d"
        )
        for provider, usage in state["usage"].items():
            stale = [day for day in usage if day < cutoff]
            for day in stale:
                del usage[day]

    def _load(self) -> dict[str, Any]:
        if self._storage_path.exists():
            try:
                with open(self._storage_path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except json.JSONDecodeError as exc:
                logger.error("cost_guard.json corrupted: %s — starting fresh", exc)
                data = {}
        else:
            data = {}
        # Normalise shape — defaults for any missing keys.
        return {
            "version": data.get("version", _DEFAULT_FILE_VERSION),
            "limits": dict(data.get("limits") or {}),
            "usage": dict(data.get("usage") or {}),
        }

    def _save(self, state: dict[str, Any]) -> None:
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._storage_path.parent), suffix=".tmp", prefix=".cost_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._storage_path)
            try:
                os.chmod(self._storage_path, 0o600)
            except (OSError, NotImplementedError):
                pass
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


_default_guard: CostGuard | None = None
_default_lock = threading.Lock()


def get_default_guard() -> CostGuard:
    """Return the process-wide default :class:`CostGuard` rooted at the active profile.

    Lazy-constructed on first call; thread-safe.
    """
    global _default_guard
    with _default_lock:
        if _default_guard is None:
            _default_guard = CostGuard(storage_path=_home() / "cost_guard.json")
        return _default_guard


def _reset_default_guard_for_tests() -> None:
    """Clear the cached default guard. Tests use this; production code does not."""
    global _default_guard
    with _default_lock:
        _default_guard = None


def _normalise_provider(provider: str) -> str:
    """Lower-case + strip — provider names are case-insensitive identifiers."""
    if not provider or not provider.strip():
        raise ValueError("provider must be a non-empty string")
    return provider.strip().lower()


def _today_key() -> str:
    """UTC date in ``YYYY-MM-DD`` form for daily-bucket keys."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _all_providers(state: dict[str, Any]) -> list[str]:
    return sorted(set(state["limits"].keys()) | set(state["usage"].keys()))


__all__ = [
    "BudgetDecision",
    "BudgetExceeded",
    "CostGuard",
    "ProviderUsage",
    "get_default_guard",
]
