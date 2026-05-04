"""Tests for the MiniMax SkillSource adapter (Wave 6.E.2).

Uses fixture filesystems instead of real GitHub clones — we plant a
fake clone directory in tmp_path and bypass git via monkeypatching
``_ensure_cloned`` to be a no-op.

Covers:
- ``name`` is stable at ``minimax`` regardless of upstream repo
- Identifier prefix is ``minimax/<skill-name>``
- ``search()`` walks ``skills/`` subdir when present, full tree otherwise
- ``fetch()`` returns SkillBundle with SKILL.md + ref files
- ``fetch()`` skips files > 1 MiB
- ``fetch()`` returns None for unknown identifier
- ``inspect()`` returns metadata for known skill
- Default tap registration: MiniMaxSource appears in ``_build_router()``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.skills_hub.sources.minimax import MiniMaxSource


@pytest.fixture()
def fake_clone(tmp_path: Path, monkeypatch) -> Path:
    """Plant a fake MiniMax-AI/cli clone with two skills."""
    clone_root = tmp_path / "_clones"
    clone_dir = clone_root / "MiniMax-AI" / "cli"
    skills = clone_dir / "skills"
    skills.mkdir(parents=True)

    # Skill 1 — vanilla SKILL.md + a ref file (description >=20 chars per validator)
    (skills / "alpha").mkdir()
    (skills / "alpha" / "SKILL.md").write_text(
        "---\n"
        "name: alpha\n"
        "description: First MiniMax skill — the alpha demo for tests\n"
        "---\n"
        "\nBody of alpha skill.\n"
    )
    (skills / "alpha" / "ref.md").write_text("Reference content.")

    # Skill 2 — different name with no extra files
    (skills / "beta").mkdir()
    (skills / "beta" / "SKILL.md").write_text(
        "---\n"
        "name: beta\n"
        "description: Second MiniMax skill — does the B things for tests\n"
        "---\n"
        "\nBody of beta skill.\n"
    )

    # Patch _ensure_cloned to be a no-op (clone already on disk)
    monkeypatch.setattr(
        "opencomputer.skills_hub.sources.github.GitHubSource._ensure_cloned",
        lambda self: None,
    )
    return clone_root


def test_name_is_stable_minimax(tmp_path: Path):
    src = MiniMaxSource(clone_root=tmp_path)
    assert src.name == "minimax"


def test_search_returns_skills_with_minimax_prefix(fake_clone: Path):
    src = MiniMaxSource(clone_root=fake_clone)
    results = src.search("")
    assert len(results) == 2
    ids = {r.identifier for r in results}
    assert ids == {"minimax/alpha", "minimax/beta"}
    for r in results:
        assert r.source == "minimax"


def test_search_filters_by_query(fake_clone: Path):
    src = MiniMaxSource(clone_root=fake_clone)
    results = src.search("B things")
    assert len(results) == 1
    assert results[0].identifier == "minimax/beta"


def test_inspect_returns_meta(fake_clone: Path):
    src = MiniMaxSource(clone_root=fake_clone)
    meta = src.inspect("minimax/alpha")
    assert meta is not None
    assert meta.name == "alpha"
    assert "alpha demo" in meta.description


def test_inspect_unknown_returns_none(fake_clone: Path):
    src = MiniMaxSource(clone_root=fake_clone)
    assert src.inspect("minimax/does-not-exist") is None


def test_inspect_wrong_prefix_returns_none(fake_clone: Path):
    src = MiniMaxSource(clone_root=fake_clone)
    assert src.inspect("github.com/foo/bar") is None


def test_fetch_returns_bundle_with_ref_files(fake_clone: Path):
    src = MiniMaxSource(clone_root=fake_clone)
    bundle = src.fetch("minimax/alpha")
    assert bundle is not None
    assert bundle.identifier == "minimax/alpha"
    assert "Body of alpha skill." in bundle.skill_md
    assert "ref.md" in bundle.files
    assert bundle.files["ref.md"] == "Reference content."


def test_fetch_unknown_returns_none(fake_clone: Path):
    src = MiniMaxSource(clone_root=fake_clone)
    assert src.fetch("minimax/does-not-exist") is None


def test_fetch_wrong_prefix_returns_none(fake_clone: Path):
    src = MiniMaxSource(clone_root=fake_clone)
    assert src.fetch("github.com/x/y") is None


def test_fetch_skips_large_files(fake_clone: Path):
    """Files > 1 MiB get logged + skipped, not returned."""
    big = fake_clone / "MiniMax-AI" / "cli" / "skills" / "alpha" / "huge.bin"
    # 1 MiB + 1
    big.write_bytes(b"x" * (1_000_001))

    src = MiniMaxSource(clone_root=fake_clone)
    bundle = src.fetch("minimax/alpha")
    assert bundle is not None
    assert "huge.bin" not in bundle.files


def test_skill_outside_skills_subdir_also_walked(tmp_path: Path, monkeypatch):
    """If MiniMax-AI/cli ever moves to flat layout, fall back to full tree."""
    clone_root = tmp_path / "_clones"
    clone_dir = clone_root / "MiniMax-AI" / "cli"
    clone_dir.mkdir(parents=True)
    # No skills/ subdir — put SKILL.md at top level
    (clone_dir / "gamma").mkdir()
    (clone_dir / "gamma" / "SKILL.md").write_text(
        "---\nname: gamma\ndescription: top-level skill demo for the fall-through walk\n---\n"
    )

    monkeypatch.setattr(
        "opencomputer.skills_hub.sources.github.GitHubSource._ensure_cloned",
        lambda self: None,
    )
    src = MiniMaxSource(clone_root=clone_root)
    results = src.search("")
    assert any(r.identifier == "minimax/gamma" for r in results)


def test_minimax_registered_as_default_tap():
    """Importing _build_router must produce a router with MiniMax in the
    default source list (next to WellKnownSource), without the user
    having to add a tap."""
    from opencomputer.cli_skills_hub import _build_router

    router = _build_router()
    names = router.list_sources()
    assert "minimax" in names
    assert "well-known" in names
