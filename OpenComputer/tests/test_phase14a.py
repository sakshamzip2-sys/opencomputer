"""Phase 14.A — per-profile directory + pre-import -p flag routing."""

from __future__ import annotations

from pathlib import Path


class TestProfileValidation:
    def test_default_profile_lives_at_home_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
        from opencomputer.agent.config import _home

        assert _home() == tmp_path

    def test_validate_profile_name_accepts_valid(self):
        from opencomputer.profiles import validate_profile_name

        validate_profile_name("coder")
        validate_profile_name("stocks_v2")
        validate_profile_name("side-hustle")
        validate_profile_name("my123")

    def test_validate_profile_name_rejects_uppercase(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        with pytest.raises(ProfileNameError):
            validate_profile_name("UPPER")

    def test_validate_profile_name_rejects_spaces(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        with pytest.raises(ProfileNameError):
            validate_profile_name("has space")

    def test_validate_profile_name_rejects_empty(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        with pytest.raises(ProfileNameError):
            validate_profile_name("")

    def test_validate_profile_name_rejects_dots(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        with pytest.raises(ProfileNameError):
            validate_profile_name("dot.profile")

    def test_validate_profile_name_rejects_reserved(self):
        import pytest

        from opencomputer.profiles import ProfileNameError, validate_profile_name

        for name in ["default", "presets", "wrappers", "plugins", "profiles", "skills"]:
            with pytest.raises(ProfileNameError):
                validate_profile_name(name)

    def test_get_profile_dir_default_returns_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import get_profile_dir

        assert get_profile_dir(None) == tmp_path
        assert get_profile_dir("default") == tmp_path

    def test_get_profile_dir_named(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import get_profile_dir

        assert get_profile_dir("coder") == tmp_path / "profiles" / "coder"

    def test_list_profiles_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import list_profiles

        assert list_profiles() == []

    def test_list_profiles_shows_created(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        (tmp_path / "profiles" / "coder").mkdir(parents=True)
        (tmp_path / "profiles" / "stocks").mkdir(parents=True)
        from opencomputer.profiles import list_profiles

        assert list_profiles() == ["coder", "stocks"]

    def test_active_profile_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import read_active_profile, write_active_profile

        assert read_active_profile() is None
        write_active_profile("coder")
        assert read_active_profile() == "coder"
        write_active_profile(None)
        assert read_active_profile() is None

    def test_write_active_profile_default_clears(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import read_active_profile, write_active_profile

        write_active_profile("coder")
        write_active_profile("default")  # clears
        assert read_active_profile() is None


class TestProfileFlagRouting:
    """Tests for _apply_profile_override. Each test must import fresh to avoid module caching."""

    def _reload_cli(self):
        import importlib

        import opencomputer.cli as cli_mod

        return importlib.reload(cli_mod)

    def test_p_flag_sets_opencomputer_home(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat"])
        self._reload_cli()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "coder")

    def test_long_form_profile_flag(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "--profile=stocks", "chat"])
        self._reload_cli()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "stocks")

    def test_long_form_profile_flag_spaced(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "--profile", "stocks", "chat"])
        self._reload_cli()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "stocks")

    def test_sticky_active_profile_applied_when_no_flag(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("coder\n")
        monkeypatch.setattr(sys, "argv", ["opencomputer", "chat"])
        self._reload_cli()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "coder")

    def test_flag_beats_sticky(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "active_profile").write_text("coder\n")
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "personal", "chat"])
        self._reload_cli()
        import os

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "personal")

    def test_flag_stripped_from_argv(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat", "--plan"])
        self._reload_cli()
        assert sys.argv == ["opencomputer", "chat", "--plan"]

    def test_invalid_profile_name_falls_back_to_default(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "BAD NAME", "chat"])
        self._reload_cli()
        import os

        # Bad name = fallback to default = no OPENCOMPUTER_HOME set (or unchanged)
        assert "OPENCOMPUTER_HOME" not in os.environ or os.environ["OPENCOMPUTER_HOME"] == str(
            tmp_path
        )

    def test_p_flag_missing_value_strips_flag(self, tmp_path, monkeypatch):
        """Issue 2 regression: -p as last arg must strip flag, not leak to Typer."""
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p"])
        self._reload_cli()
        import os

        assert "-p" not in sys.argv
        assert "OPENCOMPUTER_HOME" not in os.environ

    def test_explicit_flag_beats_parent_env_var(self, tmp_path, monkeypatch):
        """Issue 5 regression: -p flag must override OPENCOMPUTER_HOME pre-set by parent."""
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        # Simulate parent process having OPENCOMPUTER_HOME set to something unrelated
        monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / "some-other-place"))
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat"])
        self._reload_cli()
        import os

        # Flag must win, even though OPENCOMPUTER_HOME was pre-set
        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "coder")

    def test_profile_empty_value_is_treated_as_default(self, tmp_path, monkeypatch):
        """Issue 3 regression: --profile= (empty value) must not leak as falsy profile."""
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "--profile=", "chat"])
        self._reload_cli()
        import os

        # Empty value → fallback to default → no OPENCOMPUTER_HOME set
        assert "OPENCOMPUTER_HOME" not in os.environ
