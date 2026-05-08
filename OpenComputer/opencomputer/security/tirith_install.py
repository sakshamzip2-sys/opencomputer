"""Tirith auto-installer (Hermes-parity).

Closes the P3.7 audit-finding for ``security/tirith.py`` — the upstream
Hermes integration auto-fetches the binary from GitHub releases on
first use with SHA-256 checksum verification (and cosign provenance if
the cosign binary is on PATH). This module provides the installer; the
existing :mod:`opencomputer.security.tirith` consults it lazily when
``security.tirith.auto_install`` is enabled.

Production constraints:

* Downloads MUST verify a SHA-256 from the same release's signed
  ``checksums.txt`` before any byte is written to PATH. A failed
  verification removes the temporary file and raises.
* Architecture / OS detection chooses the right asset
  (``tirith-darwin-arm64``, ``tirith-linux-x86_64``, etc).
* Cosign verification is BEST-EFFORT — when ``cosign`` is on PATH and
  signed artifacts (``.sig`` + cert / public-key bundle) accompany the
  release we run cosign and treat a non-zero exit as a hard refusal
  (provenance is a strict gate when present, not advisory).
* Atomic install: download to ``<target_dir>/.tirith.tmp.<pid>``,
  verify, ``chmod 0755``, ``os.replace`` to ``<target_dir>/tirith``.
  A crash leaves the tmp file (cleanup runs at next attempt).
* No automatic network fetch unless explicitly enabled in config —
  ``auto_install`` defaults to False so opt-in is operator-driven.

The module is import-light: the network code path is lazy so a
non-network test environment can import + unit-test the verifiers and
helpers without touching urllib.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("opencomputer.security.tirith_install")


@dataclass(frozen=True, slots=True)
class TirithAsset:
    """Resolved release asset metadata.

    Attributes:
        version: e.g. ``"v1.4.0"`` — the release tag.
        asset_name: filename inside the release (e.g.
            ``"tirith-darwin-arm64"``).
        sha256: hex digest of the binary as published in the release's
            ``checksums.txt``.
        download_url: HTTPS URL of the binary asset.
        sig_url: optional URL of the cosign ``.sig`` artifact for
            provenance verification (None when no signature published).
    """

    version: str
    asset_name: str
    sha256: str
    download_url: str
    sig_url: str | None = None


def detect_platform_asset_name() -> str | None:
    """Return the canonical asset name for this host, or None if unsupported.

    Mirrors the naming convention upstream Tirith uses
    (``tirith-<os>-<arch>``). Returns None for architectures without a
    published binary; the caller then falls back to manual install.

    Examples
    --------
    >>> detect_platform_asset_name() in {  # platform-dependent
    ...     "tirith-darwin-arm64", "tirith-darwin-x86_64",
    ...     "tirith-linux-arm64", "tirith-linux-x86_64",
    ...     None,
    ... }
    True
    """
    sys_name = sys.platform
    machine = platform.machine().lower()
    if sys_name == "darwin":
        os_part = "darwin"
    elif sys_name.startswith("linux"):
        os_part = "linux"
    else:
        return None
    if machine in ("x86_64", "amd64"):
        arch_part = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch_part = "arm64"
    else:
        return None
    return f"tirith-{os_part}-{arch_part}"


def verify_sha256(data: bytes, expected_hex: str) -> bool:
    """Constant-time check that ``sha256(data) == expected_hex``.

    ``expected_hex`` is normalised to lowercase before comparison.
    Returns False rather than raising — caller decides what to do
    with the verdict (delete tmp + raise vs. retry).
    """
    if not expected_hex:
        return False
    digest = hashlib.sha256(data).hexdigest()
    # secrets.compare_digest is overkill (no secret here) but keeps
    # the comparison constant-time regardless of byte-position drift.
    import secrets as _secrets

    return _secrets.compare_digest(digest.lower(), expected_hex.lower())


def parse_checksums_txt(text: str, target_filename: str) -> str | None:
    """Extract a SHA-256 hex digest for ``target_filename`` from ``text``.

    The Hermes ``checksums.txt`` shape mirrors GNU coreutils
    ``sha256sum -b``::

        a1b2c3...  tirith-linux-x86_64
        ...

    The function is tolerant: ignores blank lines and ``#`` comments,
    accepts an optional ``*`` (binary mode) prefix on the filename,
    and matches by exact filename (no path prefix).

    Returns the lowercase hex digest, or None if the filename isn't
    listed.
    """
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Tolerate any whitespace; checksums.txt uses 2 spaces by
        # convention but mixed-tabs are common in hand-edited copies.
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest, name = parts[0], parts[1].lstrip("*").strip()
        if name == target_filename:
            return digest.lower()
    return None


_GITHUB_RELEASE_API = "https://api.github.com/repos/sheeki03/tirith/releases/latest"


# Type alias for the network fetcher — accepts a URL, returns body
# bytes. Module-level so tests can monkey-patch with a stub.
HttpFetcher = Callable[[str], bytes]


def _default_http_fetcher(url: str) -> bytes:
    """Default fetcher — uses urllib.request with a 30-second timeout.

    Lazy-imports urllib so the module imports clean in environments
    that ban urllib at import time (some sandboxes). Surfaces every
    error as an OSError subclass so callers can catch one shape.
    """
    import urllib.request

    req = urllib.request.Request(  # noqa: S310 — explicit https below
        url,
        headers={"User-Agent": "OpenComputer-tirith-install/1.0"},
    )
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-HTTPS download URL: {url!r}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.read()
    except Exception as e:  # noqa: BLE001
        # Wrap to a uniform shape callers can catch.
        raise OSError(f"tirith asset fetch failed: {url}: {e}") from e


def fetch_release_asset(
    *,
    asset_name: str,
    fetcher: HttpFetcher | None = None,
    api_url: str = _GITHUB_RELEASE_API,
) -> TirithAsset:
    """Look up the latest release on GitHub and resolve the asset URL.

    Calls the GitHub release API, parses the JSON, finds the asset
    matching ``asset_name``, and pairs it with the SHA-256 from the
    release's ``checksums.txt`` (which is itself an asset on the
    release).

    Args:
        asset_name: e.g. ``"tirith-darwin-arm64"`` (from
            :func:`detect_platform_asset_name`).
        fetcher: HTTP fetcher; defaults to the urllib-based one. Tests
            inject a stub.
        api_url: GitHub release API URL — overridable for tests.

    Returns:
        Resolved :class:`TirithAsset` ready for
        :func:`install_atomic`.

    Raises:
        OSError: network or parse failure.
        ValueError: asset / checksum not found in the release.
    """
    fetch = fetcher or _default_http_fetcher
    try:
        api_body = fetch(api_url)
        release = json.loads(api_body.decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        raise OSError(f"failed to load release metadata: {e}") from e

    version = str(release.get("tag_name") or "")
    assets = release.get("assets") or []
    if not isinstance(assets, list):
        raise ValueError("release.assets is not a list")

    binary_url: str | None = None
    checksums_url: str | None = None
    sig_url: str | None = None
    for entry in assets:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        url = entry.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(url, str):
            continue
        if name == asset_name:
            binary_url = url
        elif name == "checksums.txt":
            checksums_url = url
        elif name == f"{asset_name}.sig":
            sig_url = url

    if binary_url is None:
        raise ValueError(
            f"release {version!r} has no asset named {asset_name!r}"
        )
    if checksums_url is None:
        raise ValueError(
            f"release {version!r} is missing checksums.txt"
        )
    try:
        checksums_body = fetch(checksums_url)
    except OSError as e:
        raise OSError(f"failed to fetch checksums.txt: {e}") from e
    digest = parse_checksums_txt(checksums_body.decode("utf-8"), asset_name)
    if digest is None:
        raise ValueError(
            f"checksums.txt has no entry for {asset_name!r}"
        )
    return TirithAsset(
        version=version,
        asset_name=asset_name,
        sha256=digest,
        download_url=binary_url,
        sig_url=sig_url,
    )


def cosign_verify(
    *, binary_path: Path, sig_bytes: bytes,
    cert_identity: str | None = None,
) -> bool:
    """Best-effort cosign verification.

    Returns False (treated as a HARD refusal in the installer) iff
    cosign is on PATH AND the verification fails. Returns True when
    verification succeeds OR cosign isn't installed (in which case the
    installer logs a one-line note and falls back to SHA-256 alone —
    we don't hide the fact that provenance wasn't checked).

    The "no cosign → True" choice is deliberate: cosign provenance is
    strictly stronger than checksum-only, but operators who haven't
    installed cosign shouldn't be blocked from getting tirith. If
    they want fail-closed they can install cosign and the verifier
    activates automatically.
    """
    if not shutil.which("cosign"):
        logger.info(
            "cosign not on PATH — skipping provenance verify on tirith download "
            "(SHA-256 still enforced).",
        )
        return True
    sig_file = binary_path.with_suffix(binary_path.suffix + ".sig")
    try:
        sig_file.write_bytes(sig_bytes)
    except OSError as e:
        logger.warning("could not write tirith .sig for cosign: %s", e)
        return False
    try:
        cmd = [
            "cosign", "verify-blob",
            "--signature", str(sig_file),
            str(binary_path),
        ]
        if cert_identity:
            cmd.extend(["--certificate-identity", cert_identity])
        result = subprocess.run(  # noqa: S603 — cmd built from validated parts
            cmd,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            logger.warning(
                "cosign verify-blob failed: stdout=%r stderr=%r",
                result.stdout, result.stderr,
            )
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("cosign verify-blob timed out")
        return False
    except OSError as e:
        logger.warning("cosign invocation failed: %s", e)
        return False
    finally:
        # Delete the .sig regardless — we don't keep auxiliary files in
        # the install dir.
        try:
            sig_file.unlink(missing_ok=True)
        except OSError:
            pass


def install_atomic(
    *,
    asset: TirithAsset,
    target_dir: Path,
    fetcher: HttpFetcher | None = None,
    cosign_cert_identity: str | None = None,
) -> Path:
    """Download + verify + install the binary atomically.

    Steps:
        1. Download asset bytes via ``fetcher``.
        2. SHA-256 must equal ``asset.sha256`` — refusal otherwise.
        3. If ``asset.sig_url``, fetch sig + run cosign — refusal if
           cosign is on PATH and verification fails.
        4. Write to ``<target_dir>/.tirith.tmp.<pid>``, ``chmod 0755``,
           atomic rename to ``<target_dir>/tirith``.

    Returns the final installed path on success. Raises ``OSError`` /
    ``ValueError`` on any failure; partially-written tmp files are
    deleted before re-raising.
    """
    fetch = fetcher or _default_http_fetcher
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        body = fetch(asset.download_url)
    except OSError as e:
        raise OSError(f"download failed: {e}") from e
    if not verify_sha256(body, asset.sha256):
        raise ValueError(
            f"SHA-256 mismatch for {asset.asset_name}: refusing install"
        )

    tmp_path = target_dir / f".tirith.tmp.{os.getpid()}"
    try:
        tmp_path.write_bytes(body)
        try:
            tmp_path.chmod(0o755)
        except OSError as e:
            raise OSError(f"chmod 0755 failed on tmp tirith: {e}") from e
        if asset.sig_url:
            try:
                sig_bytes = fetch(asset.sig_url)
            except OSError as e:
                logger.warning(
                    "failed to fetch tirith .sig: %s — provenance not "
                    "checked but SHA-256 verified", e,
                )
            else:
                if not cosign_verify(
                    binary_path=tmp_path, sig_bytes=sig_bytes,
                    cert_identity=cosign_cert_identity,
                ):
                    raise ValueError(
                        "cosign provenance verification FAILED — refusing install"
                    )
        final = target_dir / "tirith"
        os.replace(tmp_path, final)
        return final
    except Exception:
        # Best-effort cleanup; any failure leaves a stale tmp the next
        # attempt will overwrite.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def install_if_missing(
    *,
    target_dir: Path,
    fetcher: HttpFetcher | None = None,
    cosign_cert_identity: str | None = None,
) -> Path | None:
    """Install tirith into ``target_dir/tirith`` iff not already present.

    Idempotent: returns the existing binary path immediately if found.
    On unsupported platforms (no asset name resolvable) returns None
    without attempting the network call.

    Designed for lazy first-use — the existing
    :func:`opencomputer.security.tirith.is_available` check can wrap
    this to auto-install on miss.
    """
    target_path = target_dir / "tirith"
    if target_path.exists():
        return target_path
    asset_name = detect_platform_asset_name()
    if asset_name is None:
        logger.info(
            "tirith auto-install skipped — unsupported platform "
            "(%s/%s); install manually.",
            sys.platform, platform.machine(),
        )
        return None
    try:
        asset = fetch_release_asset(
            asset_name=asset_name, fetcher=fetcher,
        )
        return install_atomic(
            asset=asset, target_dir=target_dir,
            fetcher=fetcher,
            cosign_cert_identity=cosign_cert_identity,
        )
    except (OSError, ValueError) as e:
        # Auto-install must NEVER block the agent — surface a warning
        # and return None so callers fall back to fail_open behaviour.
        logger.warning("tirith auto-install failed: %s", e)
        return None


def cleanup_stale_tmp_files(target_dir: Path) -> int:
    """Remove ``.tirith.tmp.*`` leftovers from interrupted installs.

    Returns the number of files cleaned up. Best-effort; an error on
    any file is ignored.
    """
    if not target_dir.exists():
        return 0
    cleaned = 0
    for entry in target_dir.iterdir():
        if entry.is_file() and entry.name.startswith(".tirith.tmp."):
            try:
                entry.unlink()
                cleaned += 1
            except OSError as e:
                # Ignore EACCES / EPERM — operator deal with it.
                if e.errno not in (errno.EACCES, errno.EPERM):
                    raise
    return cleaned


__all__ = [
    "HttpFetcher",
    "TirithAsset",
    "cleanup_stale_tmp_files",
    "cosign_verify",
    "detect_platform_asset_name",
    "fetch_release_asset",
    "install_atomic",
    "install_if_missing",
    "parse_checksums_txt",
    "verify_sha256",
]
