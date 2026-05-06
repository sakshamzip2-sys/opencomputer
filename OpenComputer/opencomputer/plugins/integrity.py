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


__all__ = [
    "DriftReport",
    "FileDifference",
    "IntegrityError",
    "NotInstalledError",
    "SourceUnreachableError",
    "verify_plugin",
]
