"""Phase 14.F / Sub-project C — profile parity with Hermes.

Covers C1 (per-profile ``home/`` subdir + subprocess env scoping),
C2 (``~/.local/bin/<name>`` wrapper scripts), and C3 (per-profile
``SOUL.md`` personality injection). C4 (doctor checks) lives at the
bottom of this file.

These tests use the ``OPENCOMPUTER_HOME_ROOT`` env var hook to
point the profile root at ``tmp_path``, matching the convention
established in ``test_phase14a.py``.
"""

from __future__ import annotations

import os

import pytest


# Every test in this file touches HOME / XDG_* via
# _apply_profile_override. Register them with monkeypatch so teardown
# restores the pre-test values — otherwise downstream tests see
# nonexistent paths from our tmp_path and plugin discovery breaks.
@pytest.fixture(autouse=True)
def _preserve_home_env(monkeypatch):
    monkeypatch.setenv("HOME", os.environ.get("HOME", ""))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    yield


# ─── C1 — per-profile home/ subdir + subprocess env scoping ─────────


class TestProfileHomeDir:
    def test_profile_home_dir_returns_profile_scoped_path(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import create_profile, profile_home_dir

        create_profile("coder")
        expected = tmp_path / "profiles" / "coder" / "home"
        assert profile_home_dir("coder") == expected

    def test_create_profile_creates_home_subdir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import create_profile

        create_profile("coder")
        assert (tmp_path / "profiles" / "coder" / "home").is_dir()


class TestScopeSubprocessEnv:
    def test_scope_subprocess_env_sets_home_xdg_when_named_profile_active(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import create_profile, scope_subprocess_env, write_active_profile

        create_profile("coder")
        write_active_profile("coder")

        env = {"HOME": "/old/home", "PATH": "/usr/bin"}
        result = scope_subprocess_env(env)

        profile_home = tmp_path / "profiles" / "coder" / "home"
        assert result["HOME"] == str(profile_home)
        assert result["XDG_CONFIG_HOME"] == str(profile_home / ".config")
        assert result["XDG_DATA_HOME"] == str(profile_home / ".local" / "share")

    def test_scope_subprocess_env_leaves_default_profile_alone(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import scope_subprocess_env

        env = {"HOME": "/original/home", "PATH": "/usr/bin"}
        result = scope_subprocess_env(env)

        # No named active profile → env is returned unchanged.
        assert result["HOME"] == "/original/home"
        assert "XDG_CONFIG_HOME" not in result
        assert "XDG_DATA_HOME" not in result

    def test_scope_subprocess_env_preserves_unrelated_env_keys(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import create_profile, scope_subprocess_env, write_active_profile

        create_profile("coder")
        write_active_profile("coder")

        env = {
            "HOME": "/old/home",
            "PATH": "/usr/bin:/bin",
            "CUSTOM_VAR": "hello",
            "LANG": "en_US.UTF-8",
        }
        result = scope_subprocess_env(env)

        assert result["PATH"] == "/usr/bin:/bin"
        assert result["CUSTOM_VAR"] == "hello"
        assert result["LANG"] == "en_US.UTF-8"

    def test_scope_subprocess_env_default_argument_copies_os_environ(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        from opencomputer.profiles import create_profile, scope_subprocess_env, write_active_profile

        create_profile("coder")
        write_active_profile("coder")

        monkeypatch.setenv("SOME_UNIQUE_ENV_MARKER_FOR_TEST", "x1y2z3")
        result = scope_subprocess_env()  # default = copy of os.environ

        assert result.get("SOME_UNIQUE_ENV_MARKER_FOR_TEST") == "x1y2z3"
        assert result["HOME"] == str(tmp_path / "profiles" / "coder" / "home")


# ─── C1 — CLI env-scoping integration ────────────────────────────────


class TestApplyProfileOverrideScopesEnv:
    """Integration: ``main()`` calls ``_apply_profile_override()`` which must,
    after setting ``OPENCOMPUTER_HOME``, replace ``os.environ`` in-place with
    the scoped env so every downstream subprocess inherits HOME/XDG paths
    pointing at the profile's ``home/`` subdir.
    """

    def test_apply_profile_override_scopes_env_for_named_profile(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat"])

        # Create the profile with home/ subdir.
        from opencomputer.profiles import create_profile

        create_profile("coder")

        from opencomputer.cli import _apply_profile_override

        _apply_profile_override()

        assert os.environ["OPENCOMPUTER_HOME"] == str(tmp_path / "profiles" / "coder")
        expected_home = tmp_path / "profiles" / "coder" / "home"
        assert os.environ["HOME"] == str(expected_home)
        assert os.environ["XDG_CONFIG_HOME"] == str(expected_home / ".config")
        assert os.environ["XDG_DATA_HOME"] == str(expected_home / ".local" / "share")

    def test_apply_profile_override_does_not_scope_env_for_default(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
        original_home = "/tmp/original-home-for-test-env"
        monkeypatch.setenv("HOME", original_home)
        monkeypatch.setattr(sys, "argv", ["opencomputer", "chat"])

        from opencomputer.cli import _apply_profile_override

        _apply_profile_override()

        assert "OPENCOMPUTER_HOME" not in os.environ
        # HOME was not scoped.
        assert os.environ["HOME"] == original_home


# ─── C2 — ~/.local/bin/<name> wrapper scripts ───────────────────────


class TestWrapperScripts:
    def test_wrapper_path_returns_local_bin_path(self, tmp_path, monkeypatch):
        from pathlib import Path

        monkeypatch.setenv("HOME", str(tmp_path))
        # Path.home() reads HOME on POSIX; these tests only run on macOS/Linux.
        from opencomputer.profiles import wrapper_path

        assert wrapper_path("coder") == Path(tmp_path) / ".local" / "bin" / "coder"

    def test_create_profile_writes_wrapper_on_unix(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "linux")

        from opencomputer.profiles import create_profile

        create_profile("coder")
        wrapper = tmp_path / ".local" / "bin" / "coder"
        assert wrapper.exists()

        import stat

        mode = wrapper.stat().st_mode
        assert mode & stat.S_IXUSR
        assert mode & stat.S_IXGRP
        assert mode & stat.S_IXOTH

    def test_wrapper_script_content_invokes_opencomputer_with_profile_flag(
        self, tmp_path, monkeypatch
    ):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "linux")

        from opencomputer.profiles import create_profile

        create_profile("coder")
        wrapper = tmp_path / ".local" / "bin" / "coder"
        content = wrapper.read_text()
        assert content.startswith("#!/")
        assert "opencomputer" in content
        assert "-p coder" in content or "--profile coder" in content or "--profile=coder" in content
        assert '"$@"' in content

    def test_create_profile_does_not_overwrite_existing_wrapper(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "linux")

        from opencomputer.profiles import create_profile, delete_profile

        # Pre-create wrapper with custom content.
        (tmp_path / ".local" / "bin").mkdir(parents=True)
        wrapper = tmp_path / ".local" / "bin" / "coder"
        wrapper.write_text("#!/bin/bash\necho custom wrapper\n")
        wrapper.chmod(0o755)

        create_profile("coder")

        # Pre-existing wrapper is untouched.
        assert wrapper.read_text() == "#!/bin/bash\necho custom wrapper\n"

        # Cleanup so profile-delete doesn't blow up.
        delete_profile("coder")

    def test_delete_profile_removes_wrapper(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "linux")

        from opencomputer.profiles import create_profile, delete_profile

        create_profile("coder")
        wrapper = tmp_path / ".local" / "bin" / "coder"
        assert wrapper.exists()

        delete_profile("coder")
        assert not wrapper.exists()

    def test_delete_profile_without_existing_wrapper_is_silent(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "linux")

        from opencomputer.profiles import create_profile, delete_profile

        create_profile("coder")
        wrapper = tmp_path / ".local" / "bin" / "coder"
        wrapper.unlink()  # manually remove wrapper pre-delete

        # Delete should not blow up despite the missing wrapper.
        delete_profile("coder")

    def test_create_profile_skips_wrapper_on_windows(self, tmp_path, monkeypatch):
        import sys

        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(sys, "platform", "win32")

        from opencomputer.profiles import create_profile

        create_profile("coder")
        wrapper = tmp_path / ".local" / "bin" / "coder"
        assert not wrapper.exists()


# ─── C3 — SOUL.md per-profile personality injection ─────────────────


class TestSoulMdSeed:
    def test_create_profile_seeds_soul_md_with_default_content(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        from opencomputer.profiles import create_profile

        create_profile("coder")
        soul = tmp_path / "profiles" / "coder" / "SOUL.md"
        assert soul.exists()
        content = soul.read_text()
        # Default template should reference the profile name + identity framing.
        assert "coder" in content
        assert "SOUL" in content

    def test_create_profile_does_not_overwrite_existing_soul_md(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        # Pre-create the profile dir + SOUL.md with custom content, then
        # call the internal seeder helper directly to verify idempotence.
        profile_dir = tmp_path / "profiles" / "coder"
        profile_dir.mkdir(parents=True)
        soul = profile_dir / "SOUL.md"
        soul.write_text("# My custom soul\nDon't overwrite me.\n")

        from opencomputer.profiles import _maybe_write_soul_md

        _maybe_write_soul_md("coder")
        assert soul.read_text() == "# My custom soul\nDon't overwrite me.\n"


class TestMemoryManagerReadSoul:
    def test_memory_manager_read_soul_returns_empty_when_absent(self, tmp_path):
        from opencomputer.agent.memory import MemoryManager

        mem = MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            skills_path=tmp_path / "skills",
            user_path=tmp_path / "USER.md",
        )
        # Default soul_path is <declarative_path.parent>/SOUL.md which doesn't
        # exist in this fresh tmp — should return "".
        assert mem.read_soul() == ""

    def test_memory_manager_read_soul_reads_from_file(self, tmp_path):
        from opencomputer.agent.memory import MemoryManager

        soul = tmp_path / "SOUL.md"
        soul.write_text("# SOUL\nI am coder.\n")
        mem = MemoryManager(
            declarative_path=tmp_path / "MEMORY.md",
            skills_path=tmp_path / "skills",
            user_path=tmp_path / "USER.md",
            soul_path=soul,
        )
        assert mem.read_soul() == "# SOUL\nI am coder.\n"


class TestPromptBuilderSoulInjection:
    def test_prompt_builder_injects_soul_when_provided(self, tmp_path):
        from opencomputer.agent.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        out = builder.build(
            soul="# SOUL\nI am coder.\n",
            skills=None,
            declarative_memory="",
            user_profile="",
        )
        assert "I am coder" in out
        assert "Profile identity" in out

    def test_prompt_builder_skips_soul_section_when_empty(self, tmp_path):
        from opencomputer.agent.prompt_builder import PromptBuilder

        builder = PromptBuilder()
        out = builder.build(
            soul="",
            skills=None,
            declarative_memory="",
            user_profile="",
        )
        # When soul is empty the whole section header shouldn't appear.
        assert "Profile identity" not in out


# ─── C4 — opencomputer doctor profile artifact checks ───────────────


class TestDoctorProfileChecks:
    def test_doctor_warns_when_profile_home_subdir_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        from opencomputer.profiles import create_profile, write_active_profile

        create_profile("coder")
        write_active_profile("coder")

        # Remove the home/ subdir to simulate the user deleting it.
        import shutil

        shutil.rmtree(tmp_path / "profiles" / "coder" / "home")

        from opencomputer.doctor import _check_profile_artifacts

        checks = _check_profile_artifacts()
        names = [c.name for c in checks]
        missing = [c for c in checks if "home" in c.name.lower()]
        assert missing, f"no home/ artifact check produced (names={names})"
        assert missing[0].status == "warn"

    def test_doctor_warns_when_profile_soul_md_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        from opencomputer.profiles import create_profile, write_active_profile

        create_profile("coder")
        write_active_profile("coder")

        (tmp_path / "profiles" / "coder" / "SOUL.md").unlink()

        from opencomputer.doctor import _check_profile_artifacts

        checks = _check_profile_artifacts()
        missing = [c for c in checks if "SOUL" in c.name or "soul" in c.name.lower()]
        assert missing, f"no SOUL.md artifact check produced (names={[c.name for c in checks]})"
        assert missing[0].status == "warn"

    def test_doctor_skips_profile_checks_when_default_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path))
        # No write_active_profile → default is active.

        from opencomputer.doctor import _check_profile_artifacts

        checks = _check_profile_artifacts()
        # Default profile must produce no checks (or all skip).
        assert all(c.status == "skip" for c in checks), (
            f"default profile should skip all profile-artifact checks "
            f"(got {[(c.name, c.status) for c in checks]})"
        )
