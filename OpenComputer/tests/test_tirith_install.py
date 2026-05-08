"""Tests for the tirith auto-installer (P3.7 deferred-MVP follow-up)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.security.tirith_install import (
    TirithAsset,
    cleanup_stale_tmp_files,
    detect_platform_asset_name,
    fetch_release_asset,
    install_atomic,
    install_if_missing,
    parse_checksums_txt,
    verify_sha256,
)

# ── verify_sha256 ────────────────────────────────────────────────────


def test_verify_sha256_match():
    data = b"hello world"
    # sha256("hello world") = b94d27...
    digest = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert verify_sha256(data, digest) is True


def test_verify_sha256_mismatch():
    assert verify_sha256(b"hello", "0" * 64) is False


def test_verify_sha256_empty_expected():
    assert verify_sha256(b"x", "") is False


def test_verify_sha256_case_insensitive():
    digest = "B94D27B9934D3E08A52E52D7DA7DABFAC484EFE37A5380EE9088F7ACE2EFCDE9"
    assert verify_sha256(b"hello world", digest) is True


# ── parse_checksums_txt ─────────────────────────────────────────────


def test_parse_checksums_finds_target():
    text = (
        "abc123  tirith-linux-x86_64\n"
        "def456  tirith-darwin-arm64\n"
    )
    assert parse_checksums_txt(text, "tirith-linux-x86_64") == "abc123"
    assert parse_checksums_txt(text, "tirith-darwin-arm64") == "def456"


def test_parse_checksums_unknown_returns_none():
    text = "abc123  tirith-linux-x86_64\n"
    assert parse_checksums_txt(text, "tirith-windows-x86_64") is None


def test_parse_checksums_handles_binary_mode_prefix():
    text = "abc123 *tirith-linux-x86_64\n"
    assert parse_checksums_txt(text, "tirith-linux-x86_64") == "abc123"


def test_parse_checksums_skips_comments_and_blanks():
    text = (
        "# This is a comment\n"
        "\n"
        "abc123  tirith-linux-x86_64\n"
        "# another comment\n"
    )
    assert parse_checksums_txt(text, "tirith-linux-x86_64") == "abc123"


# ── detect_platform_asset_name ──────────────────────────────────────


def test_detect_platform_returns_known_or_none():
    name = detect_platform_asset_name()
    valid = {
        "tirith-darwin-arm64", "tirith-darwin-x86_64",
        "tirith-linux-arm64", "tirith-linux-x86_64",
        None,
    }
    assert name in valid


# ── fetch_release_asset (mocked) ─────────────────────────────────────


def _release_payload(asset_name: str, *, with_sig: bool = True) -> dict:
    assets = [
        {
            "name": asset_name,
            "browser_download_url": f"https://example.test/{asset_name}",
        },
        {
            "name": "checksums.txt",
            "browser_download_url": "https://example.test/checksums.txt",
        },
    ]
    if with_sig:
        assets.append({
            "name": f"{asset_name}.sig",
            "browser_download_url": f"https://example.test/{asset_name}.sig",
        })
    return {"tag_name": "v1.0.0", "assets": assets}


def test_fetch_release_asset_success():
    asset_name = "tirith-linux-x86_64"
    payload = _release_payload(asset_name)
    body_sha = "a" * 64

    def fetch(url: str) -> bytes:
        if url.endswith("releases/latest"):
            return json.dumps(payload).encode("utf-8")
        if url.endswith("checksums.txt"):
            return f"{body_sha}  {asset_name}\n".encode()
        raise OSError(f"unexpected url: {url}")

    asset = fetch_release_asset(
        asset_name=asset_name, fetcher=fetch,
        api_url="https://example.test/releases/latest",
    )
    assert asset.asset_name == asset_name
    assert asset.sha256 == body_sha
    assert asset.version == "v1.0.0"
    assert asset.sig_url is not None


def test_fetch_release_asset_missing_asset_raises():
    payload = _release_payload("tirith-linux-x86_64")

    def fetch(url: str) -> bytes:
        return json.dumps(payload).encode("utf-8")

    with pytest.raises(ValueError, match="no asset named"):
        fetch_release_asset(
            asset_name="tirith-darwin-arm64",
            fetcher=fetch,
            api_url="https://example.test/releases/latest",
        )


def test_fetch_release_asset_missing_checksums_raises():
    payload = {
        "tag_name": "v1.0.0",
        "assets": [
            {
                "name": "tirith-linux-x86_64",
                "browser_download_url": "https://example.test/tirith",
            },
        ],
    }

    def fetch(url: str) -> bytes:
        return json.dumps(payload).encode("utf-8")

    with pytest.raises(ValueError, match="missing checksums.txt"):
        fetch_release_asset(
            asset_name="tirith-linux-x86_64",
            fetcher=fetch,
            api_url="https://example.test/releases/latest",
        )


# ── install_atomic ──────────────────────────────────────────────────


def test_install_atomic_success(tmp_path: Path):
    body = b"#!/bin/sh\necho ok\n"
    import hashlib

    digest = hashlib.sha256(body).hexdigest()
    asset = TirithAsset(
        version="v1.0.0",
        asset_name="tirith-linux-x86_64",
        sha256=digest,
        download_url="https://example.test/tirith",
        sig_url=None,
    )

    def fetch(url: str) -> bytes:
        return body

    final = install_atomic(asset=asset, target_dir=tmp_path, fetcher=fetch)
    assert final.exists()
    assert final.name == "tirith"
    assert final.read_bytes() == body
    if sys.platform != "win32":
        # 0o755 on POSIX
        mode = final.stat().st_mode & 0o777
        assert mode == 0o755


def test_install_atomic_sha_mismatch_refuses(tmp_path: Path):
    asset = TirithAsset(
        version="v1.0.0",
        asset_name="tirith-linux-x86_64",
        sha256="0" * 64,  # won't match
        download_url="https://example.test/tirith",
        sig_url=None,
    )

    def fetch(url: str) -> bytes:
        return b"different bytes"

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        install_atomic(asset=asset, target_dir=tmp_path, fetcher=fetch)
    # Tmp file cleaned up.
    leftovers = list(tmp_path.glob(".tirith.tmp.*"))
    assert leftovers == []


def test_install_atomic_no_sig_skips_cosign(tmp_path: Path):
    """asset.sig_url=None → cosign path not entered, install completes."""
    body = b"OK"
    import hashlib

    digest = hashlib.sha256(body).hexdigest()
    asset = TirithAsset(
        version="v1.0.0",
        asset_name="tirith-linux-x86_64",
        sha256=digest,
        download_url="https://example.test/tirith",
        sig_url=None,
    )

    def fetch(url: str) -> bytes:
        return body

    final = install_atomic(asset=asset, target_dir=tmp_path, fetcher=fetch)
    assert final.exists()


def test_install_atomic_cosign_failure_refuses(tmp_path: Path, monkeypatch):
    body = b"OK"
    import hashlib

    digest = hashlib.sha256(body).hexdigest()
    asset = TirithAsset(
        version="v1.0.0",
        asset_name="tirith-linux-x86_64",
        sha256=digest,
        download_url="https://example.test/tirith",
        sig_url="https://example.test/tirith.sig",
    )

    def fetch(url: str) -> bytes:
        if url.endswith(".sig"):
            return b"\x00\x00fake-sig"
        return body

    # Force cosign_verify to return False — simulating a failed
    # provenance check.
    monkeypatch.setattr(
        "opencomputer.security.tirith_install.cosign_verify",
        lambda **kw: False,
    )
    with pytest.raises(ValueError, match="cosign provenance verification FAILED"):
        install_atomic(asset=asset, target_dir=tmp_path, fetcher=fetch)


# ── install_if_missing ──────────────────────────────────────────────


def test_install_if_missing_returns_existing(tmp_path: Path):
    pre = tmp_path / "tirith"
    pre.write_bytes(b"existing")
    assert install_if_missing(target_dir=tmp_path) == pre


def test_install_if_missing_unsupported_platform(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "opencomputer.security.tirith_install.detect_platform_asset_name",
        lambda: None,
    )
    assert install_if_missing(target_dir=tmp_path) is None


def test_install_if_missing_swallows_install_failure(
    monkeypatch, tmp_path: Path, caplog,
):
    """A network / verification failure must NOT raise to the caller."""
    monkeypatch.setattr(
        "opencomputer.security.tirith_install.detect_platform_asset_name",
        lambda: "tirith-linux-x86_64",
    )

    def boom(**kw):
        raise OSError("network down")

    monkeypatch.setattr(
        "opencomputer.security.tirith_install.fetch_release_asset", boom
    )
    out = install_if_missing(target_dir=tmp_path)
    assert out is None


# ── cleanup_stale_tmp_files ────────────────────────────────────────


def test_cleanup_stale_tmp(tmp_path: Path):
    (tmp_path / ".tirith.tmp.1234").write_text("x")
    (tmp_path / ".tirith.tmp.5678").write_text("y")
    (tmp_path / "tirith").write_text("real")
    cleaned = cleanup_stale_tmp_files(tmp_path)
    assert cleaned == 2
    assert (tmp_path / "tirith").exists()
    assert not list(tmp_path.glob(".tirith.tmp.*"))


def test_cleanup_missing_target_dir(tmp_path: Path):
    missing = tmp_path / "nope"
    assert cleanup_stale_tmp_files(missing) == 0
