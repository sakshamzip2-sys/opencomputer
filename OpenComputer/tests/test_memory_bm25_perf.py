"""Performance smoke test (lenient on CI; loose vs spec target)."""

import time
from pathlib import Path

from opencomputer.agent.memory_index import BM25Index


def _synth_memory_4kb(profile_home: Path) -> None:
    profile_home.mkdir(parents=True, exist_ok=True)
    paragraphs = []
    for i in range(40):
        paragraphs.append(
            f"Entry {i}: This is a paragraph about topic number {i} "
            f"with several keywords like postgresql, rocketship, alpha, "
            f"and beta to give the BM25 index something to differentiate."
        )
    text = "\n\n".join(paragraphs)
    assert 3000 <= len(text) <= 8000, f"synthetic corpus size out of expected range: {len(text)}"
    (profile_home / "MEMORY.md").write_text(text, encoding="utf-8")


def test_cold_build_under_250ms(tmp_path: Path) -> None:
    _synth_memory_4kb(tmp_path)
    idx = BM25Index(tmp_path)
    t0 = time.perf_counter()
    idx.query("postgresql")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 250, f"cold build took {elapsed_ms:.1f}ms (target <250ms)"


def test_warm_query_under_50ms(tmp_path: Path) -> None:
    _synth_memory_4kb(tmp_path)
    idx = BM25Index(tmp_path)
    idx.query("postgresql")  # build + cache

    idx2 = BM25Index(tmp_path)
    t0 = time.perf_counter()
    idx2.query("rocketship")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 50, f"warm query took {elapsed_ms:.1f}ms (target <50ms)"
