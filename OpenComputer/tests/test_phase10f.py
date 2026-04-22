"""Phase 10f — Memory baseline completion.

Tests are organized by sub-phase (10f.A — 10f.J). Honcho plugin tests live in
tests/test_phase10f_honcho.py.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

# ─── 10f.A — MemoryManager hardening ───────────────────────────────────


class TestMemoryManagerHardening:
    """Atomic writes, locks, USER.md, replace/remove, backup/restore, stats."""

    @pytest.fixture
    def mm(self, tmp_path: Path):
        from opencomputer.agent.memory import MemoryManager

        return MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
            skills_path=tmp_path / "skills",
            memory_char_limit=2000,
            user_char_limit=1000,
        )

    # USER.md round-trip

    def test_read_user_returns_empty_when_missing(self, mm):
        assert mm.read_user() == ""

    def test_append_user_then_read(self, mm):
        mm.append_user("user prefers concise output")
        text = mm.read_user()
        assert "user prefers concise output" in text

    # replace / remove on declarative

    def test_replace_declarative_substitutes(self, mm):
        mm.append_declarative("the moon is made of cheese")
        assert mm.replace_declarative("cheese", "rock") is True
        assert "rock" in mm.read_declarative()
        assert "cheese" not in mm.read_declarative()

    def test_replace_declarative_returns_false_when_not_found(self, mm):
        mm.append_declarative("hello")
        assert mm.replace_declarative("nonexistent", "new") is False

    def test_remove_declarative_removes_block(self, mm):
        mm.append_declarative("line one")
        mm.append_declarative("line two")
        assert mm.remove_declarative("line one") is True
        remaining = mm.read_declarative()
        assert "line one" not in remaining
        assert "line two" in remaining

    # replace / remove on user

    def test_replace_user_substitutes(self, mm):
        mm.append_user("user likes blue")
        assert mm.replace_user("blue", "green") is True
        assert "green" in mm.read_user()

    def test_remove_user_deletes_block(self, mm):
        mm.append_user("ephemeral")
        mm.append_user("permanent")
        mm.remove_user("ephemeral")
        assert "ephemeral" not in mm.read_user()
        assert "permanent" in mm.read_user()

    # character limit enforcement

    def test_append_over_limit_raises(self, mm):
        from opencomputer.agent.memory import MemoryTooLargeError

        giant = "x" * 5000
        with pytest.raises(MemoryTooLargeError):
            mm.append_declarative(giant)

    def test_user_append_over_limit_raises(self, mm):
        from opencomputer.agent.memory import MemoryTooLargeError

        with pytest.raises(MemoryTooLargeError):
            mm.append_user("y" * 3000)

    # backup + restore

    def test_backup_and_restore(self, mm):
        mm.append_declarative("original content")
        # Next write triggers backup of the prior state.
        mm.append_declarative("newer content")
        assert Path(str(mm.declarative_path) + ".bak").exists()
        mm.restore_backup("memory")
        text = mm.read_declarative()
        assert "original content" in text
        # "newer content" was added after the backup was captured, so it goes
        # away on restore.
        assert "newer content" not in text

    def test_restore_backup_user(self, mm):
        mm.append_user("v1")
        mm.append_user("v2")
        mm.restore_backup("user")
        assert "v1" in mm.read_user()
        assert "v2" not in mm.read_user()

    # stats

    def test_stats_reports_sizes(self, mm):
        mm.append_declarative("abc")
        mm.append_user("xyz123")
        stats = mm.stats()
        assert stats["memory_chars"] >= 3
        assert stats["user_chars"] >= 6
        assert stats["memory_char_limit"] == 2000
        assert stats["user_char_limit"] == 1000

    # atomic + thread-safe writes

    def test_concurrent_appends_do_not_corrupt(self, mm):
        # Each entry is short and unique. With proper locking, every entry
        # should be readable after all threads finish.
        entries = [f"entry-{i}" for i in range(20)]

        def worker(e: str) -> None:
            mm.append_declarative(e)

        threads = [threading.Thread(target=worker, args=(e,)) for e in entries]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = mm.read_declarative()
        for e in entries:
            assert e in final, f"lost entry: {e}"

    def test_atomic_write_never_leaves_partial_file(self, mm, monkeypatch):
        # Simulate interruption: patch os.replace to raise after the tmp file
        # is created. The original MEMORY.md should remain intact.
        import os

        mm.append_declarative("original")
        original_bytes = mm.declarative_path.read_bytes()

        real_replace = os.replace

        def boom(src, dst):  # type: ignore
            raise RuntimeError("simulated interrupt")

        monkeypatch.setattr(os, "replace", boom)
        try:
            mm.append_declarative("new entry")  # should raise, but atomically
        except RuntimeError:
            pass
        monkeypatch.setattr(os, "replace", real_replace)

        # Original preserved.
        assert mm.declarative_path.read_bytes() == original_bytes
