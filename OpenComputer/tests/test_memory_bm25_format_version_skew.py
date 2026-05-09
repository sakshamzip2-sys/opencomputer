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
