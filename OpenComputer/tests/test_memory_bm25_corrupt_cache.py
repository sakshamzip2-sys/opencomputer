"""A corrupt cache file must not surface as a runtime error."""

from pathlib import Path

from opencomputer.agent.memory_index import BM25Index


# BM25 needs a corpus with more than ~2 docs to score reliably (IDF on a
# 2-doc corpus where every doc shares a term collapses to 0).  These
# fixtures use 5 docs to keep the cache-integrity behavior testable
# independent of BM25 ranking quirks.
_CORPUS_5 = (
    "alpha cats\n\n"
    "beta dogs\n\n"
    "gamma birds\n\n"
    "delta fish\n\n"
    "epsilon turtles"
)


def test_truncated_cache_rebuilds_silently(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text(_CORPUS_5, encoding="utf-8")
    idx = BM25Index(tmp_path)
    idx.query("cats")  # build + cache

    cache_path = tmp_path / "cache" / "memory_bm25.idx"
    raw = cache_path.read_bytes()
    cache_path.write_bytes(raw[: len(raw) // 2])  # truncate to 50%

    idx2 = BM25Index(tmp_path)
    hits = idx2.query("dogs")
    assert hits, "rebuild must succeed and surface results"
    # cache must be rewritten to a valid file
    assert cache_path.read_bytes() != raw[: len(raw) // 2]


def test_garbage_cache_rebuilds_silently(tmp_path: Path) -> None:
    import pickle as _pickle

    (tmp_path / "MEMORY.md").write_text(_CORPUS_5, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_path = cache_dir / "memory_bm25.idx"
    cache_path.write_bytes(b"this is not a pickle")

    idx = BM25Index(tmp_path)
    hits = idx.query("cats")
    assert hits, "garbage cache must not block retrieval"

    # the rewritten cache must be a valid pickle with the expected schema
    with cache_path.open("rb") as f:
        data = _pickle.load(f)
    assert data["header"]["format_version"] == BM25Index.FORMAT_VERSION
    assert "entries" in data and "tokens" in data and "bm25" in data
