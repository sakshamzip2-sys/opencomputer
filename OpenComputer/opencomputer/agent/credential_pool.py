"""Credential pool with pluggable rotation strategies + JWT refresh.

Mirrors hermes-agent v0.7's credential_pool.py pattern: per-provider
multi-key pool; configurable distribution strategy; on 401, key gets
quarantined for ROTATE_COOLDOWN_SECONDS (or reset_at if provided) and
the next key is tried.

JWT keys are auto-refreshed when within 60s of expiry if a refresher
callback is supplied.

Single-key behavior IDENTICAL to no-pool path (regression test enforces).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random as _random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ROTATE_COOLDOWN_SECONDS: float = 60.0
EXHAUSTED_TTL_429_SECONDS: float = 3600.0
_JWT_REFRESH_THRESHOLD_S: float = 60.0  # refresh if expiry within this many seconds

STRATEGY_FILL_FIRST = "fill_first"
STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGY_RANDOM = "random"
STRATEGY_LEAST_USED = "least_used"
SUPPORTED_STRATEGIES = frozenset({
    STRATEGY_FILL_FIRST,
    STRATEGY_ROUND_ROBIN,
    STRATEGY_RANDOM,
    STRATEGY_LEAST_USED,
})


class CredentialPoolExhausted(RuntimeError):  # noqa: N818
    """Raised when every key is quarantined and rotate retries exhausted."""


@dataclass
class _KeyState:
    key: str
    use_count: int = 0
    last_used_at: float = 0.0
    quarantined_until: float = 0.0
    last_failure_reason: str | None = None

    def is_eligible(self, now: float) -> bool:
        return self.quarantined_until <= now


def _parse_jwt_exp(token: str) -> float | None:
    """Return the `exp` claim from a JWT, or None if token is not a valid JWT."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        # add padding
        payload_b64 = parts[1] + "=="
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


class CredentialPool:
    """Thread-safe (asyncio.Lock) credential pool.

    Usage::

        pool = CredentialPool(keys=["sk-a", "sk-b", "sk-c"])
        result = await pool.with_retry(
            lambda key: provider_call_with(key),
            is_auth_failure=lambda exc: "401" in str(exc),
        )
    """

    def __init__(
        self,
        *,
        keys: Sequence[str],
        max_rotation_attempts: int = 3,
        rotate_cooldown_seconds: float = ROTATE_COOLDOWN_SECONDS,
        strategy: str = STRATEGY_LEAST_USED,
        refresher: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        if not keys:
            raise ValueError("CredentialPool requires at least one key")
        if strategy not in SUPPORTED_STRATEGIES:
            raise ValueError(f"strategy must be one of {SUPPORTED_STRATEGIES}")
        self._states: list[_KeyState] = [_KeyState(key=k) for k in keys]
        self._lock: asyncio.Lock = asyncio.Lock()
        self._max_rotation_attempts: int = max_rotation_attempts
        self._cooldown: float = rotate_cooldown_seconds
        self._strategy: str = strategy
        self._rr_index: int = 0
        self._refresher = refresher

    @property
    def size(self) -> int:
        return len(self._states)

    async def _maybe_refresh_jwt(self, state: _KeyState) -> None:
        """Replace state.key with a fresh token if JWT is near expiry."""
        if self._refresher is None:
            return
        exp = _parse_jwt_exp(state.key)
        if exp is None:
            return
        if exp - time.time() < _JWT_REFRESH_THRESHOLD_S:
            new_key = await self._refresher(state.key)
            state.key = new_key

    async def acquire(self) -> str:
        async with self._lock:
            now = time.time()
            eligible = [s for s in self._states if s.is_eligible(now)]
            if not eligible:
                reasons = "; ".join(
                    f"{s.key[:8]}...={s.last_failure_reason or 'unknown'}"
                    for s in self._states
                )
                raise CredentialPoolExhausted(
                    f"All {len(self._states)} keys quarantined: {reasons}"
                )
            if self._strategy == STRATEGY_FILL_FIRST:
                chosen = eligible[0]
            elif self._strategy == STRATEGY_ROUND_ROBIN:
                idx = self._rr_index % len(eligible)
                chosen = eligible[idx]
                self._rr_index = (self._rr_index + 1) % len(eligible)
            elif self._strategy == STRATEGY_RANDOM:
                chosen = _random.choice(eligible)
            else:  # STRATEGY_LEAST_USED
                chosen = min(eligible, key=lambda s: (s.use_count, s.last_used_at))

            await self._maybe_refresh_jwt(chosen)
            chosen.use_count += 1
            chosen.last_used_at = time.time()
            return chosen.key

    async def report_auth_failure(
        self,
        key: str,
        *,
        reason: str = "401",
        reset_at: float | None = None,
    ) -> None:
        async with self._lock:
            now = time.time()
            for s in self._states:
                if s.key == key:
                    if reset_at is not None and reset_at > now:
                        s.quarantined_until = reset_at
                    else:
                        s.quarantined_until = now + self._cooldown
                    s.last_failure_reason = reason
                    logger.warning(
                        "credential_pool: quarantined key %s... for %.0fs (reason: %s)",
                        key[:8],
                        s.quarantined_until - now,
                        reason,
                    )
                    return
            logger.warning(
                "credential_pool: report_auth_failure for unknown key %s...", key[:8]
            )

    async def with_retry(self, fn, *, is_auth_failure):
        attempts = 0
        last_exc: Exception | None = None
        while attempts < self._max_rotation_attempts:
            key = await self.acquire()
            try:
                return await fn(key)
            except Exception as exc:
                if is_auth_failure(exc):
                    await self.report_auth_failure(key, reason=type(exc).__name__)
                    last_exc = exc
                    attempts += 1
                    continue
                raise
        raise CredentialPoolExhausted(
            f"Exhausted {self._max_rotation_attempts} rotation attempts; "
            f"last failure: {last_exc!r}"
        ) from last_exc

    def stats(self) -> dict[str, Any]:
        now = time.time()
        return {
            "size": self.size,
            "keys": [
                {
                    "key_preview": s.key[:8] + "..." if len(s.key) > 8 else s.key,
                    "use_count": s.use_count,
                    "last_used_at": s.last_used_at,
                    "quarantined": not s.is_eligible(now),
                    "quarantine_remaining_s": max(0.0, s.quarantined_until - now),
                    "last_failure_reason": s.last_failure_reason,
                }
                for s in self._states
            ],
        }


__all__ = [
    "CredentialPool",
    "CredentialPoolExhausted",
    "ROTATE_COOLDOWN_SECONDS",
    "EXHAUSTED_TTL_429_SECONDS",
    "STRATEGY_FILL_FIRST",
    "STRATEGY_ROUND_ROBIN",
    "STRATEGY_RANDOM",
    "STRATEGY_LEAST_USED",
    "SUPPORTED_STRATEGIES",
]
