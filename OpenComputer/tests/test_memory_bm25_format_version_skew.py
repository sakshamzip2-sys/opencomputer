"""A cache with a stale format_version triggers rebuild."""

import pickle
from pathlib import Path

from opencomputer.agent.memory_index import BM25Index

_CORPUS_5 = (
    "alpha cats\n\n"
    "beta dogs\n\n"
    "gamma birds\n\n"
    "delta fish\n\n"
    "epsilon turtles"
)


def test_old_format_version_rebuilds(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text(_CORPUS_5, encoding="utf-8")
    idx = BM25Index(tmp_path)
    idx.query("cats")

    cache_path = tmp_path / "cache" / "memory_bm25.idx"
    with cache_path.open("rb") as f:
        data = pickle.load(f)
    data["header"]["format_version"] = 999
    with cache_path.open("wb") as f:
        pickle.dump(data, f)

    idx2 = BM25Index(tmp_path)
    hits = idx2.query("dogs")
    assert hits

    with cache_path.open("rb") as f:
        rebuilt = pickle.load(f)
    assert rebuilt["header"]["format_version"] == BM25Index.FORMAT_VERSION


def test_rank_bm25_version_skew_rebuilds(tmp_path: Path) -> None:
    """An in-place rank_bm25 upgrade must invalidate the cache (the pickled
    BM25Okapi instance may have a different shape across versions)."""
    (tmp_path / "MEMORY.md").write_text(_CORPUS_5, encoding="utf-8")
    idx = BM25Index(tmp_path)
    idx.query("cats")

    cache_path = tmp_path / "cache" / "memory_bm25.idx"
    with cache_path.open("rb") as f:
        data = pickle.load(f)
    data["header"]["rank_bm25_version"] = "0.1.0"
    with cache_path.open("wb") as f:
        pickle.dump(data, f)

    idx2 = BM25Index(tmp_path)
    hits = idx2.query("dogs")
    assert hits, "version skew must trigger rebuild + return real hits"

    with cache_path.open("rb") as f:
        rebuilt = pickle.load(f)
    # The rebuilt cache must record the actually-installed rank_bm25 version,
    # not "0.1.0".  Use the package version path for the assertion.
    from importlib.metadata import version
    assert rebuilt["header"]["rank_bm25_version"] == version("rank_bm25")


def test_bm25_object_type_guard_rebuilds(tmp_path: Path) -> None:
    """If the pickled bm25 field has been swapped for a non-BM25Okapi
    object (corruption or malicious manipulation), the loader rejects
    the cache rather than handing the bogus object to ``query``."""
    (tmp_path / "MEMORY.md").write_text(_CORPUS_5, encoding="utf-8")
    idx = BM25Index(tmp_path)
    idx.query("cats")

    cache_path = tmp_path / "cache" / "memory_bm25.idx"
    with cache_path.open("rb") as f:
        data = pickle.load(f)
    data["bm25"] = {"this": "is not a BM25Okapi"}
    with cache_path.open("wb") as f:
        pickle.dump(data, f)

    idx2 = BM25Index(tmp_path)
    hits = idx2.query("dogs")
    assert hits, "type guard must trigger rebuild"
