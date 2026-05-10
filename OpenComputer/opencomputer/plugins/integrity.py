"""Re-fetch + bytes-compare drift detection for installed plugins.

Used by ``oc plugin verify <plugin_id>`` to confirm that the installed
files still match the source they came from.

For ``catalog`` and ``url`` installs we re-download the tarball and
compare its sha256 to the recorded ``tarball_sha256``. If those match
but on-disk files don't match the tarball contents, we report it as
on-disk drift (someone hand-edited an installed plugin).

For ``git`` installs we re-clone the recorded ref and diff trees.
For ``path`` installs (future) drift detection is not meaningful.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from opencomputer.plugins.installed_index import (
    InstalledRecord,
    find_record,
)


class IntegrityError(Exception):
    """Base class for integrity check failures."""


class NotInstalledError(IntegrityError):
    """No installed-index entry for the given plugin_id."""


class SourceUnreachableError(IntegrityError):
    """Re-fetch raised — install source is no longer reachable."""


@dataclass(frozen=True)
class FileDifference:
    path: str
    kind: str  # "missing" | "extra" | "modified"


@dataclass(frozen=True)
class DriftReport:
    plugin_id: str
    source: str
    source_url: str
    has_drift: bool
    differences: list[FileDifference] = field(default_factory=list)


def _refetch_default(rec: InstalledRecord) -> bytes:
    """Default re-fetcher for the `url` source only.

    Catalog re-fetch is more complex (slug → catalog → tarball_url) so the
    CLI passes its own ``refetch_fn`` when the source is ``catalog``.
    Git re-fetch is handled inside ``_verify_via_git`` — never reaches here.
    """
    if rec.source == "url":
        from opencomputer.plugins.remote_install import _http_get_bytes

        return _http_get_bytes(rec.source_url, max_bytes=50 * 1024 * 1024)

    raise SourceUnreachableError(
        f"default refetch_fn doesn't handle source={rec.source!r}; "
        "the CLI should pass a source-specific refetch_fn for catalog installs."
    )


def verify_plugin(
    plugin_id: str,
    *,
    dest_root: Path,
    refetch_fn: Callable[[InstalledRecord], bytes] = _refetch_default,
) -> DriftReport:
    """Re-fetch the source bytes and compare against on-disk plugin tree.

    Returns a DriftReport even when has_drift=False so the CLI can print
    a uniform summary.
    """
    rec = find_record(dest_root / ".installed_index.json", plugin_id)
    if rec is None:
        raise NotInstalledError(f"no installed-index entry for {plugin_id!r}")

    plugin_dir = dest_root / plugin_id
    if not plugin_dir.exists():
        # Index says installed but dir is missing — treat as drift.
        return DriftReport(
            plugin_id=plugin_id,
            source=rec.source,
            source_url=rec.source_url,
            has_drift=True,
            differences=[FileDifference(path="<plugin-dir>", kind="missing")],
        )

    if rec.source in ("catalog", "url"):
        return _verify_via_tarball(
            rec=rec, plugin_dir=plugin_dir, refetch_fn=refetch_fn
        )
    if rec.source == "git":
        return _verify_via_git(rec=rec, plugin_dir=plugin_dir)
    # Unknown source — best-effort: no drift to report.
    return DriftReport(
        plugin_id=plugin_id,
        source=rec.source,
        source_url=rec.source_url,
        has_drift=False,
    )


def _verify_via_tarball(
    *,
    rec: InstalledRecord,
    plugin_dir: Path,
    refetch_fn: Callable[[InstalledRecord], bytes],
) -> DriftReport:
    try:
        raw = refetch_fn(rec)
    except SourceUnreachableError:
        raise
    except Exception as e:
        raise SourceUnreachableError(
            f"could not re-fetch {rec.source_url!r}: {e}"
        ) from e

    if rec.tarball_sha256 is not None:
        actual = hashlib.sha256(raw).hexdigest()
        if actual != rec.tarball_sha256:
            return DriftReport(
                plugin_id=rec.plugin_id,
                source=rec.source,
                source_url=rec.source_url,
                has_drift=True,
                differences=[
                    FileDifference(
                        path=(
                            f"<source-tarball sha256 differs: "
                            f"recorded={rec.tarball_sha256[:12]}.. "
                            f"fetched={actual[:12]}..>"
                        ),
                        kind="modified",
                    )
                ],
            )

    differences: list[FileDifference] = []
    on_disk: dict[str, bytes] = {}
    for f in plugin_dir.rglob("*"):
        if f.is_file():
            on_disk[str(f.relative_to(plugin_dir))] = f.read_bytes()

    in_tarball: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            f = tar.extractfile(member)
            if f is None:
                continue
            in_tarball[member.name] = f.read()

    for path, body in in_tarball.items():
        if path not in on_disk:
            differences.append(FileDifference(path=path, kind="missing"))
        elif on_disk[path] != body:
            differences.append(FileDifference(path=path, kind="modified"))

    for path in on_disk:
        if path not in in_tarball:
            differences.append(FileDifference(path=path, kind="extra"))

    return DriftReport(
        plugin_id=rec.plugin_id,
        source=rec.source,
        source_url=rec.source_url,
        has_drift=bool(differences),
        differences=differences,
    )


def _verify_via_git(
    *, rec: InstalledRecord, plugin_dir: Path
) -> DriftReport:
    """Reclone + diff. Skipped if git binary is missing."""
    import shutil
    import subprocess
    import tempfile

    git = shutil.which("git")
    if git is None:
        return DriftReport(
            plugin_id=rec.plugin_id,
            source=rec.source,
            source_url=rec.source_url,
            has_drift=False,
            differences=[
                FileDifference(
                    path="<git binary not found — skipping verify>",
                    kind="modified",
                )
            ],
        )

    with tempfile.TemporaryDirectory(prefix="oc-verify-") as td:
        clone_dir = Path(td) / "clone"
        try:
            subprocess.run(
                [
                    git,
                    "clone",
                    "--quiet",
                    rec.source_url.removeprefix("git+"),
                    str(clone_dir),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            if rec.source_ref:
                subprocess.run(
                    [git, "checkout", "--quiet", rec.source_ref],
                    cwd=clone_dir,
                    check=True,
                    capture_output=True,
                    text=True,
                )
        except subprocess.CalledProcessError as e:
            raise SourceUnreachableError(
                f"git re-clone of {rec.source_url} failed: {e.stderr.strip()}"
            ) from e

        # Strip .git from clone before comparing.
        clone_git = clone_dir / ".git"
        if clone_git.exists():
            shutil.rmtree(clone_git)

        clone_files: dict[str, bytes] = {}
        for f in clone_dir.rglob("*"):
            if f.is_file():
                clone_files[str(f.relative_to(clone_dir))] = f.read_bytes()
        on_disk: dict[str, bytes] = {}
        for f in plugin_dir.rglob("*"):
            if f.is_file():
                on_disk[str(f.relative_to(plugin_dir))] = f.read_bytes()

        differences: list[FileDifference] = []
        for path, body in clone_files.items():
            if path not in on_disk:
                differences.append(FileDifference(path=path, kind="missing"))
            elif on_disk[path] != body:
                differences.append(FileDifference(path=path, kind="modified"))
        for path in on_disk:
            if path not in clone_files:
                differences.append(FileDifference(path=path, kind="extra"))

        return DriftReport(
            plugin_id=rec.plugin_id,
            source=rec.source,
            source_url=rec.source_url,
            has_drift=bool(differences),
            differences=differences,
        )


@dataclass(frozen=True)
class SigstoreVerifyReport:
    """Outcome of re-verifying an install's recorded sigstore signature.

    Returned by :func:`verify_plugin_signature`.  ``has_signature``
    is False when no sidecar was recorded at install time (pre-M11.3
    install or operator never opted into sigstore).
    """

    plugin_id: str
    has_signature: bool
    verified: bool = False
    cosign_version: str = ""
    cert_identity: str = ""
    cert_oidc_issuer: str = ""
    error: str = ""


def verify_plugin_signature(
    plugin_id: str,
    *,
    dest_root: Path,
    refetch_artifact_fn: Callable[[InstalledRecord], bytes] = _refetch_default,
) -> SigstoreVerifyReport:
    """Re-verify the recorded sigstore signature for a plugin.

    Reads the sidecar at ``<dest_root>/.sigstore/<plugin_id>.json``.
    When present, re-fetches the source artifact and runs cosign
    against the recorded signature URL + identity/issuer claims.

    Returns a :class:`SigstoreVerifyReport` with ``has_signature=False``
    when no sidecar exists; the caller decides whether that's a hard
    failure (operator policy) or a warning.
    """
    import json

    sidecar = dest_root / ".sigstore" / f"{plugin_id}.json"
    if not sidecar.exists():
        return SigstoreVerifyReport(
            plugin_id=plugin_id, has_signature=False
        )

    try:
        record = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return SigstoreVerifyReport(
            plugin_id=plugin_id,
            has_signature=True,
            verified=False,
            error=f"sidecar malformed: {exc}",
        )

    rec = find_record(dest_root / ".installed_index.json", plugin_id)
    if rec is None:
        return SigstoreVerifyReport(
            plugin_id=plugin_id,
            has_signature=True,
            verified=False,
            error="installed-index entry missing; can't refetch artifact",
        )

    signature_value = record.get("signature", "")
    if signature_value.startswith("<inline:"):
        return SigstoreVerifyReport(
            plugin_id=plugin_id,
            has_signature=True,
            verified=False,
            error=(
                "signature was provided inline at install time; the "
                "raw bytes are not retained for re-verification."
            ),
        )

    # Re-fetch artifact; verify cosign against recorded signature URL.
    try:
        raw = refetch_artifact_fn(rec)
    except SourceUnreachableError as exc:
        return SigstoreVerifyReport(
            plugin_id=plugin_id,
            has_signature=True,
            verified=False,
            error=f"refetch failed: {exc}",
        )

    import tempfile

    from opencomputer.plugins.sigstore_verify import (
        SigstoreUnavailableError,
        SigstoreVerificationFailedError,
        verify_blob,
    )

    with tempfile.TemporaryDirectory(prefix="oc-verify-sig-") as tmp:
        tmp_path = Path(tmp)
        artifact_path = tmp_path / "artifact"
        artifact_path.write_bytes(raw)
        sig_path = tmp_path / "artifact.sig"
        # Refetch the signature blob — same URL the install recorded.
        from opencomputer.plugins.remote_install import _http_get_bytes

        try:
            sig_path.write_bytes(
                _http_get_bytes(signature_value, max_bytes=64 * 1024)
            )
        except Exception as exc:  # noqa: BLE001
            return SigstoreVerifyReport(
                plugin_id=plugin_id,
                has_signature=True,
                verified=False,
                error=f"signature refetch failed: {exc}",
            )

        try:
            v = verify_blob(
                artifact_path,
                signature_path=sig_path,
                cert_identity=record.get("cert_identity") or None,
                cert_oidc_issuer=record.get("cert_oidc_issuer") or None,
            )
        except SigstoreUnavailableError as exc:
            return SigstoreVerifyReport(
                plugin_id=plugin_id,
                has_signature=True,
                verified=False,
                error=str(exc),
            )
        except SigstoreVerificationFailedError as exc:
            return SigstoreVerifyReport(
                plugin_id=plugin_id,
                has_signature=True,
                verified=False,
                error=str(exc),
            )

    return SigstoreVerifyReport(
        plugin_id=plugin_id,
        has_signature=True,
        verified=True,
        cosign_version=v.cosign_version,
        cert_identity=record.get("cert_identity", ""),
        cert_oidc_issuer=record.get("cert_oidc_issuer", ""),
    )


__all__ = [
    "DriftReport",
    "FileDifference",
    "IntegrityError",
    "NotInstalledError",
    "SigstoreVerifyReport",
    "SourceUnreachableError",
    "verify_plugin",
    "verify_plugin_signature",
]
