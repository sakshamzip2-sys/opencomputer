"""Layered Awareness MVP — Layer 2 browser history reader tests.

Builds a minimal Chrome-shaped fixture DB so we exercise the real
``shutil.copyfile`` + ``sqlite3.connect`` + WebKit-timestamp-decode path
without relying on a real Chrome installation.
"""
import sqlite3
import time
from pathlib import Path

from opencomputer.profile_bootstrap.browser_history import (
    BrowserVisitSummary,
    read_chrome_history,
)


def _build_chrome_db(path: Path, urls: list[tuple[str, str, int]]) -> None:
    """Build a minimal Chrome-shaped History DB. urls = [(url, title, visit_seconds)]."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE urls(
            id INTEGER PRIMARY KEY,
            url TEXT,
            title TEXT,
            visit_count INTEGER DEFAULT 0,
            last_visit_time INTEGER DEFAULT 0
        );
        """
    )
    # Chrome encodes time as microseconds since 1601-01-01.
    for i, (u, t, secs) in enumerate(urls):
        chrome_time = (secs + 11644473600) * 1_000_000
        conn.execute(
            "INSERT INTO urls(id, url, title, visit_count, last_visit_time) VALUES (?, ?, ?, ?, ?)",
            (i + 1, u, t, 1, chrome_time),
        )
    conn.commit()
    conn.close()


def test_read_chrome_history_recent_only(tmp_path: Path):
    db = tmp_path / "History"
    now = int(time.time())
    _build_chrome_db(
        db,
        [
            ("https://example.com", "Example", now - 60),
            ("https://old.com", "Old", now - 30 * 24 * 3600),
        ],
    )
    visits = read_chrome_history(history_db=db, days=7)
    assert len(visits) == 1
    assert visits[0].url == "https://example.com"


def test_read_chrome_history_returns_empty_when_missing(tmp_path: Path):
    visits = read_chrome_history(history_db=tmp_path / "nope", days=7)
    assert visits == []
