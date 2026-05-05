"""Ed25519 sign + verify for the plugin catalog (D.3 T3).

Mirrors the standard apt/yum signed-repo pattern: an operator generates a
keypair, signs their catalog JSON with the private key, and distributes
the public key to users via README. Users add the public key to their
``~/.opencomputer/trusted_catalog_keys.json`` to enforce signature
verification on every catalog fetch.

Wire format addition to the catalog JSON:

    {
      "schema_version": 1,
      "plugins": [...],
      "signing_key_fingerprint": "ed25519:<sha256-of-pem-prefix>",
      "signature": "<base64 ed25519 over canonical body>"
    }

Canonicalization: ``json.dumps(body, sort_keys=True, separators=(",", ":"))``
applied to the catalog with ``signature`` and ``signing_key_fingerprint``
fields removed. This produces a stable byte sequence given the same
logical content regardless of dict insertion order or whitespace.

Trusted-keys store at ``~/.opencomputer/trusted_catalog_keys.json``:

    {
      "ed25519:<fingerprint>": {
        "name": "OC official",
        "added_at": "2026-05-05T...",
        "public_key_pem": "-----BEGIN PUBLIC KEY-----\n..."
      }
    }
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any


class VerifyResult(Enum):
    """Outcome of a verify_catalog call."""

    OK = "ok"
    MISSING_SIGNATURE = "missing_signature"
    UNTRUSTED_KEY = "untrusted_key"
    TAMPERED = "tampered"
    MALFORMED = "malformed"


# ─── Canonicalization ─────────────────────────────────────────────────


def canonicalize_body(catalog: dict[str, Any]) -> bytes:
    """Strip signature fields + return canonical bytes for sign/verify.

    Sorted keys, no whitespace. Stable across Python versions.
    """
    body = {
        k: v
        for k, v in catalog.items()
        if k not in ("signature", "signing_key_fingerprint")
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ─── Fingerprint ──────────────────────────────────────────────────────


def public_key_fingerprint(public_key_pem: bytes) -> str:
    """Stable short fingerprint for a PEM-encoded Ed25519 public key.

    sha256 of the PEM bytes, hex-encoded, prefixed with ``ed25519:``.
    """
    digest = hashlib.sha256(public_key_pem).hexdigest()
    return f"ed25519:{digest[:32]}"


# ─── Sign ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SignedCatalog:
    """Result of signing — the catalog with sig fields populated."""

    catalog: dict[str, Any]
    fingerprint: str


def sign_catalog(catalog: dict[str, Any], private_key_pem: bytes) -> SignedCatalog:
    """Sign the catalog body in-place. Returns the (mutated) catalog + fingerprint.

    Raises ``ValueError`` if the PEM doesn't decode to an Ed25519 key.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )

    sk = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(sk, Ed25519PrivateKey):
        raise ValueError("private key is not an Ed25519 key")

    pk = sk.public_key()
    pk_pem = pk.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = public_key_fingerprint(pk_pem)

    body = canonicalize_body(catalog)
    sig = sk.sign(body)

    catalog["signing_key_fingerprint"] = fingerprint
    catalog["signature"] = base64.b64encode(sig).decode("ascii")

    return SignedCatalog(catalog=catalog, fingerprint=fingerprint)


# ─── Verify ───────────────────────────────────────────────────────────


def verify_catalog(
    catalog: dict[str, Any], trusted_keys: dict[str, bytes]
) -> VerifyResult:
    """Verify a catalog's signature against a set of trusted public keys.

    ``trusted_keys`` maps fingerprint → PEM bytes. Returns a
    :class:`VerifyResult` enum — never raises for normal failure modes.

    Order of checks:
      1. Body lacks ``signature`` or ``signing_key_fingerprint`` → MISSING_SIGNATURE
      2. fingerprint not in trusted_keys → UNTRUSTED_KEY
      3. Trusted PEM doesn't parse OR sig invalid → TAMPERED
      4. All good → OK

    Empty ``trusted_keys`` always returns UNTRUSTED_KEY. Callers wanting
    "advisory mode" should pre-check and skip calling verify_catalog
    when no keys are configured.
    """
    sig_b64 = catalog.get("signature")
    fp = catalog.get("signing_key_fingerprint")
    if not isinstance(sig_b64, str) or not isinstance(fp, str) or not sig_b64 or not fp:
        return VerifyResult.MISSING_SIGNATURE

    pem = trusted_keys.get(fp)
    if pem is None:
        return VerifyResult.UNTRUSTED_KEY

    try:
        sig = base64.b64decode(sig_b64.encode("ascii"))
    except (ValueError, base64.binascii.Error):
        return VerifyResult.MALFORMED

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError:
        # cryptography lib unavailable — treat as advisory.
        return VerifyResult.MALFORMED

    try:
        pk = serialization.load_pem_public_key(pem)
    except Exception:  # noqa: BLE001
        return VerifyResult.MALFORMED
    if not isinstance(pk, Ed25519PublicKey):
        return VerifyResult.MALFORMED

    body = canonicalize_body(catalog)
    try:
        pk.verify(sig, body)
    except InvalidSignature:
        return VerifyResult.TAMPERED
    except Exception:  # noqa: BLE001
        return VerifyResult.MALFORMED

    return VerifyResult.OK


# ─── Keygen helper ────────────────────────────────────────────────────


@dataclass(frozen=True)
class KeyPair:
    """Generated Ed25519 keypair — PEM bytes for both halves."""

    private_pem: bytes
    public_pem: bytes
    fingerprint: str


def generate_keypair() -> KeyPair:
    """Generate a fresh Ed25519 keypair. Used by ``oc plugin catalog keygen``."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sk = Ed25519PrivateKey.generate()
    sk_pem = sk.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pk_pem = sk.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return KeyPair(
        private_pem=sk_pem,
        public_pem=pk_pem,
        fingerprint=public_key_fingerprint(pk_pem),
    )


__all__ = [
    "KeyPair",
    "SignedCatalog",
    "VerifyResult",
    "canonicalize_body",
    "generate_keypair",
    "public_key_fingerprint",
    "sign_catalog",
    "verify_catalog",
]
