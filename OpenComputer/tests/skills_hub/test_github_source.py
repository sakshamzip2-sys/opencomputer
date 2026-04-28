"""Tests for GitHubSource. Filesystem-based — git clone is mocked or bypassed."""
import subprocess
from pathlib import Path

from opencomputer.skills_hub.sources.github import GitHubSource


def _seed_fake_repo(target: Path, skill_name: str = "demo-skill") -> None:
    """Pre-create a fake cloned repo with one valid SKILL.md so the
    'already cloned' branch fires and no git is needed."""
    skill_dir = target / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: A demo skill from a tapped GitHub repo for testing fetches\nversion: 1.0.0\n---\n# {skill_name}\n"
    )


def test_github_source_name_uses_repo(tmp_path):
    src = GitHubSource(repo="alice/cool-skills", clone_root=tmp_path / "taps")
    assert src.name == "alice/cool-skills"


def test_invalid_repo_raises():
    import pytest
    with pytest.raises(ValueError):
        GitHubSource(repo="invalid_repo_no_slash", clone_root=Path("/tmp"))


def test_search_walks_cloned_skills(tmp_path):
    """Pre-seed the clone dir so _ensure_cloned is a no-op."""
    src = GitHubSource(repo="alice/cool-skills", clone_root=tmp_path / "taps")
    clone_dir = tmp_path / "taps" / "alice" / "cool-skills"
    clone_dir.mkdir(parents=True, exist_ok=True)
    _seed_fake_repo(clone_dir)
    results = src.search("demo")
    assert len(results) == 1
    assert results[0].name == "demo-skill"
    assert results[0].identifier == "alice/cool-skills/demo-skill"


def test_inspect_returns_meta(tmp_path):
    src = GitHubSource(repo="alice/cool", clone_root=tmp_path / "taps")
    clone_dir = tmp_path / "taps" / "alice" / "cool"
    clone_dir.mkdir(parents=True, exist_ok=True)
    _seed_fake_repo(clone_dir)
    meta = src.inspect("alice/cool/demo-skill")
    assert meta is not None
    assert meta.name == "demo-skill"
    assert meta.version == "1.0.0"


def test_fetch_returns_bundle(tmp_path):
    src = GitHubSource(repo="alice/cool", clone_root=tmp_path / "taps")
    clone_dir = tmp_path / "taps" / "alice" / "cool"
    clone_dir.mkdir(parents=True, exist_ok=True)
    _seed_fake_repo(clone_dir)
    bundle = src.fetch("alice/cool/demo-skill")
    assert bundle is not None
    assert "demo-skill" in bundle.skill_md


def test_clone_invokes_git(tmp_path, monkeypatch):
    """If clone dir is missing, GitHubSource shells out to git clone."""
    src = GitHubSource(repo="alice/cool", clone_root=tmp_path / "taps")
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        Path(args[-1]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    src._ensure_cloned()
    assert any("clone" in c for c in calls)


def test_skipped_skill_with_invalid_frontmatter(tmp_path):
    """A SKILL.md that fails the validator is silently skipped."""
    src = GitHubSource(repo="alice/cool", clone_root=tmp_path / "taps")
    clone_dir = tmp_path / "taps" / "alice" / "cool"
    bad = clone_dir / "skills" / "bad" / "SKILL.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("not a valid frontmatter")
    good = clone_dir / "skills" / "good" / "SKILL.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text(
        "---\nname: good\ndescription: A valid skill alongside a broken one in the same repo\n---\n"
    )
    results = src.search("")
    names = {r.name for r in results}
    assert "good" in names
    assert "bad" not in names
