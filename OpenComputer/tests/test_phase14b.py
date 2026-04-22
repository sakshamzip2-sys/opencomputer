"""Phase 14.B — `opencomputer profile` CLI subapp tests."""

from __future__ import annotations

from typer.testing import CliRunner


def _runner():
    return CliRunner()


def _app_with_isolated_home(tmp_path, monkeypatch):
    """Set up isolated profile root + return the profile_app for testing."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    # Reload cli_profile so it picks up the new OPENCOMPUTER_HOME_ROOT
    import importlib

    from opencomputer import cli_profile

    importlib.reload(cli_profile)
    return cli_profile.profile_app


class TestProfileCreate:
    def test_create_basic(self, tmp_path, monkeypatch):
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["create", "coder"])
        assert result.exit_code == 0
        assert (tmp_path / "profiles" / "coder").is_dir()

    def test_create_clone_config(self, tmp_path, monkeypatch):
        # Seed a source profile with a config.yaml
        (tmp_path / "profiles" / "src").mkdir(parents=True)
        (tmp_path / "profiles" / "src" / "config.yaml").write_text(
            "model:\n  provider: anthropic\n"
        )
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["create", "coder2", "--clone-from", "src"])
        assert result.exit_code == 0
        dest = tmp_path / "profiles" / "coder2" / "config.yaml"
        assert dest.exists()
        assert "anthropic" in dest.read_text()

    def test_create_clone_all(self, tmp_path, monkeypatch):
        # Seed with config + MEMORY.md + subdir
        src = tmp_path / "profiles" / "src"
        src.mkdir(parents=True)
        (src / "config.yaml").write_text("x: 1\n")
        (src / "MEMORY.md").write_text("entry\n")
        (src / "skills").mkdir()
        (src / "skills" / "dummy.md").write_text("skill\n")
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["create", "coder3", "--clone-from", "src", "--clone-all"])
        assert result.exit_code == 0
        dest = tmp_path / "profiles" / "coder3"
        assert (dest / "MEMORY.md").read_text() == "entry\n"
        assert (dest / "skills" / "dummy.md").read_text() == "skill\n"

    def test_create_refuses_default_name(self, tmp_path, monkeypatch):
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["create", "default"])
        assert result.exit_code != 0

    def test_create_refuses_invalid_name(self, tmp_path, monkeypatch):
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["create", "BAD NAME"])
        assert result.exit_code != 0

    def test_create_refuses_existing(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["create", "coder"])
        assert result.exit_code != 0


class TestProfileList:
    def test_list_empty(self, tmp_path, monkeypatch):
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["list"])
        assert result.exit_code == 0
        # Should include the default profile row even when no named profiles exist
        assert "default" in result.stdout.lower()

    def test_list_shows_active_marker(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "profiles" / "stocks").mkdir(parents=True)
        (tmp_path).mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("coder\n")
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["list"])
        assert result.exit_code == 0
        assert "coder" in result.stdout
        assert "stocks" in result.stdout
        # The active marker is some visible glyph; just check coder has some nearby marker
        active_line = [ln for ln in result.stdout.splitlines() if "coder" in ln]
        assert any(
            "◆" in ln or "*" in ln or "->" in ln or "active" in ln.lower() for ln in active_line
        )


class TestProfileUse:
    def test_use_writes_sticky_file(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["use", "coder"])
        assert result.exit_code == 0
        assert (tmp_path / "active_profile").read_text().strip() == "coder"

    def test_use_default_clears_sticky(self, tmp_path, monkeypatch):
        (tmp_path / "active_profile").write_text("coder\n")
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["use", "default"])
        assert result.exit_code == 0
        assert not (tmp_path / "active_profile").exists()

    def test_use_refuses_nonexistent(self, tmp_path, monkeypatch):
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["use", "missing"])
        assert result.exit_code != 0


class TestProfileDelete:
    def test_delete_refuses_default(self, tmp_path, monkeypatch):
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["delete", "default", "--yes"])
        assert result.exit_code != 0

    def test_delete_with_yes_removes_dir(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "profiles" / "coder" / "MEMORY.md").write_text("entry\n")
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["delete", "coder", "--yes"])
        assert result.exit_code == 0
        assert not (tmp_path / "profiles" / "coder").exists()

    def test_delete_clears_sticky_if_deleted_was_active(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "active_profile").write_text("coder\n")
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["delete", "coder", "--yes"])
        assert result.exit_code == 0
        assert not (tmp_path / "active_profile").exists()


class TestProfileRename:
    def test_rename_moves_directory(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "old").mkdir(parents=True)
        (tmp_path / "profiles" / "old" / "MEMORY.md").write_text("entry\n")
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["rename", "old", "new"])
        assert result.exit_code == 0
        assert not (tmp_path / "profiles" / "old").exists()
        assert (tmp_path / "profiles" / "new" / "MEMORY.md").read_text() == "entry\n"

    def test_rename_updates_active_if_renamed_was_active(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "old").mkdir(parents=True)
        (tmp_path / "active_profile").write_text("old\n")
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["rename", "old", "new"])
        assert result.exit_code == 0
        assert (tmp_path / "active_profile").read_text().strip() == "new"

    def test_rename_warns_about_honcho(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "old").mkdir(parents=True)
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["rename", "old", "new"])
        assert result.exit_code == 0
        # Warning message should mention Honcho or memory continuity
        assert (
            "honcho" in result.stdout.lower()
            or "continuity" in result.stdout.lower()
            or "memory" in result.stdout.lower()
        )


class TestProfilePath:
    def test_path_default(self, tmp_path, monkeypatch):
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["path"])
        assert result.exit_code == 0
        assert str(tmp_path) in result.stdout

    def test_path_named(self, tmp_path, monkeypatch):
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        app = _app_with_isolated_home(tmp_path, monkeypatch)
        result = _runner().invoke(app, ["path", "coder"])
        assert result.exit_code == 0
        assert str(tmp_path / "profiles" / "coder") in result.stdout
