"""Layered Awareness MVP — Layer 2 browser history reader tests.

Builds a minimal Chrome-shaped fixture DB so we exercise the real
``shutil.copyfile`` + ``sqlite3.connect`` + WebKit-timestamp-decode path
without relying on a real Chrome installation.

V2.A-T6 — adds coverage for multi-Chromium-family discovery
(:func:`_discover_history_dbs`) and aggregation
(:func:`read_all_browser_history`) plus the legacy
:func:`read_chrome_history` alias.
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


# ─── V2.A-T6 — multi-Chromium-family discovery + aggregation ─────────


def test_discover_history_dbs_returns_pairs(tmp_path: Path, monkeypatch):
    """``_discover_history_dbs`` walks known browser roots and returns existing DBs."""
    # Build a fake macOS layout under tmp_path
    chrome_root = tmp_path / "Chrome"
    (chrome_root / "Default").mkdir(parents=True)
    (chrome_root / "Default" / "History").touch()
    (chrome_root / "Profile 1").mkdir()
    (chrome_root / "Profile 1" / "History").touch()
    (chrome_root / "Empty").mkdir()  # no History — skipped

    brave_root = tmp_path / "Brave"
    (brave_root / "Default").mkdir(parents=True)
    (brave_root / "Default" / "History").touch()

    # Monkeypatch the family root map to point at our fake paths
    import opencomputer.profile_bootstrap.browser_history as bh
    monkeypatch.setattr(bh, "_CHROMIUM_FAMILY_ROOTS", {
        "chrome": chrome_root,
        "brave": brave_root,
    })

    pairs = bh._discover_history_dbs()
    by_browser = sorted([(b, p.parent.name) for b, p in pairs])
    assert by_browser == sorted([
        ("chrome", "Default"),
        ("chrome", "Profile 1"),
        ("brave", "Default"),
    ])


def test_read_all_browser_history_aggregates_across_browsers(
    tmp_path: Path, monkeypatch
):
    """End-to-end: build fake DBs in two browsers, verify aggregated reads."""
    chrome_root = tmp_path / "Chrome"
    (chrome_root / "Default").mkdir(parents=True)
    chrome_db = chrome_root / "Default" / "History"

    brave_root = tmp_path / "Brave"
    (brave_root / "Default").mkdir(parents=True)
    brave_db = brave_root / "Default" / "History"

    now = int(time.time())
    _build_chrome_db(chrome_db, [("https://chrome-recent.com", "Chrome", now - 60)])
    _build_chrome_db(brave_db, [("https://brave-recent.com", "Brave", now - 60)])

    import opencomputer.profile_bootstrap.browser_history as bh
    monkeypatch.setattr(bh, "_CHROMIUM_FAMILY_ROOTS", {
        "chrome": chrome_root,
        "brave": brave_root,
    })

    visits = bh.read_all_browser_history(days=7)
    urls = sorted([v.url for v in visits])
    browsers = sorted([v.browser for v in visits])
    assert urls == ["https://brave-recent.com", "https://chrome-recent.com"]
    assert browsers == ["brave", "chrome"]


def test_read_all_browser_history_returns_empty_on_no_browsers(
    tmp_path, monkeypatch
):
    """No installed browsers → []."""
    import opencomputer.profile_bootstrap.browser_history as bh
    # Empty roots map — no browsers configured.
    monkeypatch.setattr(bh, "_CHROMIUM_FAMILY_ROOTS", {})
    assert bh.read_all_browser_history(days=7) == []


def test_read_chrome_history_alias_still_works(tmp_path: Path):
    """Backward-compat: ``read_chrome_history`` still exists and reads a single DB."""
    db = tmp_path / "History"
    now = int(time.time())
    _build_chrome_db(db, [("https://example.com", "Example", now - 60)])

    visits = read_chrome_history(history_db=db, days=7)
    assert len(visits) == 1
    assert visits[0].browser == "chrome"
