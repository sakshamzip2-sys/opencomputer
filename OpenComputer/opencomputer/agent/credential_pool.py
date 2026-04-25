"""Credential pool with least-used rotation + 401 rotate-and-retry.

Mirrors hermes-agent v0.7's credential_pool.py pattern: per-provider
multi-key pool; least_used distribution; on 401, key gets quarantined
for ROTATE_COOLDOWN_SECONDS and the next key is tried.

Single-key behavior IDENTICAL to no-pool path (regression test enforces).

PR-A of /Users/saksham/.claude/plans/replicated-purring-dewdrop.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ROTATE_COOLDOWN_SECONDS: float = 60.0


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
    ) -> None:
        if not keys:
            raise ValueError("CredentialPool requires at least one key")
        self._states: list[_KeyState] = [_KeyState(key=k) for k in keys]
        self._lock: asyncio.Lock = asyncio.Lock()
        self._max_rotation_attempts: int = max_rotation_attempts
        self._cooldown: float = rotate_cooldown_seconds

    @property
    def size(self) -> int:
        return len(self._states)

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
            chosen = min(eligible, key=lambda s: (s.use_count, s.last_used_at))
            chosen.use_count += 1
            chosen.last_used_at = now
            return chosen.key

    async def report_auth_failure(self, key: str, *, reason: str = "401") -> None:
        async with self._lock:
            now = time.time()
            for s in self._states:
                if s.key == key:
                    s.quarantined_until = now + self._cooldown
                    s.last_failure_reason = reason
                    logger.warning(
                        "credential_pool: quarantined key %s... for %ds (reason: %s)",
                        key[:8],
                        int(self._cooldown),
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


__all__ = ["CredentialPool", "CredentialPoolExhausted", "ROTATE_COOLDOWN_SECONDS"]
