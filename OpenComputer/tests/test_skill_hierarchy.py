"""III.4 — hierarchical skill layout (references/ + examples/ subdirs).

Claude Code skills can live as either a flat single file (``<skill>/SKILL.md``)
OR as a directory tree with sibling ``references/*.md`` + ``examples/*`` holding
progressive-disclosure content. This suite pins:

- Flat skills still load (backwards compat).
- Hierarchical skills surface references + examples.
- On-access reads return actual file contents (lazy is ok; the visible
  contract is "ref.content gives you the markdown body").
- Mixed roots of both layouts coexist.
- A ``references/`` directory without a sibling ``SKILL.md`` is ignored
  rather than crashing the loader (documented skip, not error).
- The bundled ``debug-python-import-error`` migrated to the new layout
  carries >= 1 reference and >= 1 example.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_flat_skill(root: Path, skill_id: str, *, description: str = "test") -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {description}\n---\nflat body\n",
        encoding="utf-8",
    )


def _write_hierarchical_skill(
    root: Path,
    skill_id: str,
    *,
    description: str = "test",
    references: dict[str, str] | None = None,
    examples: dict[str, str] | None = None,
) -> None:
    skill_dir = root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: {description}\n---\nmain body\n",
        encoding="utf-8",
    )
    if references:
        refs_dir = skill_dir / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)
        for fname, body in references.items():
            (refs_dir / fname).write_text(body, encoding="utf-8")
    if examples:
        ex_dir = skill_dir / "examples"
        ex_dir.mkdir(parents=True, exist_ok=True)
        for fname, body in examples.items():
            (ex_dir / fname).write_text(body, encoding="utf-8")


# ─── flat skills still load ────────────────────────────────────────────


def test_flat_skill_loads_with_empty_references_and_examples(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    _write_flat_skill(user_skills, "flat-one", description="a flat skill")

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    found = {s.id: s for s in mm.list_skills()}
    assert "flat-one" in found
    meta = found["flat-one"]
    assert meta.references == ()
    assert meta.examples == ()


# ─── hierarchical: references + examples surfaced ─────────────────────


def test_hierarchical_skill_exposes_references_and_examples(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    _write_hierarchical_skill(
        user_skills,
        "deep-one",
        description="a hierarchical skill",
        references={
            "alpha.md": "# Alpha\nalpha body\n",
            "beta.md": "# Beta\nbeta body\n",
        },
        examples={
            "worked.md": "# Worked\nexample body\n",
            "payload.json": '{"kind":"example"}\n',
        },
    )

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    found = {s.id: s for s in mm.list_skills()}
    meta = found["deep-one"]
    ref_paths = {r.path.name for r in meta.references}
    assert ref_paths == {"alpha.md", "beta.md"}
    ex_paths = {e.path.name for e in meta.examples}
    assert ex_paths == {"worked.md", "payload.json"}


def test_reference_content_is_readable(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    _write_hierarchical_skill(
        user_skills,
        "deep-two",
        references={"note.md": "# Note\nreference content here\n"},
        examples={"ex.py": "print('hello')\n"},
    )

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    meta = next(s for s in mm.list_skills() if s.id == "deep-two")
    ref = meta.references[0]
    assert "reference content here" in ref.content
    ex = meta.examples[0]
    assert "print('hello')" in ex.content


def test_reference_title_prefers_frontmatter_then_h1_then_filename(tmp_path: Path) -> None:
    """Title derivation for on-demand references:
    - prefer frontmatter ``name`` / ``title`` if present,
    - else first ``# Heading`` line,
    - else filename stem.
    """
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    skill_dir = user_skills / "titled"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: titled\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "with-h1.md").write_text("# Real Title\nbody\n", encoding="utf-8")
    (refs / "no-heading.md").write_text("plain body\n", encoding="utf-8")
    (refs / "fm.md").write_text(
        "---\ntitle: Frontmatter Title\n---\nbody\n", encoding="utf-8"
    )

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    meta = next(s for s in mm.list_skills() if s.id == "titled")
    titles = {r.path.name: r.title for r in meta.references}
    assert titles["with-h1.md"] == "Real Title"
    assert titles["no-heading.md"] == "no-heading"
    assert titles["fm.md"] == "Frontmatter Title"


# ─── mixed flat + hierarchical in same root ───────────────────────────


def test_mixed_flat_and_hierarchical_in_same_root(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    _write_flat_skill(user_skills, "flat-x")
    _write_hierarchical_skill(
        user_skills,
        "deep-x",
        references={"r.md": "# R\nref\n"},
        examples={"e.md": "# E\nex\n"},
    )

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    ids = {s.id for s in mm.list_skills()}
    assert {"flat-x", "deep-x"} <= ids
    by_id = {s.id: s for s in mm.list_skills()}
    assert by_id["flat-x"].references == ()
    assert by_id["flat-x"].examples == ()
    assert len(by_id["deep-x"].references) == 1
    assert len(by_id["deep-x"].examples) == 1


# ─── malformed skill dir: references/ without SKILL.md is ignored ─────


def test_directory_with_references_but_no_skill_md_is_ignored(tmp_path: Path) -> None:
    """A directory under skills/ that has a ``references/`` subdir but no
    sibling ``SKILL.md`` is a partial/incomplete skill — it should be
    silently skipped by the loader, not raise. This matches the existing
    contract where directories without SKILL.md are already skipped.
    """
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    # Only a references/ dir — no SKILL.md file
    broken = user_skills / "broken"
    broken.mkdir()
    (broken / "references").mkdir()
    (broken / "references" / "something.md").write_text("# Orphan\n", encoding="utf-8")

    # Also add one valid skill so we know the loader kept going
    _write_flat_skill(user_skills, "valid-one")

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    ids = {s.id for s in mm.list_skills()}
    assert "broken" not in ids
    assert "valid-one" in ids


# ─── non-md example files are still readable ──────────────────────────


def test_examples_directory_reads_non_markdown_as_text(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    _write_hierarchical_skill(
        user_skills,
        "poly",
        examples={
            "run.py": "print('ok')\n",
            "config.yaml": "key: value\n",
            "data.json": '{"a": 1}\n',
        },
    )

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    meta = next(s for s in mm.list_skills() if s.id == "poly")
    by_name = {e.path.name: e for e in meta.examples}
    assert "print('ok')" in by_name["run.py"].content
    assert "key: value" in by_name["config.yaml"].content
    assert '"a": 1' in by_name["data.json"].content


# ─── references sorted deterministically ──────────────────────────────


def test_references_and_examples_sorted_by_filename(tmp_path: Path) -> None:
    """Deterministic ordering so prompt injection is stable across runs."""
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    _write_hierarchical_skill(
        user_skills,
        "sorted",
        references={
            "zeta.md": "# Z\n",
            "alpha.md": "# A\n",
            "middle.md": "# M\n",
        },
        examples={
            "z.md": "# Z\n",
            "a.md": "# A\n",
        },
    )

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    meta = next(s for s in mm.list_skills() if s.id == "sorted")
    assert [r.path.name for r in meta.references] == ["alpha.md", "middle.md", "zeta.md"]
    assert [e.path.name for e in meta.examples] == ["a.md", "z.md"]


# ─── bundled debug-python-import-error migrated to the new layout ─────


def test_bundled_debug_skill_migrated_to_hierarchy() -> None:
    """The bundled skill we picked to showcase the new layout must carry
    at least one reference + one example after migration. This doubles as
    a regression guard for future refactors."""
    from opencomputer.agent.memory import MemoryManager

    mm = MemoryManager(
        declarative_path=Path("/tmp/does-not-matter.md"),
        skills_path=Path("/tmp/nonexistent-user-skills"),
    )
    found = {s.id: s for s in mm.list_skills()}
    assert "debug-python-import-error" in found
    meta = found["debug-python-import-error"]
    assert len(meta.references) >= 1, "expected >=1 reference under references/"
    assert len(meta.examples) >= 1, "expected >=1 example under examples/"

    # Sanity-check: the reference files actually have content when read.
    for ref in meta.references:
        assert ref.content.strip(), f"empty reference content: {ref.path.name}"
    for ex in meta.examples:
        assert ex.content.strip(), f"empty example content: {ex.path.name}"


# ─── existing skill tool still works with bundled skill body ──────────


def test_skill_tool_still_reads_bundled_skill_body() -> None:
    """The Skill tool reads SKILL.md body via frontmatter — new references
    subdir shouldn't break that path."""
    import asyncio
    import tempfile

    from opencomputer.agent.memory import MemoryManager
    from opencomputer.tools.skill import SkillTool
    from plugin_sdk.core import ToolCall

    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        mm = MemoryManager(
            declarative_path=tmp_p / "MEMORY.md",
            skills_path=tmp_p / "user-skills",
        )
        tool = SkillTool(memory_manager=mm)
        result = asyncio.run(
            tool.execute(
                ToolCall(id="1", name="Skill", arguments={"name": "debug-python-import-error"})
            )
        )
    assert not result.is_error
    # Body content should be preserved
    assert "import error" in result.content.lower()


# ─── SkillReference type is importable from memory module ─────────────


def test_skill_reference_class_is_exposed() -> None:
    """Public API surface: SkillReference / SkillExample should be importable
    from opencomputer.agent.memory so consumers can type-hint against them."""
    from opencomputer.agent import memory as mm_mod

    assert hasattr(mm_mod, "SkillReference"), "SkillReference not exported"
    # SkillExample is just an alias for the same shape — not required to be
    # a distinct class, but at minimum the reference class must expose
    # path, title, content.
    r_cls = mm_mod.SkillReference
    # Either a dataclass or a class with these attrs on an instance —
    # easiest to check by constructing one and probing.
    if hasattr(r_cls, "__dataclass_fields__"):
        fields = set(r_cls.__dataclass_fields__.keys())
        assert {"path", "title"} <= fields


# ─── empty references/ or examples/ dirs give empty tuples ────────────


def test_empty_subdirs_yield_empty_tuples(tmp_path: Path) -> None:
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    skill_dir = user_skills / "empty-subs"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: empty-subs\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "examples").mkdir()

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    meta = next(s for s in mm.list_skills() if s.id == "empty-subs")
    assert meta.references == ()
    assert meta.examples == ()


# ─── references/ contents that aren't .md are skipped; examples/ accepts any ─


def test_references_filter_to_markdown_only(tmp_path: Path) -> None:
    """references/ is for structured docs — .md only. examples/ accepts
    mixed content (scripts, data files). Non-md files under references/
    are ignored rather than surfaced as references with potentially
    confusing titles.
    """
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    skill_dir = user_skills / "ref-filter"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: ref-filter\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    refs = skill_dir / "references"
    refs.mkdir()
    (refs / "real.md").write_text("# Real\n", encoding="utf-8")
    (refs / "image.png").write_bytes(b"\x89PNG\r\n")
    (refs / "script.py").write_text("print('ignored')\n", encoding="utf-8")

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    meta = next(s for s in mm.list_skills() if s.id == "ref-filter")
    ref_names = {r.path.name for r in meta.references}
    assert ref_names == {"real.md"}


# ─── regression: existing tests still work ────────────────────────────


def test_list_skills_returns_tuples_not_lists(tmp_path: Path) -> None:
    """references + examples fields must be tuples (frozen-dataclass
    friendly, hashable), not lists."""
    from opencomputer.agent.memory import MemoryManager

    user_skills = tmp_path / "skills"
    user_skills.mkdir()
    _write_hierarchical_skill(
        user_skills,
        "tuples",
        references={"r.md": "# R\n"},
        examples={"e.md": "# E\n"},
    )

    mm = MemoryManager(
        declarative_path=tmp_path / "MEMORY.md",
        skills_path=user_skills,
        bundled_skills_paths=[],
    )
    meta = next(s for s in mm.list_skills() if s.id == "tuples")
    assert isinstance(meta.references, tuple)
    assert isinstance(meta.examples, tuple)
