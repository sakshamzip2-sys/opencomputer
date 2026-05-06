"""Regression tests for the OutgoingQueue file-descriptor leak.

Production bug — when the gateway runs continuously, the outgoing
drainer's tight 1-second poll loop calls ``list_queued`` repeatedly.
Every call would open a fresh sqlite3 Connection without closing it
(``with self._connect() as conn:`` is a transaction context manager,
not a close-on-exit), eventually exhausting the per-process FD limit
and surfacing as ``sqlite3.OperationalError: unable to open database
file`` — at which point telegram outbound delivery stops cold and the
user sees "the bot doesn't reply".

This test exercises the call paths the drainer actually uses
(``list_queued``, ``get``, ``list_``) at high frequency and asserts
that the open-file-descriptor count for the database doesn't grow.

Test must FAIL on the bug (with self._connect() as conn) and PASS on
the fix (contextlib.closing(self._connect())).
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest

from opencomputer.gateway.outgoing_queue import OutgoingQueue


def _open_fd_count_for_path(pid: int, path_substring: str) -> int:
    """Count file descriptors in /proc/<pid>/fd (Linux) or via lsof (macOS)
    that point at any file containing ``path_substring``."""
    proc_fd = Path(f"/proc/{pid}/fd")
    if proc_fd.is_dir():
        n = 0
        for fd in proc_fd.iterdir():
            try:
                target = os.readlink(fd)
                if path_substring in target:
                    n += 1
            except OSError:
                pass
        return n
    # macOS — fall back to lsof.
    try:
        out = subprocess.check_output(
            ["lsof", "-p", str(pid)], stderr=subprocess.DEVNULL, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("neither /proc nor lsof available — skipping FD count test")
    return sum(1 for line in out.splitlines() if path_substring in line)


def test_list_queued_does_not_leak_file_descriptors(tmp_path: Path):
    """Calling list_queued() many times must not grow the FD count."""
    db = tmp_path / "outgoing.db"
    queue = OutgoingQueue(db)

    pid = os.getpid()
    baseline = _open_fd_count_for_path(pid, str(db))

    for _ in range(50):
        queue.list_queued(limit=4)

    after = _open_fd_count_for_path(pid, str(db))
    growth = after - baseline
    assert growth <= 1, (
        f"FD leak detected: list_queued() x50 grew open-FD count by "
        f"{growth} (baseline={baseline}, after={after}). The drainer's "
        "poll loop will exhaust the per-process FD limit within minutes "
        "and break telegram outbound delivery."
    )


def test_mixed_reads_do_not_leak_file_descriptors(tmp_path: Path):
    """Calling list_queued / get / list_ in a tight loop must not leak."""
    db = tmp_path / "outgoing.db"
    queue = OutgoingQueue(db)
    msg = queue.enqueue(platform="telegram", chat_id="1", body="hi")

    pid = os.getpid()
    baseline = _open_fd_count_for_path(pid, str(db))

    for _ in range(30):
        queue.list_queued(limit=4)
        queue.get(msg.id)
        queue.list_(limit=4)

    after = _open_fd_count_for_path(pid, str(db))
    growth = after - baseline
    assert growth <= 1, (
        f"Mixed-read FD leak: 30 cycles of list_queued/get/list_ grew "
        f"open-FD count by {growth} (baseline={baseline}, after={after})."
    )


def test_drainer_can_run_5000_passes_without_exhausting_fds(tmp_path: Path):
    """End-to-end pin: simulate the drainer's 1-second poll loop.

    With the original bug, this test exhausts the soft FD limit
    (``RLIMIT_NOFILE``) within a few hundred iterations and raises
    ``sqlite3.OperationalError``. With the fix in place it completes
    cleanly.
    """
    db = tmp_path / "outgoing.db"
    queue = OutgoingQueue(db)

    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    # Drop the soft limit to make the bug surface fast even if the
    # default is high — without this, the test would need 10_000+
    # iterations on macOS where soft is often 256.
    cap = min(soft, 128)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (cap, _hard))
        for _ in range(5000):
            queue.list_queued(limit=4)
    except OSError as e:
        pytest.fail(
            f"FD exhaustion: drainer-style polling raised {e!r} after "
            f"some number of iterations under FD cap {cap}. The fix "
            "(close connections returned by _connect) is missing."
        )
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, _hard))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
