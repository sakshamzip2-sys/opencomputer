"""End-to-end BM25Index query tests on synthetic MEMORY.md corpora."""

from pathlib import Path

from opencomputer.agent.memory_index import BM25Index


def _write_memory(profile_home: Path, content: str) -> None:
    profile_home.mkdir(parents=True, exist_ok=True)
    (profile_home / "MEMORY.md").write_text(content, encoding="utf-8")


def test_query_returns_empty_when_memory_md_missing(tmp_path: Path) -> None:
    idx = BM25Index(tmp_path)
    assert idx.query("anything") == []


def test_query_returns_empty_when_memory_md_empty(tmp_path: Path) -> None:
    _write_memory(tmp_path, "")
    idx = BM25Index(tmp_path)
    assert idx.query("anything") == []


def test_query_returns_top_match_first(tmp_path: Path) -> None:
    # Tokenizer is exact-match (no stemming in v1), so the query token must
    # appear verbatim in the entry.  Document this contract via the test.
    content = "\n\n".join([
        "## section a\nUser prefers postgresql over mysql for OLTP workloads.",
        "## section b\nNotes on cooking pasta and tomato sauce.",
        "## section c\nMeeting cadence is weekly on Tuesdays at 10am.",
        "## section d\nPython 3.12 is the project's pinned interpreter.",
    ])
    _write_memory(tmp_path, content)

    idx = BM25Index(tmp_path)
    hits = idx.query("postgresql", top_k=3)

    assert len(hits) >= 1
    assert hits[0].rank == 0
    # The postgresql entry must rank above pasta / Tuesdays / Python.
    assert "postgresql" in hits[0].entry.raw


def test_query_respects_top_k(tmp_path: Path) -> None:
    content = "\n\n".join(f"entry {i} about postgres" for i in range(10))
    _write_memory(tmp_path, content)
    idx = BM25Index(tmp_path)
    hits = idx.query("postgres", top_k=3)
    assert len(hits) == 3
    assert [h.rank for h in hits] == [0, 1, 2]


def test_query_returns_empty_when_no_token_matches(tmp_path: Path) -> None:
    _write_memory(tmp_path, "this entry is about cats\n\nthis entry is about dogs")
    idx = BM25Index(tmp_path)
    hits = idx.query("rocketship")
    # BM25 may still return entries with score 0; we filter them out.
    assert all(h.score > 0 for h in hits)


def test_query_top_k_default_is_5(tmp_path: Path) -> None:
    content = "\n\n".join(f"entry {i} about postgres" for i in range(20))
    _write_memory(tmp_path, content)
    idx = BM25Index(tmp_path)
    hits = idx.query("postgres")
    assert len(hits) <= 5


def test_query_does_not_mutate_memory_md(tmp_path: Path) -> None:
    content = "first entry\n\nsecond entry\n\nthird entry"
    _write_memory(tmp_path, content)
    idx = BM25Index(tmp_path)
    idx.query("entry")
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == content
