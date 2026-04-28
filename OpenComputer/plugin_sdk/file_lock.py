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
    """Open *path* for exclusive read+write with platform-specific lock.

    Yields a text-mode file handle positioned at the start of the file
    (the caller is responsible for ``seek`` / ``truncate`` semantics
    around the actual write). On exit the handle is closed and the
    lock is released by the OS.

    Use the tmp-file + ``Path.replace`` pattern for the actual write
    so an interrupted write (SIGKILL between write and replace) leaves
    the file intact:

    .. code-block:: python

        from plugin_sdk.file_lock import exclusive_lock

        with exclusive_lock(target_path):
            tmp = target_path.with_suffix(target_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(target_path)
    """
    # Ensure the directory exists so ``open(..., "a+")`` can create the
    # file on first use. Best-effort: a missing parent on Windows
    # without permission would surface as a FileNotFoundError below.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    fh: IO[str] = open(path, "a+", encoding="utf-8")
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
