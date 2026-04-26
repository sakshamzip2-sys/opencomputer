"""Layer 2 helper — Chromium-family browser history reader (SQLite).

Each Chromium-family browser (Chrome, Brave, Edge, Vivaldi, Arc,
Chromium itself) stores its per-profile history at
``~/Library/Application Support/<Vendor>/.../<Profile>/History`` on
macOS. They all share the same SQLite schema (``urls`` table with
``last_visit_time`` in WebKit microseconds since 1601-01-01), so a
single reader handles the entire family — we just enumerate the
profile dirs.

Reading requires the file to be unlocked — Chromium holds a SQLite
exclusive lock while running. We copy the DB to a tempfile first
(file copy bypasses the SQLite lock on macOS APFS), then read.

Platform caveat: ``shutil.copyfile`` bypasses the SQLite exclusive lock
on macOS APFS and on Linux ext4 (these filesystems do not honor the
SQLite advisory lock for a plain file copy). On Windows NTFS, however,
the lock IS respected and this strategy will fail when the browser is
running. Windows support is out of MVP scope.

Safari uses ``~/Library/Safari/History.db`` (different schema —
not in MVP). Firefox uses ``places.sqlite`` (different schema — also
not in MVP).

V2.A-T6 — multi-browser scanning. Public API:

- :func:`read_browser_history` — single-DB workhorse, ``browser`` param
  tags the source on each :class:`BrowserVisitSummary`.
- :func:`read_all_browser_history` — discovers all installed browsers
  + profiles and aggregates visits across them.
- :func:`read_chrome_history` — backward-compat single-Chrome-profile
  alias; new callers should prefer :func:`read_all_browser_history`.
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


#: Known Chromium-family browsers and their macOS Application Support root paths.
#: All share the same SQLite schema (urls table, WebKit µs timestamps), so one
#: reader handles all of them — we just enumerate.
_CHROMIUM_FAMILY_ROOTS: dict[str, Path] = {
    "chrome": Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
    "brave": Path.home() / "Library" / "Application Support" / "BraveSoftware" / "Brave-Browser",
    "edge": Path.home() / "Library" / "Application Support" / "Microsoft Edge",
    "vivaldi": Path.home() / "Library" / "Application Support" / "Vivaldi",
    "arc": Path.home() / "Library" / "Application Support" / "Arc" / "User Data",
    "chromium": Path.home() / "Library" / "Application Support" / "Chromium",
}


def _discover_history_dbs() -> list[tuple[str, Path]]:
    """Enumerate ``(browser_name, history_db_path)`` pairs for installed browsers.

    A browser profile dir is recognized by the presence of a ``History``
    SQLite file. Profile dirs are typically named ``Default`` or
    ``Profile N``. Browsers not installed (root path missing) yield no
    entries. Permission errors on ``iterdir`` are swallowed per-browser
    so one inaccessible vendor dir does not hide the others.
    """
    out: list[tuple[str, Path]] = []
    for browser, root in _CHROMIUM_FAMILY_ROOTS.items():
        if not root.exists():
            continue
        try:
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                history = entry / "History"
                if history.exists():
                    out.append((browser, history))
        except (OSError, PermissionError):
            continue
    return out


def read_browser_history(
    *,
    history_db: Path | None = None,
    browser: str = "chrome",
    days: int = 7,
    max_visits: int = 2000,
) -> list[BrowserVisitSummary]:
    """Read history from a single Chromium-family DB.

    ``browser`` tags the source on each returned summary. ``history_db``
    defaults to the standard macOS Chrome ``Default`` profile path so
    the legacy single-Chrome-profile flow keeps working.
    """
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
            _log.warning("Could not copy %s History (%s): %s", browser, history_db, exc)
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
                browser=browser,
            )
        )
    return out


def read_all_browser_history(
    *,
    days: int = 7,
    max_visits_per_db: int = 2000,
) -> list[BrowserVisitSummary]:
    """Discover installed Chromium-family browsers and read history from each.

    Returns a flat list of visits across all browsers + profiles. The
    ``browser`` field on each summary identifies the source (chrome /
    brave / edge / vivaldi / arc / chromium). The aggregate may exceed
    ``max_visits_per_db`` because each DB caps independently — V2 keeps
    this simple; a global cap or merge-by-URL dedup is a V3 concern.
    """
    out: list[BrowserVisitSummary] = []
    for browser, db_path in _discover_history_dbs():
        out.extend(
            read_browser_history(
                history_db=db_path,
                browser=browser,
                days=days,
                max_visits=max_visits_per_db,
            )
        )
    return out


def read_chrome_history(
    *,
    history_db: Path | None = None,
    days: int = 7,
    max_visits: int = 2000,
) -> list[BrowserVisitSummary]:
    """Legacy single-Chrome-profile reader. Prefer ``read_all_browser_history``."""
    return read_browser_history(
        history_db=history_db,
        browser="chrome",
        days=days,
        max_visits=max_visits,
    )
