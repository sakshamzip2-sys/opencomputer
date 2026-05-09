"""MemoryManager owns one BM25Index per profile and invalidates on writes."""

from pathlib import Path

from opencomputer.agent.memory import MemoryManager


def _make_manager(tmp_path: Path) -> MemoryManager:
    decl = tmp_path / "MEMORY.md"
    decl.write_text("", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    return MemoryManager(declarative_path=decl, skills_path=skills)


def _seed_5_entries(mm: MemoryManager) -> None:
    """Seed enough entries for BM25 IDF to be non-degenerate."""
    for entry in [
        "alpha cats topic",
        "beta dogs topic",
        "gamma birds topic",
        "delta fish topic",
        "epsilon turtles topic",
    ]:
        mm.append_declarative(entry)


def test_memory_manager_exposes_bm25_index(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    assert mm.bm25_index is not None
    assert hasattr(mm.bm25_index, "query")


def test_append_declarative_makes_entry_queryable(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    _seed_5_entries(mm)
    mm.append_declarative("user prefers postgresql for OLTP workloads")

    hits = mm.bm25_index.query("postgresql")
    assert any("postgresql" in h.entry.raw for h in hits)


def test_replace_declarative_invalidates_index(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    _seed_5_entries(mm)
    mm.replace_declarative("dogs", "rocketships")
    hits = mm.bm25_index.query("rocketships")
    assert any("rocketships" in h.entry.raw for h in hits)


def test_remove_declarative_invalidates_index(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    _seed_5_entries(mm)
    pre_hits = mm.bm25_index.query("dogs")
    assert any("dogs" in h.entry.raw for h in pre_hits)

    mm.remove_declarative("beta dogs topic")
    post_hits = mm.bm25_index.query("dogs")
    assert not any("dogs" in h.entry.raw for h in post_hits)


def test_rebind_to_profile_swaps_index(tmp_path: Path) -> None:
    profile_a = tmp_path / "a"
    profile_a.mkdir()
    decl_a = profile_a / "MEMORY.md"
    # 5+ entries so BM25 IDF works.  Last entry contains the unique token "alpha".
    decl_a.write_text(
        "alpha cats\n\nbeta dogs\n\ngamma birds\n\ndelta fish\n\nepsilon turtles",
        encoding="utf-8",
    )
    skills_a = profile_a / "skills"
    skills_a.mkdir()

    profile_b = tmp_path / "b"
    profile_b.mkdir()
    decl_b = profile_b / "MEMORY.md"
    decl_b.write_text(
        "uno orbit\n\ndos rocketship\n\ntres satellite\n\ncuatro mission\n\ncinco probe",
        encoding="utf-8",
    )

    mm = MemoryManager(declarative_path=decl_a, skills_path=skills_a)
    a_hits = mm.bm25_index.query("alpha")
    assert any("alpha" in h.entry.raw for h in a_hits)

    mm.rebind_to_profile(profile_b)
    b_hits = mm.bm25_index.query("rocketship")
    assert any("rocketship" in h.entry.raw for h in b_hits)
    a_hits_after = mm.bm25_index.query("alpha")
    assert not any("alpha" in h.entry.raw for h in a_hits_after)


def test_cache_lives_under_profile_home(tmp_path: Path) -> None:
    mm = _make_manager(tmp_path)
    _seed_5_entries(mm)
    mm.bm25_index.query("cats")
    assert (tmp_path / "cache" / "memory_bm25.idx").exists()
