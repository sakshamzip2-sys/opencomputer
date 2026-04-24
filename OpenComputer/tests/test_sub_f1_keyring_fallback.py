"""Keyring adapter with graceful fallback when keyring unavailable."""
import logging
from unittest.mock import patch

from opencomputer.agent.consent.keyring_adapter import KeyringAdapter


def test_keyring_round_trip_when_available(tmp_path):
    adapter = KeyringAdapter(service="opencomputer-test-unique", fallback_dir=tmp_path)
    adapter.set("mykey", "myvalue")
    assert adapter.get("mykey") == "myvalue"
    # cleanup (the macOS Keychain will persist otherwise)
    try:
        import keyring
        keyring.delete_password("opencomputer-test-unique", "mykey")
    except Exception:
        pass


def test_falls_back_to_file_when_keyring_raises(tmp_path, caplog):
    adapter = KeyringAdapter(service="test-service", fallback_dir=tmp_path)
    with patch(
        "opencomputer.agent.consent.keyring_adapter.keyring.set_password",
        side_effect=RuntimeError("no daemon"),
    ):
        with caplog.at_level(logging.WARNING):
            adapter.set("k", "v")
        assert "fall" in caplog.text.lower() or "fallback" in caplog.text.lower()
    # stored in fallback file
    assert (tmp_path / "test-service.json").exists()

    with patch(
        "opencomputer.agent.consent.keyring_adapter.keyring.get_password",
        side_effect=RuntimeError("no daemon"),
    ):
        assert adapter.get("k") == "v"


def test_missing_key_returns_none(tmp_path):
    adapter = KeyringAdapter(service="test-service", fallback_dir=tmp_path)
    assert adapter.get("nonexistent") is None


def test_get_tries_file_when_keyring_returns_none(tmp_path):
    """Keyring silently returns None for missing keys — check file next."""
    adapter = KeyringAdapter(service="test-service-2", fallback_dir=tmp_path)
    # Write directly to fallback file
    (tmp_path / "test-service-2.json").write_text('{"k": "file-value"}')
    # With working keyring but no keyring entry, falls back to file
    assert adapter.get("k") == "file-value"


def test_fallback_file_has_0600_permissions(tmp_path):
    """M1 regression — fallback file must be owner-r/w only.

    The fallback holds the HMAC chain key in plaintext; default 0644 on
    multi-user Linux exposes it to other users who could then forge audit
    entries.
    """
    import stat
    adapter = KeyringAdapter(service="perm-test", fallback_dir=tmp_path)
    with patch(
        "opencomputer.agent.consent.keyring_adapter.keyring.set_password",
        side_effect=RuntimeError("no daemon"),
    ):
        adapter.set("k", "secret")
    fallback = tmp_path / "perm-test.json"
    assert fallback.exists()
    mode = stat.S_IMODE(fallback.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
