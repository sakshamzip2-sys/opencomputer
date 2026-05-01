"""After ``_apply_profile_override``, ``Path.home()`` in the parent
process is unchanged.

Regression test for the architectural fix that eliminated parent-process
HOME mutation. The 16+ ``Path.home()`` callsites in production code
rely on this invariant — snapshot tarball destinations, the
``~/.local/bin`` wrapper path, the workspace walk-up's home guard, the
Jinja system-prompt's ``user_home`` variable, identity bootstrap scan
roots, ``oc snapshot export`` defaults, and so on.

Bug class history (now closed):
- The original ``_apply_profile_override`` mutated ``os.environ['HOME']``
  in the parent so spawned subprocesses (git/ssh/npm/MCP servers) got
  per-profile credential isolation. But ``Path.home()`` honors ``$HOME``
  on POSIX, so every in-process consumer started returning
  ``<profile>/home/...`` instead of the real home — silent breakage
  surfaced as nested paths like
  ``~/.opencomputer/profiles/coder/home/.opencomputer/profiles/coder``.
- 4 prior workaround commits (c4932d55, 54c83e9f, b1c6638b, 7de77003)
  introduced ``pwd.getpwuid()`` bypass + migrated 16+ callsites to
  ``real_user_home()``. PR #282 patched 10 broken tests. Each fix
  revealed more callsites — a leaky abstraction.
- The architectural fix removes the parent-process mutation entirely.
  Subprocess HOME-scoping is now done at each spawn boundary via
  ``scope_subprocess_env()`` — see ``opencomputer/tools/bash.py`` and
  ``opencomputer/mcp/client.py``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only — uses HOME env var")
def test_path_home_unchanged_after_apply_profile_override(tmp_path, monkeypatch):
    """The parent process's ``Path.home()`` must be the user's real home,
    NOT the profile sandbox, after ``_apply_profile_override`` runs.
    """
    from opencomputer.cli import _apply_profile_override
    from opencomputer.profiles import create_profile

    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path / ".opencomputer"))
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    # Pin a known HOME for the parent process. Path.home() reads $HOME
    # on POSIX, so this is the value we expect to remain stable.
    real_home = tmp_path / "real-user-home"
    real_home.mkdir()
    monkeypatch.setenv("HOME", str(real_home))

    # Create the target profile, then simulate `opencomputer -p coder ...`.
    create_profile("coder")
    monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat"])

    _apply_profile_override()

    # OPENCOMPUTER_HOME is set (profile-scoped behavior preserved).
    assert os.environ["OPENCOMPUTER_HOME"] == str(
        tmp_path / ".opencomputer" / "profiles" / "coder"
    )

    # Path.home() in the parent is unchanged. The architectural fix
    # eliminates parent-process HOME mutation; subprocess HOME-scoping
    # happens at each spawn boundary instead.
    assert Path.home() == real_home, (
        f"_apply_profile_override mutated parent HOME — "
        f"Path.home() returned {Path.home()}, expected {real_home}. "
        f"The fix is to NOT mutate parent HOME; scope subprocesses "
        f"via scope_subprocess_env(env=) at spawn time."
    )

    # The HOME env var itself is also unchanged.
    assert os.environ["HOME"] == str(real_home)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only — uses HOME env var")
def test_xdg_vars_unchanged_after_apply_profile_override(tmp_path, monkeypatch):
    """Parent-process ``XDG_CONFIG_HOME`` / ``XDG_DATA_HOME`` are not
    mutated by ``_apply_profile_override``.
    """
    from opencomputer.cli import _apply_profile_override
    from opencomputer.profiles import create_profile

    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path / ".opencomputer"))
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "real-user-home"))
    (tmp_path / "real-user-home").mkdir()

    create_profile("coder")
    monkeypatch.setattr(sys, "argv", ["opencomputer", "-p", "coder", "chat"])

    _apply_profile_override()

    # XDG_* must remain absent (the user hadn't set them, and the fix
    # forbids the parent-process mutation that previously injected them).
    assert "XDG_CONFIG_HOME" not in os.environ
    assert "XDG_DATA_HOME" not in os.environ


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only — uses HOME env var")
def test_scope_subprocess_env_provides_profile_home_for_subprocesses(
    tmp_path, monkeypatch
):
    """The replacement contract: ``scope_subprocess_env(env, profile=...)``
    returns a dict with HOME / XDG_* pointing at the profile's
    ``home/`` subdir. This dict is passed to subprocesses via ``env=``
    at spawn time (BashTool, MCP launcher).
    """
    from opencomputer.profiles import create_profile, scope_subprocess_env

    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path / ".opencomputer"))
    create_profile("coder")

    base_env = {"HOME": "/parent/home", "PATH": "/usr/bin", "MARKER": "x1y2"}
    scoped = scope_subprocess_env(base_env, profile="coder")

    profile_home = tmp_path / ".opencomputer" / "profiles" / "coder" / "home"
    assert scoped["HOME"] == str(profile_home)
    assert scoped["XDG_CONFIG_HOME"] == str(profile_home / ".config")
    assert scoped["XDG_DATA_HOME"] == str(profile_home / ".local" / "share")

    # Other env vars are preserved (subprocess still gets PATH, etc.).
    assert scoped["PATH"] == "/usr/bin"
    assert scoped["MARKER"] == "x1y2"

    # Original dict was NOT mutated (scope_subprocess_env copies).
    assert base_env["HOME"] == "/parent/home"
