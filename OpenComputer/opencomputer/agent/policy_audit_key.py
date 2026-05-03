"""Phase 2 v0: HMAC key sourcing for the policy_audit chain.

Mirrors the consent/audit.py + consent/keyring_adapter.py pattern. Stores
the key under namespace ``opencomputer-policy-audit`` (separate from the
consent chain's namespace so a keyring wipe of one doesn't compromise the
other). File fallback at ``<profile_home>/secrets/policy_audit_hmac.key``
with mode 0o700 on the parent dir.

Generated lazily — first call creates 32 random bytes; subsequent calls
return the same key.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from opencomputer.agent.consent.keyring_adapter import KeyringAdapter

_KEYRING_SERVICE = "opencomputer-policy-audit"
_KEY_NAME = "hmac_key_v1"


def get_policy_audit_hmac_key(profile_home: Path) -> bytes:
    """Return the 32-byte HMAC key for the policy_changes chain.

    Generates and persists a fresh key on first call. Subsequent calls
    return the cached value (whether from keyring or file fallback).
    """
    secrets_dir = profile_home / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    adapter = KeyringAdapter(_KEYRING_SERVICE, fallback_dir=secrets_dir)
    existing = adapter.get(_KEY_NAME)
    if existing:
        try:
            return bytes.fromhex(existing)
        except ValueError:
            pass  # corrupted — regenerate

    new_key = secrets.token_bytes(32)
    adapter.set(_KEY_NAME, new_key.hex())
    return new_key
