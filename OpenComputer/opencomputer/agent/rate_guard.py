"""TS-T7 — Cross-session rate-limit guard, generalized per-provider.

Writes rate-limit state to a shared file under ``<profile_home>/rate_limits/``
so all sessions (CLI, gateway, cron, auxiliary) can check whether a given
provider is currently rate-limited before issuing requests. Prevents
retry amplification when a provider's RPH is tapped.

Each 429 from a provider can trigger up to 9 API calls per conversation
turn (e.g. 3 SDK retries × 3 OC fallback retries), and every one of
those calls counts against the provider's quota. By recording the
rate-limit state on the first 429 and checking it before subsequent
attempts, we eliminate the amplification effect.

Generalized from Hermes's ``nous_rate_guard`` (single-provider) so each
provider plugin (anthropic, openai, …) gets its own state file keyed
by the provider name passed in.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Mapping
from typing import Any

from opencomputer.agent.config import _home

logger = logging.getLogger(__name__)

_STATE_SUBDIR = "rate_limits"


def _state_path(provider: str) -> str:
    """Return the path to ``provider``'s rate-limit state file.

    Lives under the active profile's ``<profile_home>/rate_limits/{provider}.json``
    so multiple OC profiles maintain independent state. ``OPENCOMPUTER_HOME``
    redirects this for tests.
    """
    base = str(_home())
    return os.path.join(base, _STATE_SUBDIR, f"{provider}.json")


def _parse_reset_seconds(headers: Mapping[str, str] | None) -> float | None:
    """Extract the best available reset-time estimate from response headers.

    Priority:
      1. ``x-ratelimit-reset-requests-1h`` — hourly RPH window (most useful)
      2. ``x-ratelimit-reset-requests``    — per-minute RPM window
      3. ``retry-after``                   — generic HTTP header

    Returns seconds-from-now, or None if no usable header is found.
    """
    if not headers:
        return None

    lowered = {k.lower(): v for k, v in headers.items()}

    for key in (
        "x-ratelimit-reset-requests-1h",
        "x-ratelimit-reset-requests",
        "retry-after",
    ):
        raw = lowered.get(key)
        if raw is not None:
            try:
                val = float(raw)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass

    return None


def record_rate_limit(
    provider: str,
    *,
    headers: Mapping[str, str] | None = None,
    error_context: dict[str, Any] | None = None,
    default_cooldown: float = 300.0,
) -> None:
    """Record that ``provider`` is rate-limited.

    Parses the reset time from response headers or error context. Falls
    back to ``default_cooldown`` (5 minutes) if no reset info is
    available. Writes to a shared file all sessions can read.

    Args:
        provider: Provider name (``"anthropic"``, ``"openai"``, …).
            Used to namespace the state file.
        headers: HTTP response headers from the 429 error.
        error_context: Structured error context (e.g. parsed body).
        default_cooldown: Fallback cooldown in seconds when no header data.
    """
    now = time.time()
    reset_at: float | None = None

    # Try headers first (most accurate)
    header_seconds = _parse_reset_seconds(headers)
    if header_seconds is not None:
        reset_at = now + header_seconds

    # Try error_context reset_at (from body parsing)
    if reset_at is None and isinstance(error_context, dict):
        ctx_reset = error_context.get("reset_at")
        if isinstance(ctx_reset, (int, float)) and ctx_reset > now:
            reset_at = float(ctx_reset)

    # Default cooldown
    if reset_at is None:
        reset_at = now + default_cooldown

    path = _state_path(provider)
    try:
        state_dir = os.path.dirname(path)
        os.makedirs(state_dir, exist_ok=True)

        state = {
            "provider": provider,
            "reset_at": reset_at,
            "recorded_at": now,
            "reset_seconds": reset_at - now,
        }

        # Atomic write: write to temp file + rename
        fd, tmp_path = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.replace(tmp_path, path)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        logger.info(
            "%s rate limit recorded: resets in %.0fs (at %.0f)",
            provider, reset_at - now, reset_at,
        )
    except Exception as exc:
        logger.debug("Failed to write %s rate limit state: %s", provider, exc)


def rate_limit_remaining(provider: str) -> float | None:
    """Check if ``provider`` is currently rate-limited.

    Returns:
        Seconds remaining until reset, or ``None`` if not rate-limited.
        Corrupt or missing state files transparently return ``None`` —
        callers don't need to wrap in try/except.
    """
    path = _state_path(provider)
    try:
        with open(path) as f:
            state = json.load(f)
        reset_at = state.get("reset_at", 0)
        remaining = reset_at - time.time()
        if remaining > 0:
            return remaining
        # Expired — clean up
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def clear_rate_limit(provider: str) -> None:
    """Clear ``provider``'s rate-limit state (e.g. after a successful request)."""
    try:
        os.unlink(_state_path(provider))
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Failed to clear %s rate limit state: %s", provider, exc)


def format_remaining(seconds: float) -> str:
    """Format seconds-remaining into a human-readable duration."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        m, sec = divmod(s, 60)
        return f"{m}m {sec}s" if sec else f"{m}m"
    h, remainder = divmod(s, 3600)
    m = remainder // 60
    return f"{h}h {m}m" if m else f"{h}h"


__all__ = [
    "record_rate_limit",
    "rate_limit_remaining",
    "clear_rate_limit",
    "format_remaining",
]
