"""Layer 2 helper — Chrome / Brave / Edge history reader (SQLite).

Each Chromium-family browser stores history at:
``~/Library/Application Support/<Vendor>/<Profile>/History`` on macOS.

Reading requires the file to be unlocked — Chromium holds a SQLite
exclusive lock while running. We copy the DB to a tempfile first
(file copy bypasses the SQLite lock on macOS APFS), then read.

Platform caveat: ``shutil.copyfile`` bypasses the SQLite exclusive lock
on macOS APFS and on Linux ext4 (these filesystems do not honor the
SQLite advisory lock for a plain file copy). On Windows NTFS, however,
the lock IS respected and this strategy will fail when Chrome is
running. Windows support is out of MVP scope.

Safari uses ``~/Library/Safari/History.db`` (different schema —
not in MVP). Firefox uses ``places.sqlite`` (different schema — also
not in MVP). When we add Brave/Edge, the ``browser`` field on
``BrowserVisitSummary`` will need to support more values than just
``"chrome"``; for MVP a free-form string is sufficient.
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("opencomputer.profile_bootstrap.browser_history")


@dataclass(frozen=True, slots=True)
class BrowserVisitSummary:
    """One URL visit. URL + title only — no page content in MVP."""

    url: str = ""
    title: str = ""
    visit_time: float = 0.0  # epoch seconds
    browser: str = ""


def read_chrome_history(
    *,
    history_db: Path | None = None,
    days: int = 7,
    max_visits: int = 2000,
) -> list[BrowserVisitSummary]:
    """Read Chrome-format history. ``history_db`` defaults to the macOS path."""
    if history_db is None:
        history_db = (
            Path.home()
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "Default"
            / "History"
        )
    if not history_db.exists():
        return []

    cutoff_secs = time.time() - (days * 24 * 3600)
    # Chrome time = microseconds since 1601-01-01; convert cutoff.
    cutoff_chrome = int((cutoff_secs + 11644473600) * 1_000_000)

    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "History"
        try:
            shutil.copyfile(history_db, copy_path)
        except OSError as exc:
            _log.warning("Could not copy Chrome History (%s): %s", history_db, exc)
            return []
        try:
            conn = sqlite3.connect(f"file:{copy_path}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            return []
        try:
            cur = conn.execute(
                "SELECT url, title, last_visit_time "
                "FROM urls "
                "WHERE last_visit_time >= ? "
                "ORDER BY last_visit_time DESC "
                "LIMIT ?",
                (cutoff_chrome, max_visits),
            )
            rows = cur.fetchall()
        except sqlite3.DatabaseError:
            return []
        finally:
            conn.close()

    out: list[BrowserVisitSummary] = []
    for url, title, visit_time in rows:
        secs = (visit_time / 1_000_000) - 11644473600
        out.append(
            BrowserVisitSummary(
                url=str(url or "")[:1024],
                title=str(title or "")[:256],
                visit_time=float(secs),
                browser="chrome",
            )
        )
    return out
