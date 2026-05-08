"""Load credential lists for CredentialPool from multiple sources.

Sources (in priority order):
1. Numbered env vars: PREFIX_1, PREFIX_2, … (stops at first gap)
2. Config YAML ``credential_pools:`` block
3. OS keyring (service name → comma-separated keys)

Also exposes :func:`resolve_pool_strategy` (T6 — Hermes-doc parity)
which selects the rotation strategy for a given provider per
``Config.credential_pool_strategies``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def load_from_env(prefix: str) -> list[str]:
    """Collect PREFIX_1, PREFIX_2, … stopping at the first missing index."""
    keys: list[str] = []
    i = 1
    while True:
        val = os.environ.get(f"{prefix}_{i}", "").strip()
        if not val:
            break
        keys.append(val)
        i += 1
    return keys


def load_from_config(provider: str, config: dict) -> list[str]:
    """Return the key list for *provider* from ``config["credential_pools"]``."""
    pools = config.get("credential_pools") or {}
    raw = pools.get(provider) or []
    return [str(k) for k in raw if k]


def load_from_keyring(service: str) -> list[str]:
    """Return keys stored in the OS keyring under *service* (comma-separated).

    Returns an empty list when the keyring package is unavailable or the
    service has no stored value.
    """
    try:
        import keyring  # already in OC deps (keyring>=24)

        secret = keyring.get_password(service, "credential_pool")
        if not secret:
            return []
        return [k.strip() for k in secret.split(",") if k.strip()]
    except Exception as exc:  # noqa: BLE001
        logger.debug("credential_sources: keyring unavailable: %s", exc)
        return []


def resolve_keys(
    provider: str,
    *,
    env_prefix: str | None = None,
    config: dict | None = None,
    keyring_service: str | None = None,
) -> list[str]:
    """Collect all keys for *provider* from all configured sources.

    Deduplicates while preserving order (first occurrence wins).
    Returns an empty list when no sources yield keys.
    """
    raw: list[str] = []
    if env_prefix:
        raw.extend(load_from_env(env_prefix))
    if config:
        raw.extend(load_from_config(provider, config))
    if keyring_service:
        raw.extend(load_from_keyring(keyring_service))
    seen: set[str] = set()
    result: list[str] = []
    for k in raw:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def resolve_pool_strategy(cfg: Any, provider: str) -> str:
    """Return the rotation strategy for ``provider`` per ``cfg``.

    T6 — Hermes-doc parity. Reads
    ``cfg.credential_pool_strategies`` (a dict) and returns the value
    for ``provider``. Falls back to ``STRATEGY_LEAST_USED`` when:

    * the key is missing
    * the value is ``None``
    * the value is not in ``SUPPORTED_STRATEGIES`` (logs a warning)
    """
    # Local import — credential_pool depends on this module via dispatch
    # in some setups; avoid an unconditional import-time cycle.
    from opencomputer.agent.credential_pool import (
        STRATEGY_LEAST_USED,
        SUPPORTED_STRATEGIES,
    )

    strategies = getattr(cfg, "credential_pool_strategies", None) or {}
    candidate = strategies.get(provider)
    if candidate is None:
        return STRATEGY_LEAST_USED
    if candidate not in SUPPORTED_STRATEGIES:
        logger.warning(
            "credential_pool_strategies[%s] = %r is unsupported; falling back to %s",
            provider,
            candidate,
            STRATEGY_LEAST_USED,
        )
        return STRATEGY_LEAST_USED
    return candidate


__all__ = [
    "load_from_env",
    "load_from_config",
    "load_from_keyring",
    "resolve_keys",
    "resolve_pool_strategy",
]
