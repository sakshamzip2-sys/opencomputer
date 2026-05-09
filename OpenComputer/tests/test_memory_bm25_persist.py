"""Cache persistence: a built BM25Index survives a fresh instance."""

from pathlib import Path
from unittest.mock import patch

from opencomputer.agent.memory_index import BM25Index


def _write_memory(profile_home: Path, content: str) -> None:
    profile_home.mkdir(parents=True, exist_ok=True)
    (profile_home / "MEMORY.md").write_text(content, encoding="utf-8")


def test_cache_written_on_first_query(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma")
    idx = BM25Index(tmp_path)
    idx.query("alpha")
    assert (tmp_path / "cache" / "memory_bm25.idx").exists()


def test_cache_loaded_on_second_instance_no_rebuild(tmp_path: Path) -> None:
    _write_memory(tmp_path, "alpha\n\nbeta\n\ngamma")
    idx1 = BM25Index(tmp_path)
    idx1.query("alpha")  # builds + caches

    idx2 = BM25Index(tmp_path)
    with patch.object(BM25Index, "_build", autospec=True) as mock_build:
        hits = idx2.query("beta")
        mock_build.assert_not_called()
    assert any("beta" in h.entry.raw for h in hits)


def test_cache_directory_created(tmp_path: Path) -> None:
    _write_memory(tmp_path, "x\n\ny")
    BM25Index(tmp_path).query("x")
    assert (tmp_path / "cache").is_dir()
