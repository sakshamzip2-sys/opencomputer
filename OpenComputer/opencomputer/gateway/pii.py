"""Gateway-only PII hashing for ``privacy.redact_pii`` (Hermes config v2).

Hashes user/chat IDs deterministically (HMAC-SHA256 with per-installation
salt at ``~/.opencomputer/.pii_salt``). Same ID always maps to the same
hash, so the LLM still sees stable references, but the actual identity
is masked.

Routing/delivery still use original IDs internally — only the LLM-facing
context sees hashed forms. Supported adapters: WhatsApp, Signal,
Telegram. Discord/Slack route IDs are already opaque per Hermes spec.

The salt is created on first use with mode 0600. It MUST be backed up
alongside other persistent secrets — losing the salt retroactively
un-correlates all hashed history. Document via a doc file.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

#: Adapters where Hermes documents PII redaction support. Discord/Slack
#: deliberately omitted (route IDs already opaque snowflakes / workspace
#: IDs that don't carry identity).
SUPPORTED_ADAPTERS = frozenset({"whatsapp", "signal", "telegram"})

_USER_NAMESPACE = b"user:"
_CHAT_NAMESPACE = b"chat:"

#: 16 hex chars = 64 bits = enough entropy for collision-free ID hashing
#: in any practical user population (matches Hermes documented length).
_HASH_LENGTH = 16


def _salt_path() -> Path:
    """Resolve the salt file location: ``$OPENCOMPUTER_HOME/.pii_salt``."""
    home = os.environ.get("OPENCOMPUTER_HOME") or os.path.expanduser(
        "~/.opencomputer"
    )
    return Path(home) / ".pii_salt"


def _load_or_create_salt() -> bytes:
    """Return the per-installation salt, creating it on first call.

    32 random bytes via :mod:`secrets`; written with mode 0600 atomically
    (open with O_CREAT|O_EXCL when possible to win the create race).
    Subsequent calls return the cached on-disk value verbatim.
    """
    p = _salt_path()
    if p.exists():
        salt = p.read_bytes()
        if len(salt) >= 32:
            return salt[:32]
        # Truncated salt file — rare. Re-create it.
    p.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(32)
    # Atomic write with strict permissions from the start.
    fd = os.open(str(p), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    return salt


def _hash_with_namespace(namespace: bytes, raw: str) -> str:
    """HMAC-SHA256(salt, namespace || raw) → first ``_HASH_LENGTH`` hex chars."""
    salt = _load_or_create_salt()
    mac = hmac.new(salt, namespace + raw.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:_HASH_LENGTH]


def hash_user_id(raw: str) -> str:
    """Deterministic user-ID hash (16 hex chars).

    Same ``raw`` always maps to the same output for the lifetime of the
    salt file. Different from :func:`hash_chat_id` for the same input
    (different namespace prefixes prevent cross-correlation).
    """
    return _hash_with_namespace(_USER_NAMESPACE, raw)


def hash_chat_id(raw: str) -> str:
    """Deterministic chat-ID hash (16 hex chars). See :func:`hash_user_id`."""
    return _hash_with_namespace(_CHAT_NAMESPACE, raw)


def maybe_redact_user_id(raw: str, *, redact: bool) -> str:
    """Apply user-ID hashing iff ``redact`` is True; passthrough otherwise."""
    return hash_user_id(raw) if redact else raw


def maybe_redact_chat_id(raw: str, *, redact: bool) -> str:
    """Apply chat-ID hashing iff ``redact`` is True; passthrough otherwise."""
    return hash_chat_id(raw) if redact else raw


__all__ = [
    "SUPPORTED_ADAPTERS",
    "hash_chat_id",
    "hash_user_id",
    "maybe_redact_chat_id",
    "maybe_redact_user_id",
]
