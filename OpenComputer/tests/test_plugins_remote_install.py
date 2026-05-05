"""Tests for opencomputer.plugins.remote_install (D.3 T1)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path

import pytest

from opencomputer.plugins.remote_install import (
    DEFAULT_CACHE_TTL_SECONDS,
    CatalogEntry,
    CatalogFetchError,
    CatalogNotConfiguredError,
    CatalogParseError,
    CatalogSignatureError,
    PluginNotInCatalogError,
    TarballChecksumError,
    TarballTooLargeError,
    download_and_verify,
    extract_tarball,
    fetch_catalog,
    find_entry,
    install_from_catalog,
    read_cache,
    resolve_catalog_url,
    write_cache,
)

# ─── Sample data builders ─────────────────────────────────────────────


def _make_sample_tarball() -> tuple[bytes, str]:
    """Build a valid gzip tarball with one plugin.json file. Returns (raw, sha256)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        manifest = {"id": "example-tool", "version": "0.1.0", "name": "ex"}
        data = json.dumps(manifest).encode("utf-8")
        info = tarfile.TarInfo("plugin.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    return raw, hashlib.sha256(raw).hexdigest()


def _sample_catalog(tarball_url: str, sha: str) -> dict:
    return {
        "schema_version": 1,
        "generated_at": "2026-05-05T00:00:00Z",
        "plugins": [
            {
                "id": "example-tool",
                "version": "0.1.0",
                "description": "an example",
                "tarball_url": tarball_url,
                "tarball_sha256": sha,
                "license": "MIT",
            }
        ],
    }


# ─── resolve_catalog_url ──────────────────────────────────────────────


def test_resolve_catalog_url_from_env(monkeypatch):
    monkeypatch.setenv("OC_PLUGIN_CATALOG_URL", "https://example.com/c.json")
    assert resolve_catalog_url() == "https://example.com/c.json"


def test_resolve_catalog_url_raises_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("OC_PLUGIN_CATALOG_URL", raising=False)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    with pytest.raises(CatalogNotConfiguredError):
        resolve_catalog_url()


# ─── read_cache / write_cache ─────────────────────────────────────────


def test_write_then_read_cache_roundtrip(tmp_path: Path):
    p = tmp_path / "cache.json"
    catalog = {"plugins": []}
    write_cache(catalog, path=p)
    result = read_cache(p)
    assert result is not None
    cached, ts = result
    assert cached == catalog
    assert ts > 0


def test_read_cache_missing_file(tmp_path: Path):
    assert read_cache(tmp_path / "nope.json") is None


def test_read_cache_corrupt_returns_none(tmp_path: Path):
    p = tmp_path / "cache.json"
    p.write_text("not json{")
    assert read_cache(p) is None


# ─── fetch_catalog ────────────────────────────────────────────────────


def test_fetch_catalog_uses_cache_when_fresh(tmp_path: Path):
    cache_p = tmp_path / "cache.json"
    catalog = {"plugins": [{"id": "stale-but-cached"}]}
    write_cache(catalog, path=cache_p)

    fetched_calls: list[str] = []

    def fake_get(url):
        fetched_calls.append(url)
        return {"plugins": [{"id": "fresh"}]}

    result = fetch_catalog(
        url="https://x/", cache_path_override=cache_p, http_get_json=fake_get,
    )
    assert result == catalog
    assert fetched_calls == []  # cache was fresh — no fetch


def test_fetch_catalog_refreshes_when_ttl_expired(tmp_path: Path):
    cache_p = tmp_path / "cache.json"
    catalog_old = {"plugins": [{"id": "old"}]}
    write_cache(catalog_old, path=cache_p)

    catalog_new = {"plugins": [{"id": "new"}]}

    def fake_get(url):
        return catalog_new

    # now is way in the future relative to cache write
    result = fetch_catalog(
        url="https://x/",
        cache_path_override=cache_p,
        http_get_json=fake_get,
        now=time.time() + DEFAULT_CACHE_TTL_SECONDS + 100,
    )
    assert result == catalog_new


def test_fetch_catalog_falls_back_to_cache_on_fetch_failure(tmp_path: Path):
    cache_p = tmp_path / "cache.json"
    catalog = {"plugins": []}
    write_cache(catalog, path=cache_p)

    def fake_get(url):
        raise OSError("network down")

    # refresh=True forces network attempt; with cache present we degrade.
    result = fetch_catalog(
        url="https://x/",
        cache_path_override=cache_p,
        http_get_json=fake_get,
        refresh=True,
    )
    assert result == catalog


def test_fetch_catalog_raises_when_no_cache_and_fetch_fails(tmp_path: Path):
    cache_p = tmp_path / "cache.json"

    def fake_get(url):
        raise OSError("network down")

    with pytest.raises(CatalogFetchError):
        fetch_catalog(
            url="https://x/",
            cache_path_override=cache_p,
            http_get_json=fake_get,
        )


def test_fetch_catalog_rejects_malformed_payload(tmp_path: Path):
    cache_p = tmp_path / "cache.json"

    def fake_get(url):
        return {"not_plugins": []}

    with pytest.raises(CatalogParseError):
        fetch_catalog(
            url="https://x/",
            cache_path_override=cache_p,
            http_get_json=fake_get,
        )


# ─── find_entry ───────────────────────────────────────────────────────


def test_find_entry_returns_typed_entry():
    cat = _sample_catalog("https://x/t.tgz", "abc123")
    entry = find_entry(cat, "example-tool")
    assert isinstance(entry, CatalogEntry)
    assert entry.id == "example-tool"
    assert entry.version == "0.1.0"
    assert entry.tarball_sha256 == "abc123"


def test_find_entry_raises_when_missing():
    cat = _sample_catalog("https://x/t.tgz", "abc")
    with pytest.raises(PluginNotInCatalogError):
        find_entry(cat, "ghost-plugin")


# ─── download_and_verify ──────────────────────────────────────────────


def test_download_and_verify_passes_on_match():
    raw, sha = _make_sample_tarball()
    entry = CatalogEntry(
        id="example-tool", version="0.1.0", description="",
        tarball_url="https://x/t.tgz", tarball_sha256=sha,
    )

    def fake_get_bytes(url, *, max_bytes):
        return raw

    out = download_and_verify(entry, http_get_bytes=fake_get_bytes)
    assert out == raw


def test_download_and_verify_raises_on_mismatch():
    raw, _ = _make_sample_tarball()
    entry = CatalogEntry(
        id="example-tool", version="0.1.0", description="",
        tarball_url="https://x/t.tgz",
        tarball_sha256="0" * 64,  # deliberately wrong
    )

    def fake_get_bytes(url, *, max_bytes):
        return raw

    with pytest.raises(TarballChecksumError):
        download_and_verify(entry, http_get_bytes=fake_get_bytes)


def test_download_and_verify_raises_on_missing_tarball_url():
    entry = CatalogEntry(
        id="example-tool", version="0.1.0", description="",
        tarball_url="", tarball_sha256="abc",
    )

    def fake_get_bytes(url, *, max_bytes):
        return b""

    with pytest.raises(CatalogParseError):
        download_and_verify(entry, http_get_bytes=fake_get_bytes)


# ─── extract_tarball ──────────────────────────────────────────────────


def test_extract_tarball_creates_destination(tmp_path: Path):
    raw, _ = _make_sample_tarball()
    dest = tmp_path / "ext"
    extract_tarball(raw, dest=dest)
    assert (dest / "plugin.json").exists()
    manifest = json.loads((dest / "plugin.json").read_text())
    assert manifest["id"] == "example-tool"


def test_extract_tarball_refuses_existing_dir(tmp_path: Path):
    raw, _ = _make_sample_tarball()
    dest = tmp_path / "ext"
    dest.mkdir()
    with pytest.raises(FileExistsError):
        extract_tarball(raw, dest=dest)


def test_extract_tarball_rejects_path_traversal(tmp_path: Path):
    """Tarballs with absolute paths or .. escapes must be rejected by filter='data'."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Try to write outside the dest dir.
        info = tarfile.TarInfo("../escape.txt")
        data = b"escaped"
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()

    dest = tmp_path / "ext"
    with pytest.raises(Exception):  # filter='data' rejects this
        extract_tarball(raw, dest=dest)


# ─── End-to-end install_from_catalog ──────────────────────────────────


def test_install_from_catalog_happy_path(tmp_path: Path):
    raw, sha = _make_sample_tarball()
    cat = _sample_catalog("https://x/t.tgz", sha)
    cache_p = tmp_path / "cache.json"

    def fake_fetch(*, url, refresh, trusted_keys=None):
        return cat

    def fake_download(entry):
        return raw

    def fake_extract(raw_bytes, *, dest):
        # Mimic real extract — just write a marker.
        dest.mkdir(parents=True)
        (dest / "plugin.json").write_text(
            json.dumps({"id": "example-tool", "version": "0.1.0"})
        )
        return dest

    dest_root = tmp_path / "plugins"
    dest_root.mkdir()
    result = install_from_catalog(
        "example-tool",
        dest_root=dest_root,
        fetch_catalog_fn=fake_fetch,
        download_fn=fake_download,
        extract_fn=fake_extract,
    )
    assert result.plugin_id == "example-tool"
    assert result.version == "0.1.0"
    assert result.install_path.exists()


def test_install_from_catalog_unknown_slug_raises(tmp_path: Path):
    def fake_fetch(*, url, refresh, trusted_keys=None):
        return {"plugins": []}

    def fake_download(entry):  # pragma: no cover — never called
        return b""

    def fake_extract(raw, *, dest):  # pragma: no cover — never called
        return dest

    with pytest.raises(PluginNotInCatalogError):
        install_from_catalog(
            "ghost",
            dest_root=tmp_path,
            fetch_catalog_fn=fake_fetch,
            download_fn=fake_download,
            extract_fn=fake_extract,
        )
