"""Tests for ScreenHistoryStore — opt-in JSONL append + 7-day TTL rotation."""
from __future__ import annotations

import json
from pathlib import Path

from extensions.screen_awareness.persist import ScreenHistoryStore
from extensions.screen_awareness.ring_buffer import ScreenCapture


def _mk_capture(captured_at: float, text: str = "x") -> ScreenCapture:
    return ScreenCapture(
        captured_at=captured_at,
        text=text,
        sha256="hash" + str(int(captured_at)),
        trigger="user_message",
        session_id="s1",
    )


def test_append_creates_jsonl_file(tmp_path: Path):
    store = ScreenHistoryStore(path=tmp_path / "screen.jsonl", enabled=True)
    store.append(_mk_capture(captured_at=100.0, text="hello"))
    assert (tmp_path / "screen.jsonl").exists()
    line = (tmp_path / "screen.jsonl").read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["text"] == "hello"
    assert record["captured_at"] == 100.0


def test_disabled_store_does_not_write(tmp_path: Path):
    store = ScreenHistoryStore(path=tmp_path / "screen.jsonl", enabled=False)
    store.append(_mk_capture(captured_at=100.0))
    assert not (tmp_path / "screen.jsonl").exists()


def test_ttl_rotation_drops_old_entries(tmp_path: Path):
    import time

    p = tmp_path / "screen.jsonl"
    store = ScreenHistoryStore(path=p, enabled=True, ttl_seconds=10.0)
    now = time.time()
    store.append(_mk_capture(captured_at=now - 100, text="old"))
    store.append(_mk_capture(captured_at=now - 1, text="recent"))
    store.prune()
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["text"] == "recent"


def test_prune_when_file_missing_is_noop(tmp_path: Path):
    store = ScreenHistoryStore(path=tmp_path / "missing.jsonl", enabled=True)
    store.prune()  # should not raise


def test_atomic_write_no_tmp_leftover(tmp_path: Path):
    import time

    p = tmp_path / "screen.jsonl"
    store = ScreenHistoryStore(path=p, enabled=True, ttl_seconds=1.0)
    store.append(_mk_capture(captured_at=time.time() - 100))
    store.prune()
    assert not (tmp_path / "screen.jsonl.tmp").exists()
