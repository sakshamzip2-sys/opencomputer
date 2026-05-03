"""Typed wire primitives that should never leak through the protocol.

Sub-project G (openclaw-parity) Task 8. Provides ``SecretRef`` - an
opaque reference to a secret (API key, OAuth token, ...) that the wire
serializes as a ref-id only, not the value. Resolution happens
in-process via ``SecretResolver`` which never serializes the registry.

Mirrors openclaw ``primitives.secretref.test.ts`` - secret references
are a typed primitive whose ``model_dump()`` cannot accidentally
include the value.

**Adoption pattern**: use ``SecretRef`` in NEW wire methods that carry
credentials (e.g. ``auth.set_token``), not in existing
``params: dict[str, Any]`` callsites. Migrating the existing call
sites is a separate hardening pass.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

__all__ = [
    "SecretRef",
    "SecretResolver",
]


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Opaque reference to a secret. The wire transport never
    serializes the value; only ``ref_id`` and ``hint`` (which is safe
    to log).

    Construct directly only when you already have a ref_id (e.g.
    parsed from wire). Most callers should use
    ``SecretResolver.register(value=...)`` which generates a fresh
    ref_id and stashes the value in-process.
    """

    ref_id: str
    hint: str = ""

    def model_dump(self) -> dict[str, str]:
        """Wire representation - explicit ``$secret_ref`` discriminator
        so receivers can detect a SecretRef inside an arbitrary
        ``dict[str, Any]`` params blob."""
        return {"$secret_ref": self.ref_id, "hint": self.hint}


class SecretResolver:
    """Per-process registry mapping ref_id -> secret value.

    Intentionally NOT thread-safe - callers wrap with their own lock
    if they share a resolver across threads. Intentionally NOT pickled
    - serializing a resolver would defeat the purpose of SecretRef.

    A resolver instance is the natural unit of secret-scope
    (per-session, per-call, per-test). Two resolvers don't share state.
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def register(self, *, value: str, hint: str = "") -> SecretRef:
        """Stash ``value`` and return a SecretRef carrying a fresh
        ref_id + the provided hint. The value never leaves this
        resolver - it's not stored on the SecretRef itself."""
        ref_id = uuid.uuid4().hex
        self._values[ref_id] = value
        return SecretRef(ref_id=ref_id, hint=hint)

    def resolve(self, ref: SecretRef) -> str | None:
        """Return the value for ``ref``, or None if this resolver
        doesn't know the ref_id (different resolver, expired, etc.)."""
        return self._values.get(ref.ref_id)

    def resolve_by_id(self, ref_id: str) -> str | None:
        """Same as ``resolve`` but for callers that only have the
        ref_id string (e.g. parsed from wire JSON)."""
        return self._values.get(ref_id)

    def clear(self) -> None:
        """Drop all registered secrets. Test helper / cleanup."""
        self._values.clear()
