"""Tests for the Skills Hub installer (fetch + scan + validate + write)."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from opencomputer.skills_hub.installer import Installer, InstallError, InstallResult
from plugin_sdk.skill_source import SkillBundle, SkillMeta

_VALID_SKILL_MD = (
    "---\n"
    "name: example-readme-summarizer\n"
    "description: Summarize a project README into 5 bullets covering goal install usage workflow gotchas\n"
    "version: 0.1.0\n"
    "---\n"
    "\n"
    "# Example\n"
)


def _allow_guard():
    """A skills_guard stub that always passes."""
    g = Mock()
    g.scan_skill.return_value = SimpleNamespace(
        verdict="safe", trust_level="trusted", findings=[]
    )
    g.should_allow_install.return_value = (True, "Allowed")
    return g


def _block_guard():
    g = Mock()
    g.scan_skill.return_value = SimpleNamespace(
        verdict="dangerous", trust_level="community",
        findings=[SimpleNamespace(pattern_id="P1", severity="critical")],
    )
    g.should_allow_install.return_value = (False, "Blocked: dangerous verdict")
    return g


def _ask_guard():
    g = Mock()
    g.scan_skill.return_value = SimpleNamespace(
        verdict="caution", trust_level="community", findings=[]
    )
    g.should_allow_install.return_value = (None, "Requires confirmation")
    return g


@pytest.fixture
def fake_router():
    router = Mock()
    bundle = SkillBundle(
        identifier="well-known/example-readme-summarizer",
        skill_md=_VALID_SKILL_MD,
        files={},
    )
    meta = SkillMeta(
        identifier="well-known/example-readme-summarizer",
        name="example-readme-summarizer",
        description="Summarize a project README into 5 bullets covering goal install usage workflow gotchas",
        source="well-known",
        version="0.1.0",
    )
    router.fetch.return_value = bundle
    router.inspect.return_value = meta
    return router


@pytest.fixture
def installer(tmp_path, fake_router):
    return Installer(
        router=fake_router,
        skills_guard=_allow_guard(),
        hub_root=tmp_path / ".hub",
    )


def test_install_writes_skill_md_to_disk(installer, tmp_path):
    result = installer.install("well-known/example-readme-summarizer")
    expected = tmp_path / ".hub" / "well-known" / "example-readme-summarizer" / "SKILL.md"
    assert expected.exists()
    assert "name: example-readme-summarizer" in expected.read_text()
    assert isinstance(result, InstallResult)
    assert result.identifier == "well-known/example-readme-summarizer"


def test_install_records_in_lockfile(installer, tmp_path):
    installer.install("well-known/example-readme-summarizer")
    lockfile = tmp_path / ".hub" / "lockfile.json"
    assert lockfile.exists()
    data = json.loads(lockfile.read_text())
    assert len(data["entries"]) == 1
    assert data["entries"][0]["identifier"] == "well-known/example-readme-summarizer"


def test_install_appends_audit_entry(installer, tmp_path):
    installer.install("well-known/example-readme-summarizer")
    audit = tmp_path / ".hub" / "audit.log"
    assert audit.exists()
    line = audit.read_text().strip()
    assert "install" in line
    assert "well-known/example-readme-summarizer" in line


def test_install_rejects_invalid_frontmatter(tmp_path, fake_router):
    bad_bundle = SkillBundle(
        identifier="well-known/bad",
        skill_md="no frontmatter at all",
        files={},
    )
    fake_router.fetch.return_value = bad_bundle
    fake_router.inspect.return_value = SkillMeta(
        identifier="well-known/bad", name="bad",
        description="x" * 30, source="well-known",
    )
    inst = Installer(router=fake_router, skills_guard=_allow_guard(), hub_root=tmp_path / ".hub")
    with pytest.raises(InstallError, match="frontmatter"):
        inst.install("well-known/bad")


def test_install_blocked_by_skills_guard(tmp_path, fake_router):
    inst = Installer(router=fake_router, skills_guard=_block_guard(), hub_root=tmp_path / ".hub")
    with pytest.raises(InstallError, match="skills_guard blocked"):
        inst.install("well-known/example-readme-summarizer")
    audit = tmp_path / ".hub" / "audit.log"
    assert audit.exists()
    assert "scan_blocked" in audit.read_text()
    # Skill dir must NOT exist after block
    skill_dir = tmp_path / ".hub" / "well-known" / "example-readme-summarizer"
    assert not skill_dir.exists()


def test_install_ask_decision_is_blocked_in_automated_path(tmp_path, fake_router):
    inst = Installer(router=fake_router, skills_guard=_ask_guard(), hub_root=tmp_path / ".hub")
    with pytest.raises(InstallError, match="skills_guard blocked"):
        inst.install("well-known/example-readme-summarizer")


def test_install_ask_decision_can_be_forced(tmp_path, fake_router):
    """force=True bypasses the ask gate — but only when policy allows it."""
    g = Mock()
    g.scan_skill.return_value = SimpleNamespace(
        verdict="caution", trust_level="community", findings=[]
    )
    # When force=True, real should_allow_install would return True;
    # simulate that contract here.
    g.should_allow_install.return_value = (True, "Force-installed despite caution verdict")
    inst = Installer(router=fake_router, skills_guard=g, hub_root=tmp_path / ".hub")
    result = inst.install("well-known/example-readme-summarizer", force=True)
    assert result.identifier == "well-known/example-readme-summarizer"


def test_install_unknown_identifier_raises(tmp_path, fake_router):
    fake_router.fetch.return_value = None
    fake_router.inspect.return_value = None
    inst = Installer(router=fake_router, skills_guard=_allow_guard(), hub_root=tmp_path / ".hub")
    with pytest.raises(InstallError, match="not found"):
        inst.install("well-known/nope")


def test_uninstall_removes_files_and_lockfile_entry(installer, tmp_path):
    installer.install("well-known/example-readme-summarizer")
    skill_dir = tmp_path / ".hub" / "well-known" / "example-readme-summarizer"
    assert skill_dir.exists()
    installer.uninstall("well-known/example-readme-summarizer")
    assert not skill_dir.exists()
    data = json.loads((tmp_path / ".hub" / "lockfile.json").read_text())
    assert data["entries"] == []


def test_double_install_replaces_lockfile_entry(installer, tmp_path):
    installer.install("well-known/example-readme-summarizer")
    installer.install("well-known/example-readme-summarizer")
    data = json.loads((tmp_path / ".hub" / "lockfile.json").read_text())
    assert len(data["entries"]) == 1


def test_install_writes_extra_files_from_bundle(tmp_path, fake_router):
    fake_router.fetch.return_value = SkillBundle(
        identifier="well-known/with-helper",
        skill_md="---\nname: with-helper\ndescription: A skill that ships a helper script alongside its prose body\n---\n",
        files={"helper.py": "def x(): return 1\n"},
    )
    fake_router.inspect.return_value = SkillMeta(
        identifier="well-known/with-helper",
        name="with-helper",
        description="A skill that ships a helper script alongside its prose body",
        source="well-known",
    )
    inst = Installer(router=fake_router, skills_guard=_allow_guard(), hub_root=tmp_path / ".hub")
    inst.install("well-known/with-helper")
    helper = tmp_path / ".hub" / "well-known" / "with-helper" / "helper.py"
    assert helper.exists()
    assert helper.read_text().startswith("def x")


def test_install_rejects_path_traversal_identifier(installer):
    with pytest.raises(InstallError, match="invalid identifier"):
        installer.install("../../etc/passwd")


def test_install_rejects_identifier_without_slash(installer):
    with pytest.raises(InstallError, match="invalid identifier"):
        installer.install("just-a-name")


def test_uninstall_unknown_identifier_raises(installer):
    with pytest.raises(InstallError, match="not installed"):
        installer.uninstall("well-known/never-installed")


def test_install_cleans_up_staging_on_block(tmp_path, fake_router):
    inst = Installer(router=fake_router, skills_guard=_block_guard(), hub_root=tmp_path / ".hub")
    try:
        inst.install("well-known/example-readme-summarizer")
    except InstallError:
        pass
    # Staging dir must be cleaned up
    staging = tmp_path / ".hub" / "_staging"
    if staging.exists():
        # If it exists, it should be empty
        assert not any(staging.rglob("SKILL.md"))
