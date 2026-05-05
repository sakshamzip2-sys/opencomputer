"""Tests for opencomputer.plugins.catalog_signing (D.3 T3)."""

from __future__ import annotations

import json

import pytest

from opencomputer.plugins.catalog_signing import (
    VerifyResult,
    canonicalize_body,
    generate_keypair,
    public_key_fingerprint,
    sign_catalog,
    verify_catalog,
)

# ─── canonicalize_body ────────────────────────────────────────────────


def test_canonicalize_body_strips_signature_fields():
    body_a = {
        "schema_version": 1,
        "plugins": [{"id": "x"}],
        "signature": "junk",
        "signing_key_fingerprint": "ed25519:abc",
    }
    body_b = {"plugins": [{"id": "x"}], "schema_version": 1}
    assert canonicalize_body(body_a) == canonicalize_body(body_b)


def test_canonicalize_body_is_stable_under_key_order():
    a = {"a": 1, "b": 2, "c": 3}
    b = {"c": 3, "b": 2, "a": 1}
    assert canonicalize_body(a) == canonicalize_body(b)


# ─── public_key_fingerprint ───────────────────────────────────────────


def test_public_key_fingerprint_deterministic():
    pem = b"-----BEGIN PUBLIC KEY-----\nfake\n-----END PUBLIC KEY-----\n"
    a = public_key_fingerprint(pem)
    b = public_key_fingerprint(pem)
    assert a == b
    assert a.startswith("ed25519:")
    assert len(a) > len("ed25519:")


def test_public_key_fingerprint_different_keys_different_fps():
    a = public_key_fingerprint(b"key1")
    b = public_key_fingerprint(b"key2")
    assert a != b


# ─── sign + verify roundtrip ─────────────────────────────────────────


def test_sign_then_verify_roundtrip():
    kp = generate_keypair()
    catalog = {
        "schema_version": 1,
        "plugins": [{"id": "example-tool", "version": "0.1.0"}],
    }

    signed = sign_catalog(catalog, kp.private_pem)
    assert signed.fingerprint == kp.fingerprint
    assert "signature" in signed.catalog
    assert "signing_key_fingerprint" in signed.catalog

    trusted = {kp.fingerprint: kp.public_pem}
    result = verify_catalog(signed.catalog, trusted)
    assert result is VerifyResult.OK


def test_verify_rejects_tampered_body():
    kp = generate_keypair()
    catalog = {"schema_version": 1, "plugins": [{"id": "a"}]}
    sign_catalog(catalog, kp.private_pem)

    # Mutate body after signing — signature should no longer verify.
    catalog["plugins"].append({"id": "b"})

    trusted = {kp.fingerprint: kp.public_pem}
    result = verify_catalog(catalog, trusted)
    assert result is VerifyResult.TAMPERED


def test_verify_rejects_untrusted_key():
    kp_signer = generate_keypair()
    kp_other = generate_keypair()
    catalog = {"schema_version": 1, "plugins": []}
    sign_catalog(catalog, kp_signer.private_pem)

    trusted_with_only_other = {kp_other.fingerprint: kp_other.public_pem}
    result = verify_catalog(catalog, trusted_with_only_other)
    assert result is VerifyResult.UNTRUSTED_KEY


def test_verify_reports_missing_signature():
    catalog = {"schema_version": 1, "plugins": []}  # no signature fields
    kp = generate_keypair()
    trusted = {kp.fingerprint: kp.public_pem}
    result = verify_catalog(catalog, trusted)
    assert result is VerifyResult.MISSING_SIGNATURE


def test_verify_reports_missing_signature_when_empty_strings():
    catalog = {
        "schema_version": 1,
        "plugins": [],
        "signature": "",
        "signing_key_fingerprint": "",
    }
    kp = generate_keypair()
    trusted = {kp.fingerprint: kp.public_pem}
    result = verify_catalog(catalog, trusted)
    assert result is VerifyResult.MISSING_SIGNATURE


def test_verify_handles_malformed_base64():
    kp = generate_keypair()
    catalog = {
        "schema_version": 1,
        "plugins": [],
        "signing_key_fingerprint": kp.fingerprint,
        "signature": "!!! not valid base64 !!!",
    }
    trusted = {kp.fingerprint: kp.public_pem}
    result = verify_catalog(catalog, trusted)
    # Either MALFORMED or TAMPERED depending on backend strictness — both
    # are acceptable failure modes; OK is not.
    assert result is not VerifyResult.OK


def test_verify_handles_corrupt_pem():
    kp = generate_keypair()
    catalog = {"schema_version": 1, "plugins": []}
    sign_catalog(catalog, kp.private_pem)

    # Trusted key entry has the right fingerprint but garbage PEM.
    bad_trusted = {kp.fingerprint: b"-----not a real PEM-----"}
    result = verify_catalog(catalog, bad_trusted)
    assert result is VerifyResult.MALFORMED


# ─── generate_keypair ─────────────────────────────────────────────────


def test_generate_keypair_emits_ed25519_pems():
    kp = generate_keypair()
    assert kp.private_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert kp.public_pem.startswith(b"-----BEGIN PUBLIC KEY-----")
    assert kp.fingerprint.startswith("ed25519:")


def test_generate_keypair_is_unique_per_call():
    a = generate_keypair()
    b = generate_keypair()
    assert a.private_pem != b.private_pem
    assert a.public_pem != b.public_pem
    assert a.fingerprint != b.fingerprint


# ─── Wire-through into remote_install ──────────────────────────────────


def test_remote_install_rejects_unsigned_catalog_when_keys_present():
    """When trusted_keys are provided, an unsigned catalog should be rejected."""
    from opencomputer.plugins.remote_install import (
        CatalogSignatureError,
        fetch_catalog,
    )

    kp = generate_keypair()
    unsigned = {"schema_version": 1, "plugins": []}

    def fake_get(url):
        return unsigned

    with pytest.raises(CatalogSignatureError):
        fetch_catalog(
            url="https://x/",
            http_get_json=fake_get,
            trusted_keys={kp.fingerprint: kp.public_pem},
        )


def test_remote_install_passes_when_signed_and_trusted(tmp_path):
    from opencomputer.plugins.remote_install import fetch_catalog

    kp = generate_keypair()
    catalog = {"schema_version": 1, "plugins": []}
    sign_catalog(catalog, kp.private_pem)

    def fake_get(url):
        return catalog

    result = fetch_catalog(
        url="https://x/",
        cache_path_override=tmp_path / "cache.json",
        http_get_json=fake_get,
        trusted_keys={kp.fingerprint: kp.public_pem},
    )
    assert result == catalog
