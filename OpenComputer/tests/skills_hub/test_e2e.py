"""End-to-end test: full lifecycle against the bundled well-known source."""
import json
from pathlib import Path

from typer.testing import CliRunner

from opencomputer.cli_skills import app

runner = CliRunner()


def test_full_lifecycle_against_bundled_well_known(monkeypatch, tmp_path):
    """Search → inspect → install → list → uninstall → audit, all hitting
    the real bundled well_known_manifest.json content."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    # 1. Search well-known
    r = runner.invoke(app, ["search", "api"])
    assert r.exit_code == 0
    assert "api-design" in r.stdout

    # 2. Inspect
    r = runner.invoke(app, ["inspect", "well-known/api-design"])
    assert r.exit_code == 0
    assert "api-design" in r.stdout

    # 3. Install
    r = runner.invoke(app, ["install", "well-known/api-design", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert "Installed" in r.stdout

    # 4. List shows it
    r = runner.invoke(app, ["installed"])
    assert r.exit_code == 0
    assert "api-design" in r.stdout

    # 5. Skill files are on disk where the loader expects them
    skill_md = (
        tmp_path / "default" / "skills" / ".hub"
        / "well-known" / "api-design" / "SKILL.md"
    )
    assert skill_md.exists()

    # 6. Audit log has install + verdict
    r = runner.invoke(app, ["audit"])
    assert r.exit_code == 0
    assert "install" in r.stdout
    assert "verdict=" in r.stdout

    # 7. Uninstall
    r = runner.invoke(app, ["uninstall", "well-known/api-design", "--yes"])
    assert r.exit_code == 0

    # 8. Installed list is empty
    r = runner.invoke(app, ["installed"])
    assert "api-design" not in r.stdout

    # 9. Audit shows both events
    r = runner.invoke(app, ["audit"])
    assert "install" in r.stdout
    assert "uninstall" in r.stdout


def test_loader_picks_up_hub_installed_skill(monkeypatch, tmp_path):
    """After install, MemoryManager.list_skills sees the hub skill."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner.invoke(app, ["install", "well-known/api-design", "--yes"])

    # Use MemoryManager directly to confirm loader recursion works
    from opencomputer.agent.memory import MemoryManager

    skills_path = tmp_path / "default" / "skills"
    mgr = MemoryManager(
        declarative_path=tmp_path / "default" / "MEMORY.md",
        skills_path=skills_path,
        bundled_skills_paths=[],  # exclude bundled to verify hub-only discovery
    )
    skills = mgr.list_skills()
    names = {s.name for s in skills}
    assert "api-design" in names, (
        f"hub-installed skill not found in list_skills(); got {names}"
    )


def test_blocked_install_does_not_pollute_state(monkeypatch, tmp_path):
    """If Skills Guard blocks an install, no skill dir, no lockfile entry."""
    from types import SimpleNamespace
    from unittest.mock import patch

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    # Patch should_allow_install to deny
    with patch("opencomputer.skills_guard.should_allow_install") as mock_decision:
        mock_decision.return_value = (False, "Test-blocked: simulated dangerous verdict")
        # Also patch scan_skill to return a dangerous verdict
        with patch("opencomputer.skills_guard.scan_skill") as mock_scan:
            mock_scan.return_value = SimpleNamespace(
                verdict="dangerous", trust_level="community", findings=[]
            )
            r = runner.invoke(app, ["install", "well-known/api-design", "--yes"])
            assert r.exit_code != 0
            assert "blocked" in r.stdout.lower() or "Install failed" in r.stdout

    # No skill dir
    skill_dir = tmp_path / "default" / "skills" / ".hub" / "well-known" / "api-design"
    assert not skill_dir.exists()
    # No lockfile entry
    lockfile_path = tmp_path / "default" / "skills" / ".hub" / "lockfile.json"
    if lockfile_path.exists():
        data = json.loads(lockfile_path.read_text())
        assert data["entries"] == []
