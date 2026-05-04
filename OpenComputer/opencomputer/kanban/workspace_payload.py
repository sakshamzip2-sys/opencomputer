"""Cross-host workspace payload pack/unpack (Wave 6.E.15).

Closes the documented limitation in PR #460: ``dir:<path>`` workspaces
across hosts. Operator-zero solution — both sides exchange a gzipped
tarball as part of the spawn (sender → peer) and callback (peer →
sender) requests.

Two helpers:

- :func:`pack_workspace` — gzip+tar a directory; raise on cap excess.
- :func:`unpack_workspace` — safe-extract via ``tarfile.extractall(filter='data')``
  to reject CVE-2007-4559 / absolute-path / symlink-escape attacks.

Both sides cap payload at 50 MiB. Operator-side load balancers
typically default to 100 MiB request bodies, so we stay safely under.

Atomic directory replacement on the receiving side (sender callback
path): we extract to ``<dir>.new``, rename old to ``<dir>.old``,
rename new to ``<dir>``, then delete old. Failure mid-flight rolls
back to the old contents.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tarfile
from pathlib import Path

logger = logging.getLogger("opencomputer.kanban.workspace_payload")

# 50 MiB. Documented in the design spec; operator can override via
# ``OC_KANBAN_WORKSPACE_MAX_BYTES`` if they have a bigger pipe.
DEFAULT_MAX_BYTES = 50 * 1024 * 1024


class WorkspacePayloadError(RuntimeError):
    """Raised when packing or unpacking fails (size cap, traversal, etc.)."""


def _max_bytes_env() -> int:
    """Read the size cap from env, falling back to default. Validation:
    must parse as positive int; otherwise default."""
    raw = os.environ.get("OC_KANBAN_WORKSPACE_MAX_BYTES", "")
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_MAX_BYTES
    except (ValueError, TypeError):
        return DEFAULT_MAX_BYTES


def pack_workspace(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> bytes:
    """Gzip + tar the directory at ``path``. Returns raw bytes.

    Raises :class:`WorkspacePayloadError` if:
    - Path doesn't exist or isn't a directory
    - Resulting payload exceeds ``max_bytes``

    Implementation note: we tar with relative paths anchored at
    ``path.name`` so the receiver knows what folder name to extract
    into. Members keep mode/mtime; we strip uid/gid/uname/gname so
    payloads are reproducible across hosts (no leaking local
    user identity).
    """
    cap = max_bytes if max_bytes is not None else _max_bytes_env()
    if not path.exists():
        raise WorkspacePayloadError(f"workspace path does not exist: {path}")
    if not path.is_dir():
        raise WorkspacePayloadError(f"workspace is not a directory: {path}")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # arcname is the top-level directory name in the tar so the
        # peer can extract under any chosen parent.
        def _filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = ""
            tarinfo.gname = ""
            return tarinfo

        tar.add(str(path), arcname=path.name, filter=_filter)
    data = buf.getvalue()
    if len(data) > cap:
        raise WorkspacePayloadError(
            f"workspace payload {len(data):,} bytes > cap {cap:,} "
            f"(set OC_KANBAN_WORKSPACE_MAX_BYTES to override)"
        )
    return data


def unpack_workspace(
    data: bytes,
    *,
    dest: Path,
    max_bytes: int | None = None,
) -> Path:
    """Extract ``data`` (gzipped tar) under ``dest``. Returns the
    directory path actually written.

    Safe-extraction via ``tarfile.extractall(filter='data')`` rejects:
    - Absolute paths in member names (CVE-2007-4559)
    - Symlinks pointing outside the destination
    - Device / FIFO members

    Caller is responsible for choosing ``dest`` carefully —
    extraction creates ``dest/<top-level-name-from-archive>/...``.
    """
    cap = max_bytes if max_bytes is not None else _max_bytes_env()
    if len(data) > cap:
        raise WorkspacePayloadError(
            f"payload {len(data):,} bytes > cap {cap:,}"
        )
    dest.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO(data)
    try:
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            # Python 3.12+ — strict 'data' filter rejects unsafe members.
            tar.extractall(path=str(dest), filter="data")
    except tarfile.TarError as exc:
        raise WorkspacePayloadError(f"tarfile extract failed: {exc}") from exc
    # The archive's top-level dir name should be the only direct child
    # of dest after extraction. Return its path.
    children = [c for c in dest.iterdir() if c.is_dir()]
    if len(children) == 1:
        return children[0]
    if len(children) == 0:
        raise WorkspacePayloadError("payload contained no top-level directory")
    # More than one top-level dir means the sender packed something
    # unusual; return dest and let the caller decide.
    return dest


def replace_workspace_atomic(target: Path, replacement: Path) -> None:
    """Replace ``target`` with the contents of ``replacement`` atomically.

    Uses a 3-rename strategy:
    1. ``target`` → ``<target>.old`` (back up current contents)
    2. ``replacement`` → ``target`` (install new)
    3. delete ``<target>.old`` (commit)

    On failure between steps 1 and 2, restore from ``<target>.old``.

    Both ``target`` and ``replacement`` must exist and be directories.
    """
    target = Path(target)
    replacement = Path(replacement)
    if not target.exists():
        # No old contents to preserve; just rename in.
        replacement.rename(target)
        return
    if not target.is_dir():
        raise WorkspacePayloadError(f"target is not a directory: {target}")
    if not replacement.is_dir():
        raise WorkspacePayloadError(f"replacement is not a directory: {replacement}")
    backup = target.with_name(target.name + ".old")
    if backup.exists():
        # Stale backup from a prior crash — clean it up
        shutil.rmtree(backup)
    target.rename(backup)
    try:
        replacement.rename(target)
    except OSError:
        # Roll back
        backup.rename(target)
        raise
    # Commit: delete the backup
    shutil.rmtree(backup, ignore_errors=True)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "WorkspacePayloadError",
    "pack_workspace",
    "unpack_workspace",
    "replace_workspace_atomic",
]
