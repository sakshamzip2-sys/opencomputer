"""Advisory file lock around profile.yaml read-modify-write.

Several callers (cli_plugin.py, setup_wizard.py via cli_plugin) read
profile.yaml, mutate it, and write back via atomic-replace. Atomic-
replace is crash-safe but doesn't serialize concurrent reads; two
parallel `oc plugin enable X` and `oc plugin enable Y` from sibling
shells would each see the same baseline and one entry would be lost.

This context manager wraps the read-modify-write in an exclusive flock
on `<profile_dir>/.profile.lock`. Blocking — concurrent writers
serialize cleanly. Lock file is separate from profile.yaml because the
yaml file gets atomic-replaced, which would invalidate any flock held
on the original inode.

Mirrors the pattern from ``opencomputer/cron/scheduler.py``
(``_acquire_tick_lock``) but uses ``LOCK_EX`` without ``LOCK_NB`` so
we serialize instead of failing-fast.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@contextmanager
def profile_yaml_lock(profile_dir: Path) -> Iterator[None]:
    """Acquire an exclusive blocking lock around profile.yaml mutation.

    Usage::

        with profile_yaml_lock(profile_dir):
            data = yaml.safe_load(path.read_text())
            data["plugins"]["enabled"].append(plugin_id)
            _atomic_write_yaml(path, data)

    The lock file (``.profile.lock``) is created in ``profile_dir`` if
    absent. Released automatically on exit (or exception). When fcntl
    AND msvcrt are both unavailable the lock is a no-op (rare,
    documented).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    lock_path = profile_dir / ".profile.lock"
    fd = open(lock_path, "w")  # noqa: SIM115 - released in finally
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            try:
                msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as exc:
                logger.warning("profile.yaml lock acquisition failed: %s", exc)
        # else: no locking primitive — operate as no-op
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
        elif msvcrt is not None:  # pragma: no cover
            try:
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        fd.close()
