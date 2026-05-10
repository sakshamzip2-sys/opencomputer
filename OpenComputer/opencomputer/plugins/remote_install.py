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

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Type alias used by install_from_catalog / install_from_git / install_from_url.
# A BeforeInstallHook receives a HookContext (typed at callsite) and returns
# an awaitable that resolves to a HookDecision (or None for "pass").
BeforeInstallHook = Callable[[Any], Awaitable[Any]]

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


class GitNotFoundError(CatalogError):
    """git binary not found on PATH."""


class GitCloneError(CatalogError):
    """git clone failed (network, auth, or remote-not-found)."""


class PluginIdMismatchError(CatalogError):
    """Extracted plugin.json's `id` doesn't match what the user asked for."""


class UnsupportedTarballFormatError(CatalogError):
    """Tarball is not .tar.gz / .tgz."""


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


def extract_tarball(
    raw: bytes,
    *,
    dest: Path,
    strip_top_level: bool = False,
) -> Path:
    """Safely extract a gzipped tarball to ``dest``. Returns the dest path.

    Uses ``filter='data'`` (Python 3.12+) which rejects absolute paths,
    symlink escapes, and device files. ``dest`` is created fresh — if it
    already exists, the caller is expected to have handled it (force flag
    on the install command).

    ``strip_top_level=True`` flattens a PEP-643-style top-level directory
    wrapper (``<name>-<version>/foo`` → ``foo``) when EVERY entry in the
    archive shares the same first path component.  PyPI sdists need this;
    catalog tarballs typically do not.  When the archive is already flat
    or members disagree on the wrapper, falls back to the standard
    extraction so the strip flag is safe to enable opportunistically.
    """
    import io

    dest.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            members = tar.getmembers()
            top_level: str | None = None
            should_strip = False
            if strip_top_level and members:
                top_level = members[0].name.split("/", 1)[0]
                should_strip = bool(top_level) and all(
                    m.name == top_level
                    or m.name.startswith(top_level + "/")
                    for m in members
                )
            if should_strip and top_level is not None:
                prefix = top_level + "/"
                # SECURITY: rebuild a synthetic in-memory archive with the
                # wrapper stripped, then hand it to tarfile.extractall with
                # filter='data' so all the upstream-CPython-vetted
                # path-traversal / symlink-escape / device-file rejections
                # apply uniformly.  Earlier hand-rolled extraction skipped
                # these checks — a malicious sdist could ship
                # "foo-1.0/../../../etc/passwd" and escape ``dest``.
                # https://docs.python.org/3/library/tarfile.html#tarfile-extraction-filter
                buf = io.BytesIO()
                with tarfile.open(
                    fileobj=buf, mode="w:gz"
                ) as rewritten_tar:
                    for m in members:
                        if m.name == top_level:
                            continue  # drop wrapper dir itself
                        stripped_name = m.name[len(prefix):]
                        if not stripped_name:
                            continue
                        # Build a TarInfo with the stripped name; preserve
                        # type / mode / size / mtime / link targets.  The
                        # ``data`` filter applied at extract time rejects
                        # any residual ../-escapes regardless of the input.
                        new_info = tarfile.TarInfo(name=stripped_name)
                        new_info.mode = m.mode
                        new_info.type = m.type
                        new_info.size = m.size
                        new_info.mtime = m.mtime
                        new_info.uid = 0
                        new_info.gid = 0
                        new_info.uname = ""
                        new_info.gname = ""
                        if m.islnk() or m.issym():
                            # Strip the wrapper from link targets too;
                            # filter='data' will reject absolute / escaping
                            # link targets at extract time.
                            link = m.linkname
                            if link.startswith(prefix):
                                link = link[len(prefix):]
                            new_info.linkname = link
                        if m.isreg():
                            f = tar.extractfile(m)
                            payload = f.read() if f is not None else b""
                            rewritten_tar.addfile(
                                new_info, io.BytesIO(payload)
                            )
                        else:
                            rewritten_tar.addfile(new_info)

                # Re-open the synthetic archive and extract through
                # ``filter='data'`` for the path-traversal / device /
                # symlink-escape rejection logic CPython upstreams.
                buf.seek(0)
                with tarfile.open(fileobj=buf, mode="r:gz") as stripped_tar:
                    stripped_tar.extractall(path=dest, filter="data")
            else:
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
    # Phase 1 (2026-05-06) — optional kwargs; default behaviour preserved.
    before_install_hook: BeforeInstallHook | None = None,
    skip_scan: bool = False,
) -> InstallResult:
    """End-to-end: fetch catalog → resolve slug → download → verify → extract
    → security-scan → fire BEFORE_INSTALL hook → record-in-index → finalize.

    ``dest_root`` is the parent directory where the plugin folder gets
    created (the new folder is named after the plugin id). Existing
    plugin dirs are refused unless ``force=True``.

    ``before_install_hook`` is an awaitable callable that receives a
    HookContext and may return a HookDecision with ``decision="block"`` to
    veto the install. ``skip_scan=True`` is a test-only escape hatch.
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
        shutil.rmtree(plugin_dir)

    extract_fn(raw, dest=plugin_dir)

    # Post-extract gate — runs the security scan, fires BEFORE_INSTALL,
    # and rolls back the dest dir on any failure so a vetoed install
    # never lands.
    try:
        _post_extract_gate(
            plugin_dir=plugin_dir,
            install_source="catalog",
            install_url=slug,
            install_plugin_id=entry.id,
            before_install_hook=before_install_hook,
            skip_scan=skip_scan,
        )
    except Exception:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise

    # Record in installed-index for `oc plugin verify`.
    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id=entry.id,
            version=entry.version,
            source="catalog",
            source_url=slug,
            source_ref=None,
            tarball_sha256=entry.tarball_sha256.lower(),
            installed_at=int(time.time()),
        ),
    )

    return InstallResult(
        plugin_id=entry.id, version=entry.version, install_path=plugin_dir
    )


def _git_path() -> str | None:
    """shutil.which wrapped for test patching. Returns None if git not on PATH."""
    return shutil.which("git")


def _normalize_git_url(arg: str) -> str:
    """Strip the leading 'git+' prefix if present; otherwise return unchanged."""
    if arg.startswith("git+"):
        return arg[len("git+"):]
    return arg


def install_from_git(
    url: str,
    *,
    dest_root: Path,
    plugin_id_hint: str,
    ref: str | None = None,
    force: bool = False,
    before_install_hook: BeforeInstallHook | None = None,
    skip_scan: bool = False,
) -> InstallResult:
    """Install a plugin via shallow `git clone`.

    ``url`` accepts ``git+https://...``, ``git+ssh://...``, ``https://...``,
    ``ssh://...``, ``file://...``. The ``git+`` prefix is stripped before
    handing to git.

    ``ref`` pins a specific sha/tag/branch. If None, the default branch's
    HEAD is cloned and its resolved sha is recorded in the installed-index.
    """
    import subprocess

    # Resolve at call-time (not via default arg) so monkeypatching
    # the module-level _git_path symbol takes effect.
    git = _git_path()
    if git is None:
        raise GitNotFoundError(
            "git binary not found on PATH — install Git or use catalog/url install instead."
        )

    plugin_dir = dest_root / plugin_id_hint
    if plugin_dir.exists():
        if not force:
            raise CatalogError(
                f"plugin '{plugin_id_hint}' already installed at {plugin_dir}. "
                "Use --force to overwrite."
            )
        shutil.rmtree(plugin_dir)

    git_url = _normalize_git_url(url)
    # Clone strategy:
    # * No ref → shallow clone of the default branch (depth=1 saves bandwidth).
    # * Explicit ref → full clone, then `git checkout <ref>`. We can't combine
    #   `--depth=1` with an arbitrary sha because shallow clones only know
    #   about the tip of the named branch/tag.
    if ref is None:
        clone_args = [git, "clone", "--depth=1", git_url, str(plugin_dir)]
    else:
        clone_args = [git, "clone", git_url, str(plugin_dir)]

    try:
        subprocess.run(clone_args, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise GitCloneError(
            f"git clone failed: {e.stderr.strip() or e}"
        ) from e

    if ref is not None:
        try:
            subprocess.run(
                [git, "checkout", "--quiet", ref],
                cwd=plugin_dir,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            shutil.rmtree(plugin_dir, ignore_errors=True)
            raise GitCloneError(
                f"git checkout {ref} failed: {e.stderr.strip()}"
            ) from e

    head_sha = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=plugin_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Verify the cloned tree's plugin.json matches plugin_id_hint
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.exists():
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(
            f"cloned repo at {git_url} has no plugin.json at the root"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(
            f"plugin.json is not valid JSON: {e}"
        ) from e

    actual_id = str(manifest.get("id", ""))
    if actual_id != plugin_id_hint:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise PluginIdMismatchError(
            f"plugin.json says id={actual_id!r} but install argument was {plugin_id_hint!r}"
        )

    version = str(manifest.get("version", ""))

    # Strip the .git directory to keep the installed tree clean and to avoid
    # accidental git-related leakage at runtime.
    git_dir = plugin_dir / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)

    # Post-extract gate: scan + BEFORE_INSTALL hook (rolls back on failure).
    try:
        _post_extract_gate(
            plugin_dir=plugin_dir,
            install_source="git",
            install_url=url,
            install_plugin_id=plugin_id_hint,
            before_install_hook=before_install_hook,
            skip_scan=skip_scan,
        )
    except Exception:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise

    # Record in installed-index
    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id=plugin_id_hint,
            version=version,
            source="git",
            source_url=url,
            source_ref=head_sha,
            tarball_sha256=None,
            installed_at=int(time.time()),
        ),
    )

    return InstallResult(
        plugin_id=plugin_id_hint, version=version, install_path=plugin_dir
    )


_TARBALL_GZIP_MAGIC = b"\x1f\x8b"


def install_from_url(
    url: str,
    *,
    dest_root: Path,
    plugin_id_hint: str,
    sha256: str | None,
    force: bool = False,
    before_install_hook: BeforeInstallHook | None = None,
    skip_scan: bool = False,
    http_get_bytes_fn=_http_get_bytes,
    max_bytes: int = MAX_TARBALL_BYTES,
) -> InstallResult:
    """Install a plugin from a raw https tarball URL.

    Requires an explicit ``sha256`` pin — refuses to install otherwise.
    Only ``.tar.gz`` / ``.tgz`` content is accepted (gzip magic bytes
    enforced; the URL extension is informational only).
    """
    if sha256 is None:
        raise TarballChecksumError(
            "https:// install requires --sha256 pin (refusing without checksum)"
        )

    plugin_dir = dest_root / plugin_id_hint
    if plugin_dir.exists():
        if not force:
            raise CatalogError(
                f"plugin '{plugin_id_hint}' already installed at {plugin_dir}. "
                "Use --force to overwrite."
            )
        shutil.rmtree(plugin_dir)

    raw = http_get_bytes_fn(url, max_bytes=max_bytes)

    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != sha256.lower():
        raise TarballChecksumError(
            f"sha256 mismatch: expected {sha256}, got {actual_sha}"
        )

    if not raw.startswith(_TARBALL_GZIP_MAGIC):
        raise UnsupportedTarballFormatError(
            f"only .tar.gz / .tgz tarballs are supported (url: {url})"
        )

    extract_tarball(raw, dest=plugin_dir)

    # Verify plugin.json id matches hint.
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.exists():
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(
            f"tarball at {url} has no plugin.json at the root"
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(
            f"plugin.json is not valid JSON: {e}"
        ) from e

    actual_id = str(manifest.get("id", ""))
    if actual_id != plugin_id_hint:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise PluginIdMismatchError(
            f"plugin.json says id={actual_id!r} but install argument was {plugin_id_hint!r}"
        )
    version = str(manifest.get("version", ""))

    try:
        _post_extract_gate(
            plugin_dir=plugin_dir,
            install_source="url",
            install_url=url,
            install_plugin_id=plugin_id_hint,
            before_install_hook=before_install_hook,
            skip_scan=skip_scan,
        )
    except Exception:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise

    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id=plugin_id_hint,
            version=version,
            source="url",
            source_url=url,
            source_ref=None,
            tarball_sha256=actual_sha,
            installed_at=int(time.time()),
        ),
    )

    return InstallResult(
        plugin_id=plugin_id_hint, version=version, install_path=plugin_dir
    )


class PypiNotFoundError(CatalogError):
    """Pip executable not found on PATH (or PEP 668 environment lockdown)."""


class PypiDownloadError(CatalogError):
    """Pip download failed (network, version not found, etc.)."""


def _pip_path() -> str | None:
    """Locate the pip binary. Mirrors :func:`_git_path` for testability.

    Prefers ``python -m pip`` over a bare ``pip`` to avoid the
    cross-Python-version PATH ambiguity Linux distros suffer from.
    Returns the python interpreter path; callers run
    ``[python, -m, pip, ...]``.
    """
    return shutil.which("python3") or shutil.which("python")


def install_from_pypi(
    package_spec: str,
    *,
    dest_root: Path,
    plugin_id_hint: str,
    force: bool = False,
    before_install_hook: BeforeInstallHook | None = None,
    skip_scan: bool = False,
    pip_download_fn: Callable[[str, Path], Path] | None = None,
    signature_url: str | None = None,
    signature_bytes: bytes | None = None,
    require_sigstore: bool = False,
    cert_identity: str | None = None,
    cert_oidc_issuer: str | None = None,
) -> InstallResult:
    """Install a plugin via ``pip download`` of its source dist.

    Strategy: ``pip download --no-deps --no-binary :all: <pkg>`` into a
    temp directory, then feed the resulting .tar.gz through the
    existing tarball-extract pipeline.  This keeps the install model
    self-contained (one plugin = one directory under ``dest_root``)
    rather than spilling into site-packages.

    Args:
        package_spec: PyPI spec, e.g. ``"opencomputer-plugin-foo"`` or
            ``"opencomputer-plugin-foo==1.2.3"``.
        dest_root: parent directory where the plugin folder lands.
        plugin_id_hint: must match ``plugin.json``'s ``id`` field
            inside the downloaded sdist.  Refused if mismatched.
        force: overwrite existing install.
        before_install_hook: same contract as the other installers.
        pip_download_fn: test injection point.  Returns the path to
            the downloaded sdist tarball.  Default invokes
            ``python3 -m pip download`` in a subprocess.

    Returns:
        :class:`InstallResult` with ``source="pypi"`` recorded in the
        installed-index for ``oc plugin verify``.

    Raises:
        :class:`PypiNotFoundError`: ``python3`` / ``pip`` unavailable.
        :class:`PypiDownloadError`: pip subprocess failed.
        :class:`UnsupportedTarballFormatError`: pip returned a wheel
            instead of an sdist (we explicitly disabled binaries).
        :class:`PluginIdMismatchError`: sdist's plugin.json id ≠ hint.
    """
    plugin_dir = dest_root / plugin_id_hint
    if plugin_dir.exists():
        if not force:
            raise CatalogError(
                f"plugin '{plugin_id_hint}' already installed at {plugin_dir}. "
                "Use --force to overwrite."
            )
        shutil.rmtree(plugin_dir)

    download_fn = pip_download_fn or _default_pip_download

    import tempfile

    with tempfile.TemporaryDirectory(prefix="oc-pypi-") as tmp:
        tmp_path = Path(tmp)
        try:
            sdist_path = download_fn(package_spec, tmp_path)
        except FileNotFoundError as e:
            raise PypiNotFoundError(
                f"pip / python3 not on PATH: {e}"
            ) from e
        except subprocess.CalledProcessError as e:
            raise PypiDownloadError(
                f"pip download failed: {e.stderr or e.stdout or e}"
            ) from e

        if not sdist_path.exists():
            raise PypiDownloadError(
                f"pip download claimed success but sdist file missing: "
                f"{sdist_path}"
            )

        raw = sdist_path.read_bytes()
        if not raw.startswith(_TARBALL_GZIP_MAGIC):
            raise UnsupportedTarballFormatError(
                f"pip returned {sdist_path.name!r} which is not a "
                "gzip sdist (wheels and binary distributions are not "
                "supported — pin the source dist)."
            )

        actual_sha = hashlib.sha256(raw).hexdigest()

        # Sigstore verification (M11.3 follow-up).  When operator passes
        # a signature URL/bytes (or sets OC_PLUGIN_REQUIRE_SIGSTORE=1),
        # run cosign verify-blob BEFORE extraction so a tampered sdist
        # never lands on disk.  ``verify_or_warn`` degrades gracefully
        # when require=False and cosign is absent / fails.
        sigstore_record: dict[str, str] | None = None
        if signature_bytes is not None or signature_url is not None:
            from opencomputer.plugins.sigstore_verify import (
                is_required_by_env,
                verify_or_warn,
            )

            sig_path = tmp_path / f"{sdist_path.name}.sig"
            if signature_bytes is not None:
                sig_path.write_bytes(signature_bytes)
            else:
                # Fetch the signature blob via the same HTTP guard as
                # tarball downloads (size-limited).
                sig_path.write_bytes(
                    _http_get_bytes(signature_url, max_bytes=64 * 1024)
                )
            require = require_sigstore or is_required_by_env()
            verification = verify_or_warn(
                sdist_path,
                signature_path=sig_path,
                require=require,
                cert_identity=cert_identity,
                cert_oidc_issuer=cert_oidc_issuer,
            )
            if verification is not None:
                sigstore_record = {
                    "cosign_version": verification.cosign_version,
                    "signature": (
                        signature_url or f"<inline:{len(signature_bytes or b'')} bytes>"
                    ),
                    "cert_identity": cert_identity or "",
                    "cert_oidc_issuer": cert_oidc_issuer or "",
                }

        # PyPI sdists have a top-level ``<name>-<version>/`` directory
        # wrapper (PEP 643).  Extract through it so plugin.json lands
        # at plugin_dir/plugin.json without the wrapper prefix.
        extract_tarball(raw, dest=plugin_dir, strip_top_level=True)

    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.exists():
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(
            f"PyPI sdist for {package_spec!r} has no plugin.json at "
            "the root after stripping the top-level wrapper."
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except json.JSONDecodeError as e:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise CatalogParseError(f"plugin.json is not valid JSON: {e}") from e

    actual_id = str(manifest.get("id", ""))
    if actual_id != plugin_id_hint:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise PluginIdMismatchError(
            f"plugin.json says id={actual_id!r} but install argument "
            f"was {plugin_id_hint!r}"
        )
    version = str(manifest.get("version", ""))

    try:
        _post_extract_gate(
            plugin_dir=plugin_dir,
            install_source="pypi",
            install_url=package_spec,
            install_plugin_id=plugin_id_hint,
            before_install_hook=before_install_hook,
            skip_scan=skip_scan,
        )
    except Exception:
        shutil.rmtree(plugin_dir, ignore_errors=True)
        raise

    from opencomputer.plugins.installed_index import (
        InstalledRecord,
        record_install,
    )

    record_install(
        dest_root / ".installed_index.json",
        InstalledRecord(
            plugin_id=plugin_id_hint,
            version=version,
            source="pypi",
            source_url=package_spec,
            source_ref=None,
            tarball_sha256=actual_sha,
            installed_at=int(time.time()),
        ),
    )

    # Persist sigstore verification side-car (out-of-band of
    # InstalledRecord schema so we don't bump the schema version for
    # an opt-in feature).  ``oc plugin verify`` reads this and re-runs
    # cosign with the same identity/issuer claims so tampering
    # post-install surfaces.
    if sigstore_record is not None:
        sigstore_dir = dest_root / ".sigstore"
        sigstore_dir.mkdir(parents=True, exist_ok=True)
        sidecar = sigstore_dir / f"{plugin_id_hint}.json"
        sidecar.write_text(
            json.dumps(
                {
                    "plugin_id": plugin_id_hint,
                    "version": version,
                    "source": "pypi",
                    "source_url": package_spec,
                    "tarball_sha256": actual_sha,
                    "verified_at": int(time.time()),
                    **sigstore_record,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    return InstallResult(
        plugin_id=plugin_id_hint, version=version, install_path=plugin_dir
    )


def _default_pip_download(package_spec: str, dest_dir: Path) -> Path:
    """Run ``python3 -m pip download`` to fetch the sdist.

    Returns the path to the downloaded sdist file.  Caller is
    responsible for cleanup of ``dest_dir``.
    """
    python = _pip_path()
    if python is None:
        raise FileNotFoundError(
            "neither python3 nor python found on PATH"
        )
    cmd = [
        python,
        "-m",
        "pip",
        "download",
        "--no-deps",
        "--no-binary",
        ":all:",
        "--dest",
        str(dest_dir),
        package_spec,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    # pip writes the sdist as <name>-<version>.tar.gz (or .zip — we
    # reject zip for consistency with other installers).
    candidates = sorted(dest_dir.glob("*.tar.gz"))
    if not candidates:
        zip_files = list(dest_dir.glob("*.zip"))
        if zip_files:
            raise UnsupportedTarballFormatError(
                f"pip returned a zip sdist ({zip_files[0].name}); only "
                ".tar.gz sdists are supported."
            )
        raise PypiDownloadError(
            f"pip download produced no .tar.gz file in {dest_dir}"
        )
    return candidates[0]


def _post_extract_gate(
    *,
    plugin_dir: Path,
    install_source: str,
    install_url: str,
    install_plugin_id: str,
    before_install_hook: BeforeInstallHook | None,
    skip_scan: bool,
) -> None:
    """Run security scan + fire BEFORE_INSTALL hook. Raise on veto/scan-block.

    Caller is responsible for rolling back ``plugin_dir`` on any exception.
    """
    from opencomputer.plugins.install_security_scan import scan_plugin_dir
    from plugin_sdk.hooks import HookContext, HookEvent

    report = None if skip_scan else scan_plugin_dir(plugin_dir)
    if report is not None:
        report.raise_for_blocks()  # raises InstallSecurityScanError on block

    if before_install_hook is None:
        return

    ctx = HookContext(
        event=HookEvent.BEFORE_INSTALL,
        session_id=f"install:{install_plugin_id}",
        install_source=install_source,
        install_url=install_url,
        install_plugin_id=install_plugin_id,
        install_scan_report=report,
    )
    # CLI install is a sync typer command running outside an event loop, so
    # asyncio.run() is the correct primitive. If a future caller invokes
    # install_from_catalog from inside an async context, they should pass
    # ``before_install_hook=None`` and call the hook themselves; we don't
    # paper over that with run_until_complete fallback (deprecated on 3.12+).
    decision = asyncio.run(before_install_hook(ctx))

    if decision is not None and getattr(decision, "decision", "pass") == "block":
        reason = getattr(decision, "reason", "") or "blocked by BEFORE_INSTALL hook"
        raise RuntimeError(reason)


__all__ = [
    "BeforeInstallHook",
    "CatalogEntry",
    "CatalogError",
    "CatalogFetchError",
    "CatalogNotConfiguredError",
    "CatalogParseError",
    "CatalogSignatureError",
    "GitCloneError",
    "GitNotFoundError",
    "InstallResult",
    "PluginIdMismatchError",
    "PluginNotInCatalogError",
    "TarballChecksumError",
    "TarballTooLargeError",
    "UnsupportedTarballFormatError",
    "MAX_TARBALL_BYTES",
    "DEFAULT_CACHE_TTL_SECONDS",
    "cache_path",
    "download_and_verify",
    "extract_tarball",
    "fetch_catalog",
    "find_entry",
    "install_from_catalog",
    "install_from_git",
    "install_from_pypi",
    "install_from_url",
    "PypiNotFoundError",
    "PypiDownloadError",
    "read_cache",
    "resolve_catalog_url",
    "write_cache",
]
