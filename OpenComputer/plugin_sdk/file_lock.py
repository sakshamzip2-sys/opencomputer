"""Cross-platform exclusive file lock for atomic JSON writes.

Hermes channel-port (PR 5 / amendment §B.4): JSON state files written
by adapters (sticker cache, DM-topic registry, future per-adapter
secret stores) need a process-level exclusive lock so two adapter
instances racing on the same profile don't shred each other's writes.

The lock is fail-open: when neither ``fcntl`` (POSIX) nor ``msvcrt``
(Windows) is importable, we log a WARNING and proceed without the
lock. Production paths (Linux/macOS bots, Windows desktop) are
covered; embedded / unusual platforms keep working with single-writer
semantics.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

logger = logging.getLogger("plugin_sdk.file_lock")


@contextmanager
def exclusive_lock(path: Path) -> Iterator[IO[str]]:
    """Acquire an exclusive lock for serialising writes to *path*.

    The lock is held on a sidecar file at ``<path>.lock`` — NOT on
    *path* itself. This matters because callers typically use the
    tmp+``Path.replace`` idiom to rewrite *path* atomically:

    .. code-block:: python

        with exclusive_lock(target_path):
            tmp = target_path.with_suffix(target_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(target_path)

    ``tmp.replace(target_path)`` rotates the inode at *path*. ``flock``
    is inode-bound, so a thread that opens *path* AFTER the replace
    locks a different inode than the still-locked previous one — and
    can therefore run concurrently with the in-progress writer. That
    race silently drops entries when a third writer reads disk
    *between* the second writer's ``read_text`` and ``tmp.replace``.

    Locking a sidecar ``.lock`` file whose inode is never replaced
    fixes this: every caller locks the same inode, so flock truly
    serialises them.

    Yields a text-mode handle to the *lock file* (rarely used by
    callers — typical usage is ``with exclusive_lock(path):`` with no
    ``as`` clause).
    """
    # Ensure the directory exists so ``open(..., "a+")`` can create the
    # file on first use. Best-effort: a missing parent on Windows
    # without permission would surface as a FileNotFoundError below.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    # Sidecar lock file path: ``foo.json`` -> ``foo.json.lock``. Using
    # ``with_name`` (append) rather than ``with_suffix`` (replace) so
    # the lock filename can't collide with the tmp file (``.json.tmp``).
    lock_path = path.with_name(path.name + ".lock")

    # The handle MUST outlive the ``with`` block so the lock survives
    # the caller's writes; a ``with open(...)`` would close it before
    # ``yield``. The try/finally below guarantees cleanup.
    fh: IO[str] = open(lock_path, "a+", encoding="utf-8")  # noqa: SIM115
    try:
        if sys.platform != "win32":
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError) as exc:
                logger.warning(
                    "file lock unavailable on %s (%s); proceeding without",
                    sys.platform, exc,
                )
        else:
            try:
                import msvcrt

                fh.seek(0)
                # ``LK_LOCK`` blocks up to 10 seconds then raises; we
                # treat that as fail-open to mirror the POSIX path.
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            except (ImportError, OSError) as exc:
                logger.warning(
                    "msvcrt.locking failed (%s); proceeding without",
                    exc,
                )
        yield fh
    finally:
        try:
            fh.close()
        except OSError:
            pass


__all__ = ["exclusive_lock"]
