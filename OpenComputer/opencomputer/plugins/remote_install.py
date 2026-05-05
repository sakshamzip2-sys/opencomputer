"""Install plugins from a remote JSON catalog.

D.3 T1 (2026-05-05). Companion to ``opencomputer plugin install <path>``
(local install from a directory). The remote path resolves a slug
through a fetched catalog → tarball URL + sha256, downloads the tarball,
verifies the checksum, then safely extracts to the destination plugins
directory.

Catalog format (versioned envelope so it can grow):

    {
      "schema_version": 1,
      "generated_at": "2026-05-05T...",
      "signing_key_fingerprint": "ed25519:abc123",  // optional (D.3 T3)
      "signature":                "<base64 ed25519>",  // optional
      "plugins": [
        {
          "id": "example-tool",
          "version": "0.1.0",
          "description": "...",
          "homepage": "https://...",
          "tarball_url": "https://...example-tool-0.1.0.tgz",
          "tarball_sha256": "abc...",
          "min_host_version": "0.1.0",
          "license": "MIT"
        }
      ]
    }

The catalog URL itself is configurable via:

  1. ``OC_PLUGIN_CATALOG_URL`` env var (highest priority)
  2. ``plugins.catalog_url`` in ``~/.opencomputer/config.yaml``
  3. (no built-in default — operator must configure)

Local cache lives at ``~/.opencomputer/plugin_catalog_cache.json`` with
a 24h TTL. Stale → re-fetch + replace; fetch failure with cache
present → use cache + warn; fetch failure no cache → raise.

Tarballs extract via :func:`tarfile.open(...).extractall(filter='data')`
(Python 3.12+) which rejects absolute paths, symlink escapes, and
device files (CVE-2007-4559).
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Hardcoded sane caps. These are deliberately not configurable to keep
# the attack surface small; if you need a bigger plugin, you have a
# bigger problem.
MAX_TARBALL_BYTES = 50 * 1024 * 1024
DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60


class CatalogError(Exception):
    """Base for everything raised by remote_install."""


class CatalogNotConfiguredError(CatalogError):
    """No catalog URL configured anywhere."""


class CatalogFetchError(CatalogError):
    """Network fetch failed AND no usable cache exists."""


class CatalogParseError(CatalogError):
    """Catalog JSON couldn't be parsed."""


class CatalogSignatureError(CatalogError):
    """Catalog has trusted-keys configured but signature verify failed."""


class PluginNotInCatalogError(CatalogError):
    """Slug not present in fetched catalog."""


class TarballChecksumError(CatalogError):
    """Downloaded tarball sha256 didn't match catalog entry."""


class TarballTooLargeError(CatalogError):
    """Downloaded tarball exceeded MAX_TARBALL_BYTES."""


@dataclass(frozen=True)
class CatalogEntry:
    """Resolved plugin entry from a catalog."""

    id: str
    version: str
    description: str
    tarball_url: str
    tarball_sha256: str
    homepage: str = ""
    min_host_version: str = ""
    license: str = ""


# ─── Catalog URL resolution ───────────────────────────────────────────


def resolve_catalog_url(*, env: dict[str, str] | None = None) -> str:
    """Resolve the plugin catalog URL.

    Order: env var > config.yaml > raise. Returns the URL string.
    """
    env_map = env if env is not None else os.environ
    url = env_map.get("OC_PLUGIN_CATALOG_URL", "").strip()
    if url:
        return url

    try:
        from opencomputer.agent.config_store import load_config
    except ImportError:  # pragma: no cover — module always present in real builds
        raise CatalogNotConfiguredError(
            "OC_PLUGIN_CATALOG_URL is unset and config loader unavailable."
        )

    cfg = load_config()
    plugins_block = (
        getattr(cfg, "plugins", None) if hasattr(cfg, "plugins") else None
    )
    candidate = ""
    if plugins_block is not None:
        candidate = (
            getattr(plugins_block, "catalog_url", "")
            if hasattr(plugins_block, "catalog_url")
            else ""
        )
    if not candidate and hasattr(cfg, "raw"):
        # Fall back to raw dict path used by some config shapes.
        raw = getattr(cfg, "raw", {}) or {}
        candidate = ((raw.get("plugins") or {}).get("catalog_url") or "").strip()

    if not candidate:
        raise CatalogNotConfiguredError(
            "no catalog URL configured. Set OC_PLUGIN_CATALOG_URL or "
            "add 'plugins.catalog_url: <url>' to ~/.opencomputer/config.yaml."
        )
    return candidate


# ─── Cache I/O ────────────────────────────────────────────────────────


def cache_path() -> Path:
    """Where the catalog cache lives."""
    from opencomputer.agent.config import _home

    return _home() / "plugin_catalog_cache.json"


def read_cache(path: Path | None = None) -> tuple[dict[str, Any], int] | None:
    """Read the cache file → (catalog, fetched_at_ts) or None if missing/bad."""
    p = path if path is not None else cache_path()
    if not p.exists():
        return None
    try:
        wrapper = json.loads(p.read_text(encoding="utf-8"))
        catalog = wrapper.get("catalog")
        ts = int(wrapper.get("fetched_at", 0))
        if not isinstance(catalog, dict) or ts <= 0:
            return None
        return catalog, ts
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def write_cache(catalog: dict[str, Any], *, path: Path | None = None) -> None:
    """Atomically write the catalog to the cache file."""
    p = path if path is not None else cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": int(time.time()),
        "catalog": catalog,
    }
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ─── HTTP helpers (kept thin so tests can monkeypatch) ────────────────


def _http_get_json(url: str) -> dict[str, Any]:
    import httpx

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def _http_get_bytes(url: str, *, max_bytes: int) -> bytes:
    import httpx

    chunks: list[bytes] = []
    total = 0
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            content_length = resp.headers.get("content-length")
            if content_length and int(content_length) > max_bytes:
                raise TarballTooLargeError(
                    f"server-reported content-length {content_length} > {max_bytes}"
                )
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise TarballTooLargeError(
                        f"streamed bytes exceeded {max_bytes}"
                    )
                chunks.append(chunk)
    return b"".join(chunks)


# ─── Catalog fetch ────────────────────────────────────────────────────


def fetch_catalog(
    *,
    url: str | None = None,
    refresh: bool = False,
    cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
    cache_path_override: Path | None = None,
    http_get_json=_http_get_json,
    now: float | None = None,
    trusted_keys: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """Fetch the catalog JSON, using cache when fresh.

    ``refresh=True`` skips the cache and forces a network fetch.

    ``trusted_keys`` (optional, D.3 T3 wiring) — when present and the
    catalog has a signature, verify; reject on mismatch. When present
    and the catalog has NO signature, raise CatalogSignatureError.
    When None, signature is advisory (warn caller).
    """
    catalog_url = url if url is not None else resolve_catalog_url()
    now_ts = int(now) if now is not None else int(time.time())
    cache_p = cache_path_override or cache_path()

    cached = read_cache(cache_p)
    if not refresh and cached is not None:
        catalog, fetched_at = cached
        if now_ts - fetched_at < cache_ttl:
            _maybe_verify_signature(catalog, trusted_keys)
            return catalog

    try:
        catalog = http_get_json(catalog_url)
    except Exception as e:  # noqa: BLE001
        if cached is not None:
            # Stale cache present — degrade gracefully.
            return cached[0]
        raise CatalogFetchError(f"fetch failed and no cache: {e}") from e

    if not isinstance(catalog, dict) or "plugins" not in catalog:
        raise CatalogParseError(
            "catalog JSON missing required 'plugins' array"
        )

    _maybe_verify_signature(catalog, trusted_keys)

    write_cache(catalog, path=cache_p)
    return catalog


def _maybe_verify_signature(
    catalog: dict[str, Any], trusted_keys: dict[str, bytes] | None
) -> None:
    if not trusted_keys:
        return
    try:
        from opencomputer.plugins.catalog_signing import (
            VerifyResult,
            verify_catalog,
        )
    except ImportError:
        return  # signing module not installed — advisory mode

    result = verify_catalog(catalog, trusted_keys)
    if result is VerifyResult.OK:
        return
    raise CatalogSignatureError(f"signature verify failed: {result.name}")


# ─── Plugin resolution ────────────────────────────────────────────────


def find_entry(catalog: dict[str, Any], slug: str) -> CatalogEntry:
    """Look up a plugin by id in the catalog. Raise if missing."""
    for entry in catalog.get("plugins", []) or []:
        if entry.get("id") == slug:
            return CatalogEntry(
                id=entry["id"],
                version=str(entry.get("version", "")),
                description=str(entry.get("description", "")),
                tarball_url=str(entry.get("tarball_url", "")),
                tarball_sha256=str(entry.get("tarball_sha256", "")),
                homepage=str(entry.get("homepage", "")),
                min_host_version=str(entry.get("min_host_version", "")),
                license=str(entry.get("license", "")),
            )
    raise PluginNotInCatalogError(f"slug not found in catalog: {slug}")


# ─── Tarball download + verify + extract ──────────────────────────────


def download_and_verify(
    entry: CatalogEntry,
    *,
    max_bytes: int = MAX_TARBALL_BYTES,
    http_get_bytes=_http_get_bytes,
) -> bytes:
    """Download + sha256-verify the tarball; return its raw bytes."""
    if not entry.tarball_url:
        raise CatalogParseError(f"catalog entry {entry.id} has no tarball_url")
    if not entry.tarball_sha256:
        raise CatalogParseError(f"catalog entry {entry.id} has no tarball_sha256")

    raw = http_get_bytes(entry.tarball_url, max_bytes=max_bytes)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != entry.tarball_sha256.lower():
        raise TarballChecksumError(
            f"sha256 mismatch for {entry.id}: "
            f"expected {entry.tarball_sha256}, got {actual}"
        )
    return raw


def extract_tarball(raw: bytes, *, dest: Path) -> Path:
    """Safely extract a gzipped tarball to ``dest``. Returns the dest path.

    Uses ``filter='data'`` (Python 3.12+) which rejects absolute paths,
    symlink escapes, and device files. ``dest`` is created fresh — if it
    already exists, the caller is expected to have handled it (force flag
    on the install command).
    """
    import io

    dest.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            tar.extractall(path=dest, filter="data")
    except Exception:
        # Roll back the dest dir on extraction failure so we don't leave
        # a half-populated plugin directory behind.
        import shutil

        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest


# ─── Top-level install flow ───────────────────────────────────────────


@dataclass(frozen=True)
class InstallResult:
    """What the install actually did, for the CLI to report."""

    plugin_id: str
    version: str
    install_path: Path


def install_from_catalog(
    slug: str,
    *,
    dest_root: Path,
    catalog_url: str | None = None,
    refresh: bool = False,
    force: bool = False,
    trusted_keys: dict[str, bytes] | None = None,
    fetch_catalog_fn=fetch_catalog,
    download_fn=download_and_verify,
    extract_fn=extract_tarball,
) -> InstallResult:
    """End-to-end: fetch catalog → resolve slug → download → verify → extract.

    ``dest_root`` is the parent directory where the plugin folder gets
    created (the new folder is named after the plugin id). Existing
    plugin dirs are refused unless ``force=True``.
    """
    catalog = fetch_catalog_fn(
        url=catalog_url, refresh=refresh, trusted_keys=trusted_keys
    )
    entry = find_entry(catalog, slug)

    raw = download_fn(entry)

    plugin_dir = dest_root / entry.id
    if plugin_dir.exists():
        if not force:
            raise CatalogError(
                f"plugin '{entry.id}' already installed at {plugin_dir}. "
                "Use --force to overwrite."
            )
        import shutil

        shutil.rmtree(plugin_dir)

    extract_fn(raw, dest=plugin_dir)
    return InstallResult(
        plugin_id=entry.id, version=entry.version, install_path=plugin_dir
    )


__all__ = [
    "CatalogEntry",
    "CatalogError",
    "CatalogFetchError",
    "CatalogNotConfiguredError",
    "CatalogParseError",
    "CatalogSignatureError",
    "InstallResult",
    "PluginNotInCatalogError",
    "TarballChecksumError",
    "TarballTooLargeError",
    "MAX_TARBALL_BYTES",
    "DEFAULT_CACHE_TTL_SECONDS",
    "cache_path",
    "download_and_verify",
    "extract_tarball",
    "fetch_catalog",
    "find_entry",
    "install_from_catalog",
    "read_cache",
    "resolve_catalog_url",
    "write_cache",
]
