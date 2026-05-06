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
import hashlib
import json
import logging
import random as _random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _safe_id(key: str, pool_index: int) -> str:
    """Return a stable, non-secret identifier for ``key`` for log lines.

    Replaces the old ``key[:8]`` fragment which leaked vendor format
    + 1 byte of secret entropy (RR-4). The sha256 12-char prefix is
    cryptographically irreversible; the pool index lets operators
    correlate without ambiguity across multiple keys with similar
    hashes.
    """
    if not key:
        return f"cred_pool[{pool_index}]:empty"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"cred_pool[{pool_index}]:{digest}"


ROTATE_COOLDOWN_SECONDS: float = 60.0
EXHAUSTED_TTL_429_SECONDS: float = 3600.0
_JWT_REFRESH_THRESHOLD_S: float = 60.0  # refresh if expiry within this many seconds

STRATEGY_FILL_FIRST = "fill_first"
STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGY_RANDOM = "random"
STRATEGY_LEAST_USED = "least_used"
SUPPORTED_STRATEGIES = frozenset(
    {
        STRATEGY_FILL_FIRST,
        STRATEGY_ROUND_ROBIN,
        STRATEGY_RANDOM,
        STRATEGY_LEAST_USED,
    }
)


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
        state_file: str | None = None,
        provider_label: str | None = None,
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
        # Phase 5 (2026-05-07) — live pool quarantine state. When ``state_file``
        # is set, every quarantine event writes the current stats() snapshot
        # to that path as JSON. ``oc doctor --auth`` reads these to surface
        # live runtime state outside the gateway process.
        self._state_file: str | None = state_file
        self._provider_label: str = provider_label or "unknown"
        if state_file:
            self._write_state_file()

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
                    f"{_safe_id(s.key, idx)}={s.last_failure_reason or 'unknown'}"
                    for idx, s in enumerate(self._states)
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
            self._write_state_file()
            return chosen.key

    def _write_state_file(self) -> None:
        """Atomically write current stats to the configured state file.

        No-op when ``state_file`` was not configured. Failures are
        swallowed (logged at WARNING) so a broken state-write never
        prevents the actual credential acquire/rotate path from running.
        """
        if not self._state_file:
            return
        import json
        import os
        import tempfile
        from pathlib import Path

        try:
            payload = {
                "provider": self._provider_label,
                "snapshot_at": time.time(),
                **self.stats(),
            }
            path = Path(self._state_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".pool-", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:  # noqa: BLE001 — state-write must never block runtime
            logger.warning("credential_pool: state-file write failed: %r", e)

    async def report_auth_failure(
        self,
        key: str,
        *,
        reason: str = "401",
        reset_at: float | None = None,
    ) -> None:
        async with self._lock:
            now = time.time()
            for idx, s in enumerate(self._states):
                if s.key == key:
                    if reset_at is not None and reset_at > now:
                        s.quarantined_until = reset_at
                    else:
                        s.quarantined_until = now + self._cooldown
                    s.last_failure_reason = reason
                    logger.warning(
                        "credential_pool: quarantined key %s for %.0fs (reason: %s)",
                        _safe_id(key, idx),
                        s.quarantined_until - now,
                        reason,
                    )
                    self._write_state_file()
                    return
            logger.warning(
                "credential_pool: report_auth_failure for unknown key %s",
                _safe_id(key, pool_index=-1),
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
            f"Exhausted {self._max_rotation_attempts} rotation attempts; last failure: {last_exc!r}"
        ) from last_exc

    def stats(self) -> dict[str, Any]:
        now = time.time()
        return {
            "size": self.size,
            "keys": [
                {
                    "key_preview": _safe_id(s.key, idx),
                    "use_count": s.use_count,
                    "last_used_at": s.last_used_at,
                    "quarantined": not s.is_eligible(now),
                    "quarantine_remaining_s": max(0.0, s.quarantined_until - now),
                    "last_failure_reason": s.last_failure_reason,
                }
                for idx, s in enumerate(self._states)
            ],
        }


def read_all_pool_states(home_dir: str) -> list[dict[str, Any]]:
    """Scan ``home_dir`` for ``auth_pool_*.json`` files and return their data.

    Phase 5 (2026-05-07) — companion to ``CredentialPool._write_state_file``.
    Used by ``oc doctor --auth`` to surface live pool quarantine state from
    a running gateway/chat process. Bad/missing files are skipped silently.
    """
    import glob
    import json
    from pathlib import Path

    out: list[dict[str, Any]] = []
    pattern = str(Path(home_dir) / "auth_pool_*.json")
    for raw_path in sorted(glob.glob(pattern)):
        try:
            data = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data["_source_file"] = raw_path
            out.append(data)
    return out


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
    "read_all_pool_states",
]
