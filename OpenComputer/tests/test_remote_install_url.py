"""Tests for install_from_url — required sha256 + gzip-magic + id check."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from opencomputer.plugins.remote_install import (
    PluginIdMismatchError,
    TarballChecksumError,
    UnsupportedTarballFormatError,
    install_from_url,
)
from tests._helpers.install_fixtures import make_tarball as _make_tarball


def test_install_from_url_happy_path(tmp_path: Path):
    raw = _make_tarball("url-example")
    sha = hashlib.sha256(raw).hexdigest()
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    result = install_from_url(
        "https://example.test/url-example.tgz",
        dest_root=dest_root,
        plugin_id_hint="url-example",
        sha256=sha,
        http_get_bytes_fn=lambda url, max_bytes: raw,
    )
    assert result.plugin_id == "url-example"


def test_install_from_url_requires_sha256(tmp_path: Path):
    raw = _make_tarball("no-sha")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    with pytest.raises(TarballChecksumError, match="--sha256"):
        install_from_url(
            "https://example.test/x.tgz",
            dest_root=dest_root,
            plugin_id_hint="no-sha",
            sha256=None,
            http_get_bytes_fn=lambda url, max_bytes: raw,
        )


def test_install_from_url_sha256_mismatch_rejected(tmp_path: Path):
    raw = _make_tarball("bad-sha")
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    with pytest.raises(TarballChecksumError, match="sha256 mismatch"):
        install_from_url(
            "https://example.test/x.tgz",
            dest_root=dest_root,
            plugin_id_hint="bad-sha",
            sha256="0" * 64,
            http_get_bytes_fn=lambda url, max_bytes: raw,
        )


def test_install_from_url_id_mismatch_rejected(tmp_path: Path):
    raw = _make_tarball("real-id")
    sha = hashlib.sha256(raw).hexdigest()
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    with pytest.raises(PluginIdMismatchError):
        install_from_url(
            "https://example.test/x.tgz",
            dest_root=dest_root,
            plugin_id_hint="WRONG-id",
            sha256=sha,
            http_get_bytes_fn=lambda url, max_bytes: raw,
        )


def test_install_from_url_unsupported_format_rejected(tmp_path: Path):
    dest_root = tmp_path / "plugins"
    dest_root.mkdir()

    raw = b"PK\x03\x04" + b"\x00" * 32  # zip magic prefix, not gzip
    sha = hashlib.sha256(raw).hexdigest()

    with pytest.raises(UnsupportedTarballFormatError):
        install_from_url(
            "https://example.test/x.zip",
            dest_root=dest_root,
            plugin_id_hint="zip",
            sha256=sha,
            http_get_bytes_fn=lambda url, max_bytes: raw,
        )
