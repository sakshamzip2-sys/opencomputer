"""External edits to MEMORY.md trigger a rebuild via sha256 mismatch."""

from pathlib import Path

from opencomputer.agent.memory_index import BM25Index


def test_external_edit_triggers_rebuild(tmp_path: Path) -> None:
    # BM25Okapi IDF degenerates to 0 when a query term appears in every doc
    # AND when corpora are tiny (N<=2).  Use a 5-doc fixture so signals
    # actually rank.
    mem = tmp_path / "MEMORY.md"
    mem.write_text(
        "alpha cats\n\nbeta dogs\n\ngamma birds\n\ndelta fish\n\nepsilon turtles",
        encoding="utf-8",
    )

    idx = BM25Index(tmp_path)
    hits = idx.query("cats")
    assert any("cats" in h.entry.raw for h in hits)

    # external edit (no MemoryManager involvement, no invalidate() call)
    mem.write_text(
        "alpha cats\n\nbeta dogs\n\ngamma birds\n\ndelta fish\n\nepsilon turtles\n\nrocketship orbit",
        encoding="utf-8",
    )

    # fresh instance simulates a fresh process
    idx2 = BM25Index(tmp_path)
    hits = idx2.query("rocketship")
    assert any("rocketship" in h.entry.raw for h in hits)
