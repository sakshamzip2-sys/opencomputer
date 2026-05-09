"""invalidate() drops in-memory state and on-disk cache; next query rebuilds."""

from pathlib import Path

from opencomputer.agent.memory_index import BM25Index


_CORPUS_5 = (
    "alpha cats\n\n"
    "beta dogs\n\n"
    "gamma birds\n\n"
    "delta fish\n\n"
    "epsilon turtles"
)


def test_invalidate_removes_cache_file(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text(_CORPUS_5, encoding="utf-8")
    idx = BM25Index(tmp_path)
    idx.query("cats")  # build + cache
    cache_path = tmp_path / "cache" / "memory_bm25.idx"
    assert cache_path.exists()

    idx.invalidate()
    assert not cache_path.exists()


def test_invalidate_drops_in_memory_state(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text(_CORPUS_5, encoding="utf-8")
    idx = BM25Index(tmp_path)
    idx.query("cats")
    assert idx._entries, "precondition: in-memory entries populated"

    idx.invalidate()
    assert not idx._entries
    assert idx._bm25 is None
    assert idx._loaded is False


def test_invalidate_then_query_reflects_new_corpus(tmp_path: Path) -> None:
    mem = tmp_path / "MEMORY.md"
    mem.write_text(_CORPUS_5, encoding="utf-8")
    idx = BM25Index(tmp_path)
    idx.query("cats")

    # write a new MEMORY.md and notify via invalidate
    mem.write_text(
        _CORPUS_5 + "\n\nzeta rocketship orbit",
        encoding="utf-8",
    )
    idx.invalidate()

    hits = idx.query("rocketship")
    assert any("rocketship" in h.entry.raw for h in hits)


def test_invalidate_when_no_cache_exists_is_noop(tmp_path: Path) -> None:
    idx = BM25Index(tmp_path)
    idx.invalidate()  # must not raise
    assert idx._entries == []
