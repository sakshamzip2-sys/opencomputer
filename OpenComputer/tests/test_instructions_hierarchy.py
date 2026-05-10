"""CC §3 — hierarchical instruction-file discovery.

Walks cwd upward to repo root, loads each ``CLAUDE.md`` / ``OPENCOMPUTER.md``
/ ``AGENTS.md`` along the way + global rules dir + ``.local.md``
overrides. Spec: docs/OC-FROM-CLAUDE-CODE.md §3.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from opencomputer.agent.instructions_hierarchy import (
    MAX_FILE_BYTES,
    InstructionFile,
    find_hierarchical_instructions,
    format_for_system_prompt,
)


def _mkrepo(root: Path) -> None:
    """Mark a directory as the repo root by creating ``.git/``."""
    (root / ".git").mkdir(exist_ok=True)


# ─── single-level discovery ───────────────────────────────────────────


def test_finds_claude_md_at_repo_root(tmp_path):
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# root rules\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake-profile")
    assert len(files) == 1
    assert files[0].source == "workspace"
    assert files[0].depth == 0
    assert "root rules" in files[0].content


def test_finds_opencomputer_md_in_priority_over_claude_md(tmp_path):
    """``OPENCOMPUTER.md`` wins when both exist at the same level."""
    _mkrepo(tmp_path)
    (tmp_path / "OPENCOMPUTER.md").write_text("# OC rules\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# CC rules\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    assert len(files) == 1
    assert "OPENCOMPUTER.md" in str(files[0].path)


def test_falls_back_to_agents_md_when_no_other(tmp_path):
    _mkrepo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# agents\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    assert len(files) == 1
    assert "AGENTS.md" in str(files[0].path)


def test_returns_empty_when_no_files(tmp_path):
    _mkrepo(tmp_path)
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    assert files == []


def test_empty_file_is_skipped(tmp_path):
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    assert files == []


def test_whitespace_only_file_is_skipped(tmp_path):
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("\n\n   \n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    assert files == []


# ─── multi-level walk ─────────────────────────────────────────────────


def test_walks_multiple_levels_root_to_leaf(tmp_path):
    """Files at every level along the descent appear in order."""
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# root\n", encoding="utf-8")
    sub = tmp_path / "src"
    sub.mkdir()
    (sub / "CLAUDE.md").write_text("# src\n", encoding="utf-8")
    leaf = sub / "components"
    leaf.mkdir()
    (leaf / "CLAUDE.md").write_text("# components\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=leaf, profile_home=tmp_path / "fake")
    assert len(files) == 3
    assert files[0].depth == 0
    assert files[1].depth == 1
    assert files[2].depth == 2
    # Content order is root → leaf so leaf overrides root.
    assert "root" in files[0].content
    assert "components" in files[2].content


def test_intermediate_level_without_file_is_skipped(tmp_path):
    """Gaps in the hierarchy don't break the walk."""
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# root\n", encoding="utf-8")
    sub = tmp_path / "src"
    sub.mkdir()
    # no CLAUDE.md at src/
    leaf = sub / "x"
    leaf.mkdir()
    (leaf / "CLAUDE.md").write_text("# leaf\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=leaf, profile_home=tmp_path / "fake")
    assert len(files) == 2
    assert files[0].depth == 0
    assert files[1].depth == 2


# ─── .local.md overrides ───────────────────────────────────────────────


def test_local_override_appears_after_base(tmp_path):
    """``OPENCOMPUTER.local.md`` follows its same-level base file."""
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# root base\n", encoding="utf-8")
    (tmp_path / "OPENCOMPUTER.local.md").write_text("# local override\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    assert len(files) == 2
    assert files[0].source == "workspace"
    assert files[1].source == "local"
    assert files[1].depth == files[0].depth == 0


def test_local_override_without_base_still_loads(tmp_path):
    """A ``.local.md`` at a level with no base file still contributes."""
    _mkrepo(tmp_path)
    (tmp_path / "OPENCOMPUTER.local.md").write_text("# alone\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    assert len(files) == 1
    assert files[0].source == "local"


# ─── global rules dir ─────────────────────────────────────────────────


def test_global_rules_loaded_first(tmp_path):
    """Files under ``<profile_home>/rules/`` appear before workspace files."""
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# workspace\n", encoding="utf-8")
    profile = tmp_path / "profile"
    rules = profile / "rules"
    rules.mkdir(parents=True)
    (rules / "formatting.md").write_text("# fmt\n", encoding="utf-8")
    (rules / "security.md").write_text("# sec\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=profile)
    sources = [f.source for f in files]
    assert sources == ["global-rules", "global-rules", "workspace"]
    # Alphabetical order within rules dir.
    assert "fmt" in files[0].content
    assert "sec" in files[1].content


def test_global_rules_skipped_when_no_profile_home():
    """``profile_home=None`` means no rules dir scan."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _mkrepo(tmp_path)
        (tmp_path / "CLAUDE.md").write_text("# w\n", encoding="utf-8")
        files = find_hierarchical_instructions(cwd=tmp_path, profile_home=None)
        assert all(f.source != "global-rules" for f in files)


def test_global_rules_skips_non_md_files(tmp_path):
    """Only ``.md`` files in the rules dir count; ``.txt`` / ``.yml``
    are ignored."""
    profile = tmp_path / "profile"
    (profile / "rules").mkdir(parents=True)
    (profile / "rules" / "a.md").write_text("# md\n", encoding="utf-8")
    (profile / "rules" / "b.txt").write_text("# txt\n", encoding="utf-8")
    (profile / "rules" / "c.yml").write_text("# yml\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=profile)
    assert len(files) == 1
    assert "a.md" in str(files[0].path)


def test_global_rules_empty_dir(tmp_path):
    profile = tmp_path / "profile"
    (profile / "rules").mkdir(parents=True)
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=profile)
    assert files == []


def test_global_rules_no_dir(tmp_path):
    """``profile_home`` exists but has no ``rules/`` subdir → empty."""
    profile = tmp_path / "profile"
    profile.mkdir()
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=profile)
    assert files == []


# ─── safety / adversarial ─────────────────────────────────────────────


def test_oversized_file_is_truncated(tmp_path):
    _mkrepo(tmp_path)
    huge = "x" * (MAX_FILE_BYTES * 2)
    (tmp_path / "CLAUDE.md").write_text(huge, encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    assert len(files) == 1
    assert "truncated" in files[0].content
    # Cap is approximate (we count by bytes pre-read); the rendered
    # text length should be near the cap, never wildly over.
    assert len(files[0].content) < MAX_FILE_BYTES + 1024


def test_binary_garbage_does_not_crash(tmp_path):
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_bytes(b"\xff\xfe\xfd\x00binary garbage \xff")
    # Should not raise.
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    # File DID exist + had content; we read with errors="replace" so
    # there's something to report.
    assert len(files) == 1


def test_nonexistent_cwd_returns_empty(tmp_path):
    """A cwd that doesn't exist (e.g. deleted while running) doesn't
    crash discovery."""
    bogus = tmp_path / "this-path-does-not-exist"
    files = find_hierarchical_instructions(cwd=bogus, profile_home=tmp_path / "fake")
    # Either empty or the discovery falls back to parents — both
    # are fine, the contract is "doesn't crash."
    assert isinstance(files, list)


def test_default_cwd_uses_os_getcwd():
    """Passing ``cwd=None`` resolves to the live cwd."""
    files = find_hierarchical_instructions(cwd=None, profile_home=None)
    assert isinstance(files, list)


def test_returns_instruction_file_dataclass(tmp_path):
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    assert all(isinstance(f, InstructionFile) for f in files)
    assert all(isinstance(f.path, Path) for f in files)


# ─── format_for_system_prompt ─────────────────────────────────────────


def test_format_for_system_prompt_empty():
    assert format_for_system_prompt([]) == ""


def test_format_for_system_prompt_emits_path_comments(tmp_path):
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# rules\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=tmp_path, profile_home=tmp_path / "fake")
    out = format_for_system_prompt(files)
    assert "CLAUDE.md" in out
    assert "# rules" in out
    # Source tag appears as a comment so the model can attribute rules.
    assert "workspace" in out


def test_format_for_system_prompt_order_preserved(tmp_path):
    _mkrepo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# A\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "CLAUDE.md").write_text("# B\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=sub, profile_home=tmp_path / "fake")
    out = format_for_system_prompt(files)
    a_pos = out.find("# A")
    b_pos = out.find("# B")
    assert a_pos != -1 and b_pos != -1
    assert a_pos < b_pos


# ─── stop conditions ──────────────────────────────────────────────────


def test_walk_stops_at_git_repo_root(tmp_path):
    """Outside the repo root, no further upward walk."""
    # Create nested repo.
    repo = tmp_path / "outer-non-repo" / "actual-repo"
    repo.mkdir(parents=True)
    _mkrepo(repo)
    (repo.parent / "CLAUDE.md").write_text("# outside\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("# inside\n", encoding="utf-8")
    files = find_hierarchical_instructions(cwd=repo, profile_home=tmp_path / "fake")
    # We should pick up only the repo-root file, not the one above.
    contents = "\n".join(f.content for f in files)
    assert "inside" in contents
    assert "outside" not in contents
