"""Sandbox cost guard — per-second sandbox-backend spend + per-session caps.

Milestone 2 task T2.8 (Hermes + OpenClaw parity plan, 2026-05-16).

The cloud E2B sandbox backend bills **per running second**; the local
backends (``docker`` / ``linux_bwrap`` / ``macos_sandbox_exec`` / ``ssh``
/ ``none``) are free. After every sandboxed ``Bash`` run the agent loop
records ``duration_seconds × rate(backend)`` against the *session's*
sandbox spend, and refuses a new sandboxed run when the session is
already over its cap.

Why a separate guard from :class:`~opencomputer.cost_guard.guard.CostGuard`:
that guard buckets spend per *provider* per *calendar day / month*. A
sandbox session cap is keyed on the **session id**, not a provider or a
date — a different bucketing entirely. Rather than overload the provider
guard, this module owns the session bucket — and its own file. It writes
``<profile_home>/sandbox_cost_guard.json``, distinct from the provider
guard's ``cost_guard.json``: one writer per file, so the per-process
singleton's in-process lock fully serializes intra-process read-modify-write
and no interleaved write can lose a session's update.

Storage shape (``<profile_home>/sandbox_cost_guard.json``)::

    {
      "sandbox": {
        "rates": {"e2b": 3.25e-05},
        "session_cap_usd": 1.0,
        "sessions": {
          "<session-id>": {"spend_usd": 0.0123, "updated": 1700000000.0}
        }
      }
    }

The ``sandbox`` nesting is the file's only top-level key — this guard owns
the whole file; it carries none of :class:`CostGuard`'s ``version`` /
``limits`` / ``usage`` keys.

* ``rates`` — per-backend USD-per-second rate. Config-driven, never
  hard-coded into call sites: :data:`DEFAULT_BACKEND_RATES_USD_PER_SECOND`
  seeds the file on first use, and operators tune it with
  ``oc sandbox set`` is *not* the surface — rates are edited via
  :meth:`SandboxCostGuard.set_rate` (exposed through ``oc cost`` / direct
  JSON edit). A backend absent from ``rates`` costs ``$0`` (the local
  backends are free and intentionally unlisted).
* ``session_cap_usd`` — the per-session ceiling. Default
  :data:`DEFAULT_SESSION_CAP_USD` (``$1``); the plan's suggested value.
* ``sessions`` — running per-session spend. Pruned to the most-recent
  :data:`_MAX_TRACKED_SESSIONS` so a long-lived gateway can't grow the
  file without bound.

Writes are atomic + ``0600``-moded via the same tmp-and-``os.replace``
pattern :class:`CostGuard` uses.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opencomputer.agent.config import _home

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config-driven defaults — NOT hard-coded at call sites
# ---------------------------------------------------------------------------

#: E2B's published per-second rate, summed across the CPU + RAM components
#: of the default 2-vCPU sandbox. From the SDK survey
#: (``docs/refs/e2b/2026-05-16-sdk-survey.md`` §A.5): CPU ≈ ``$0.000028/s``
#: plus RAM ≈ ``$0.0000045/GiB/s``. This is a *seed default* written to
#: ``sandbox_cost_guard.json`` on first use — E2B adjusts its rates, so
#: operators edit the persisted ``rates`` value rather than this constant.
DEFAULT_E2B_RATE_USD_PER_SECOND: float = 0.000028 + 0.0000045

#: Daytona's per-second rate for a 2-vCPU + 1 GiB sandbox. Public pricing
#: at https://www.daytona.io/pricing (2026): ``$0.0504 / vCPU-hour`` +
#: ``$0.0162 / GiB-hour`` — same per-vCPU rate as E2B. Per-second:
#: ``2 vCPU * (0.0504 / 3600) + 1 GiB * (0.0162 / 3600) ≈ $0.0000325/s``.
#: Conservative seed default; operators tune the persisted ``rates`` value
#: as Daytona adjusts pricing (the cost guard is operator-owned).
DEFAULT_DAYTONA_RATE_USD_PER_SECOND: float = (
    2 * (0.0504 / 3600) + (0.0162 / 3600)
)

#: Modal's per-second rate for a 2-vCPU + 1 GiB sandbox. Public pricing
#: at https://modal.com/pricing (2026): ``$0.01667 / vCPU-hour`` +
#: ``$0.00833 / GiB-hour`` — meaningfully cheaper per-vCPU than E2B /
#: Daytona. Per-second: ``2 vCPU * (0.01667 / 3600) + 1 GiB * (0.00833 /
#: 3600) ≈ $0.0000116/s``. Conservative seed default; operators tune the
#: persisted ``rates`` value as Modal adjusts pricing.
DEFAULT_MODAL_RATE_USD_PER_SECOND: float = (
    2 * (0.01667 / 3600) + (0.00833 / 3600)
)

#: Seed per-backend rate table. The cloud backends (``e2b`` / ``daytona``
#: / ``modal``) have non-zero rates; ``docker`` / ``linux_bwrap`` /
#: ``macos_sandbox_exec`` / ``ssh`` / ``none`` run on hardware the
#: operator already owns and are free — intentionally absent so
#: :meth:`SandboxCostGuard.rate_for` returns ``0.0`` for them. F16 (M2
#: audit): a paid backend with no entry silently bypasses the session
#: cap, so every paid backend MUST be listed here.
DEFAULT_BACKEND_RATES_USD_PER_SECOND: dict[str, float] = {
    "e2b": DEFAULT_E2B_RATE_USD_PER_SECOND,
    "daytona": DEFAULT_DAYTONA_RATE_USD_PER_SECOND,
    "modal": DEFAULT_MODAL_RATE_USD_PER_SECOND,
}

#: Default per-session sandbox spend ceiling, in USD. The parity plan
#: (T2.8) suggests ``$1/session``. Operators retune via
#: :meth:`SandboxCostGuard.set_session_cap`.
DEFAULT_SESSION_CAP_USD: float = 1.0

#: Most-recent sessions to keep in the ``sessions`` map. Bounds the file
#: for a long-lived gateway that accumulates many session ids.
_MAX_TRACKED_SESSIONS = 500


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SandboxBudgetDecision:
    """Result of :meth:`SandboxCostGuard.check_session_budget`.

    Attributes:
        allowed: ``True`` when a further sandboxed run fits under the cap.
        reason: Human-readable explanation — always set on ``allowed=False``,
            informational on ``allowed=True``.
        session_spend_usd: USD already spent on sandboxing in this session.
        session_cap_usd: The per-session ceiling.
    """

    allowed: bool
    reason: str
    session_spend_usd: float
    session_cap_usd: float


# ---------------------------------------------------------------------------
# SandboxCostGuard — main API
# ---------------------------------------------------------------------------


class SandboxCostGuard:
    """Per-session sandbox-spend tracker with a configurable session cap.

    Construct with an explicit storage path for tests; production callers
    use :func:`get_default_sandbox_cost_guard`, which resolves to the
    active profile's ``sandbox_cost_guard.json``. Thread-safe via an
    in-process lock — the same shape
    :class:`~opencomputer.cost_guard.guard.CostGuard` uses.
    """

    def __init__(self, *, storage_path: Path) -> None:
        self._storage_path = storage_path
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Rates
    # ------------------------------------------------------------------

    def rate_for(self, backend: str) -> float:
        """Return the USD-per-second rate for ``backend`` (``0.0`` if free).

        ``backend`` is a sandbox strategy name (``"e2b"`` / ``"docker"`` /
        …). A backend with no entry in the persisted ``rates`` table costs
        ``$0`` — the local backends are free and intentionally unlisted.
        The cloud ``e2b`` rate defaults to
        :data:`DEFAULT_E2B_RATE_USD_PER_SECOND` on a fresh file.
        """
        name = (backend or "").strip().lower()
        if not name:
            return 0.0
        with self._lock:
            rates = self._load()["sandbox"]["rates"]
        raw = rates.get(name)
        if not isinstance(raw, int | float):
            return 0.0
        return max(0.0, float(raw))

    def set_rate(self, backend: str, *, usd_per_second: float) -> None:
        """Set the USD-per-second rate for one sandbox backend.

        Persisted to ``sandbox_cost_guard.json``. A negative rate is
        rejected — a sandbox second can never have negative cost.
        """
        if usd_per_second < 0:
            raise ValueError(
                f"usd_per_second must be >= 0, got {usd_per_second!r}"
            )
        name = _normalise_backend(backend)
        with self._lock:
            state = self._load()
            state["sandbox"]["rates"][name] = float(usd_per_second)
            self._save(state)

    # ------------------------------------------------------------------
    # Session cap
    # ------------------------------------------------------------------

    def session_cap_usd(self) -> float:
        """Return the per-session sandbox-spend ceiling, in USD."""
        with self._lock:
            return self._load()["sandbox"]["session_cap_usd"]

    def set_session_cap(self, cap_usd: float) -> None:
        """Set the per-session sandbox-spend ceiling.

        Persisted to ``sandbox_cost_guard.json``. A negative cap is
        rejected. ``0`` is a valid cap — it means "no sandboxed run is
        ever in budget", which fully disables paid sandboxing for the
        profile.
        """
        if cap_usd < 0:
            raise ValueError(f"cap_usd must be >= 0, got {cap_usd!r}")
        with self._lock:
            state = self._load()
            state["sandbox"]["session_cap_usd"] = float(cap_usd)
            self._save(state)

    # ------------------------------------------------------------------
    # Per-session spend
    # ------------------------------------------------------------------

    def session_spend(self, session_id: str) -> float:
        """Return USD already spent sandboxing in ``session_id`` (``0`` if none)."""
        sid = (session_id or "").strip()
        if not sid:
            return 0.0
        with self._lock:
            sessions = self._load()["sandbox"]["sessions"]
        entry = sessions.get(sid)
        if not isinstance(entry, dict):
            return 0.0
        raw = entry.get("spend_usd")
        if not isinstance(raw, int | float):
            return 0.0
        return max(0.0, float(raw))

    def cost_for_run(self, *, backend: str, duration_seconds: float) -> float:
        """Compute the USD cost of one sandboxed run — ``duration × rate``.

        Returns ``0.0`` for a free (local) backend or a non-positive
        duration. This is a pure read; it records nothing.
        """
        if duration_seconds <= 0:
            return 0.0
        return self.rate_for(backend) * float(duration_seconds)

    def check_session_budget(
        self,
        session_id: str,
        *,
        projected_cost_usd: float = 0.0,
    ) -> SandboxBudgetDecision:
        """Decide whether a further sandboxed run fits under the session cap.

        Pure read — does not record. ``projected_cost_usd`` is added to the
        session's current spend for the comparison so a caller can
        pre-flight the *next* run; pass ``0`` to test only what is already
        spent (the loop's pre-run gate uses ``0`` — a sandbox's cost is
        unknown until it has run).
        """
        if projected_cost_usd < 0:
            raise ValueError(
                f"projected_cost_usd must be >= 0, got {projected_cost_usd!r}"
            )
        spend = self.session_spend(session_id)
        cap = self.session_cap_usd()
        projected = spend + projected_cost_usd
        if projected > cap:
            return SandboxBudgetDecision(
                allowed=False,
                reason=(
                    f"sandbox session cap exceeded: {projected:.4f} USD "
                    f"> {cap:.4f} USD cap for this session"
                ),
                session_spend_usd=spend,
                session_cap_usd=cap,
            )
        pct = f"{projected / cap * 100:.0f}% of the" if cap > 0 else "no"
        return SandboxBudgetDecision(
            allowed=True,
            reason=f"within budget — {pct} ${cap:.2f}/session sandbox cap",
            session_spend_usd=spend,
            session_cap_usd=cap,
        )

    def record_run(
        self,
        session_id: str,
        *,
        backend: str,
        duration_seconds: float,
    ) -> float:
        """Record one sandboxed run's spend against ``session_id``.

        Computes ``duration × rate(backend)`` and adds it to the session's
        running total. Returns the USD cost actually recorded — ``0.0``
        for a free local backend, and ``0.0`` when ``session_id`` is empty
        or the duration is non-positive (nothing is persisted in those
        cases, so nothing was "recorded").
        """
        sid = (session_id or "").strip()
        cost = self.cost_for_run(backend=backend, duration_seconds=duration_seconds)
        if not sid or cost <= 0:
            return 0.0
        with self._lock:
            state = self._load()
            sessions = state["sandbox"]["sessions"]
            entry = sessions.get(sid)
            prior = 0.0
            if isinstance(entry, dict) and isinstance(
                entry.get("spend_usd"), int | float
            ):
                prior = max(0.0, float(entry["spend_usd"]))
            sessions[sid] = {
                "spend_usd": prior + cost,
                "updated": time.time(),
            }
            self._prune_sessions(state)
            self._save(state)
        return cost

    def reset_session(self, session_id: str | None = None) -> None:
        """Clear recorded sandbox spend. Rates + the cap are NOT reset.

        ``session_id=None`` clears every session's spend.
        """
        with self._lock:
            state = self._load()
            sessions = state["sandbox"]["sessions"]
            if session_id is None:
                sessions.clear()
            else:
                sessions.pop((session_id or "").strip(), None)
            self._save(state)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _prune_sessions(state: dict[str, Any]) -> None:
        """Keep only the most-recently-updated ``_MAX_TRACKED_SESSIONS``."""
        sessions = state["sandbox"]["sessions"]
        if len(sessions) <= _MAX_TRACKED_SESSIONS:
            return
        # Sort by ``updated`` descending; drop the stale tail.
        ordered = sorted(
            sessions.items(),
            key=lambda kv: (
                kv[1].get("updated", 0.0)
                if isinstance(kv[1], dict)
                else 0.0
            ),
            reverse=True,
        )
        state["sandbox"]["sessions"] = dict(ordered[:_MAX_TRACKED_SESSIONS])

    def _load(self) -> dict[str, Any]:
        """Read ``sandbox_cost_guard.json`` and return a normalised state dict.

        Robust to a missing, corrupt, or non-``dict`` file — any of those
        yields a fresh state with the published default rate table and the
        default session cap. This guard owns the whole file, so the only
        top-level key is ``sandbox``.
        """
        if self._storage_path.exists():
            try:
                with open(self._storage_path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except json.JSONDecodeError as exc:
                logger.error(
                    "sandbox_cost_guard.json corrupted: %s — starting fresh",
                    exc,
                )
                data = {}
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}

        raw_sandbox = data.get("sandbox")
        raw_sandbox = raw_sandbox if isinstance(raw_sandbox, dict) else {}

        raw_rates = raw_sandbox.get("rates")
        if isinstance(raw_rates, dict):
            rates = {
                str(k): float(v)
                for k, v in raw_rates.items()
                if isinstance(v, int | float)
            }
        else:
            # Fresh file — seed the published default rate table.
            rates = dict(DEFAULT_BACKEND_RATES_USD_PER_SECOND)

        raw_cap = raw_sandbox.get("session_cap_usd")
        cap = (
            float(raw_cap)
            if isinstance(raw_cap, int | float) and raw_cap >= 0
            else DEFAULT_SESSION_CAP_USD
        )

        raw_sessions = raw_sandbox.get("sessions")
        sessions = (
            dict(raw_sessions) if isinstance(raw_sessions, dict) else {}
        )

        return {
            "sandbox": {
                "rates": rates,
                "session_cap_usd": cap,
                "sessions": sessions,
            },
        }

    def _save(self, state: dict[str, Any]) -> None:
        """Atomically write ``sandbox_cost_guard.json`` (tmp + ``os.replace``)."""
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


_default_sandbox_guard: SandboxCostGuard | None = None
_default_lock = threading.Lock()


def get_default_sandbox_cost_guard() -> SandboxCostGuard:
    """Return the process-wide :class:`SandboxCostGuard` for the active profile.

    Lazy-constructed on first call; thread-safe. Rooted at the active
    profile's ``sandbox_cost_guard.json`` — its own file, distinct from
    the provider guard's ``cost_guard.json``, so the singleton's
    in-process lock fully serializes every read-modify-write.
    """
    global _default_sandbox_guard
    with _default_lock:
        if _default_sandbox_guard is None:
            _default_sandbox_guard = SandboxCostGuard(
                storage_path=_home() / "sandbox_cost_guard.json"
            )
        return _default_sandbox_guard


def _reset_default_sandbox_cost_guard_for_tests() -> None:
    """Clear the cached default sandbox guard. Tests use this; production does not."""
    global _default_sandbox_guard
    with _default_lock:
        _default_sandbox_guard = None


def _normalise_backend(backend: str) -> str:
    """Lower-case + strip a backend name; reject an empty one."""
    if not backend or not backend.strip():
        raise ValueError("backend must be a non-empty string")
    return backend.strip().lower()


__all__ = [
    "DEFAULT_BACKEND_RATES_USD_PER_SECOND",
    "DEFAULT_DAYTONA_RATE_USD_PER_SECOND",
    "DEFAULT_E2B_RATE_USD_PER_SECOND",
    "DEFAULT_MODAL_RATE_USD_PER_SECOND",
    "DEFAULT_SESSION_CAP_USD",
    "SandboxBudgetDecision",
    "SandboxCostGuard",
    "get_default_sandbox_cost_guard",
]
