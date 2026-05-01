"""Tests for plugin_sdk.profile_subprocess.scope_subprocess_env (Follow-up A to PR #284).

The SDK helper is the STATELESS, side-effect-free version of the
``opencomputer.profiles.scope_subprocess_env`` wrapper. Plugins use it
directly with ``current_profile_home.get()``.

Coverage:

1. ``profile_home=None`` returns env unchanged (and as a NEW dict).
2. With ``profile_home`` set, redirects HOME / XDG_CONFIG_HOME /
   XDG_DATA_HOME to ``<home>/`` derivatives.
3. Does NOT mutate the input env dict.
4. Convention: ``profile_home`` already ending in ``/home`` is used
   directly (no double-append).
"""
from __future__ import annotations

from pathlib import Path

from plugin_sdk.profile_subprocess import scope_subprocess_env


def test_none_profile_home_returns_env_unchanged_as_new_dict():
    src = {"HOME": "/Users/real", "PATH": "/usr/bin", "FOO": "bar"}
    out = scope_subprocess_env(src, profile_home=None)

    assert out == src, "env contents must be preserved when profile_home is None"
    assert out is not src, "must return a NEW dict, not the input"


def test_profile_home_redirects_home_and_xdg(tmp_path: Path):
    # Caller passes a profile root WITHOUT a /home suffix — the helper
    # appends /home to match opencomputer.profiles.profile_home_dir.
    profile_root = tmp_path / "profiles" / "coder"
    src = {"HOME": "/Users/real", "PATH": "/usr/bin"}

    out = scope_subprocess_env(src, profile_home=profile_root)

    expected_home = profile_root / "home"
    assert out["HOME"] == str(expected_home)
    assert out["XDG_CONFIG_HOME"] == str(expected_home / ".config")
    assert out["XDG_DATA_HOME"] == str(expected_home / ".local" / "share")
    # Non-HOME keys are preserved.
    assert out["PATH"] == "/usr/bin"


def test_does_not_mutate_input_dict(tmp_path: Path):
    src = {"HOME": "/Users/real", "PATH": "/usr/bin"}
    snapshot = dict(src)

    scope_subprocess_env(src, profile_home=tmp_path / "profile")

    assert src == snapshot, "input env must not be mutated"


def test_profile_home_already_ending_in_home_is_used_directly(tmp_path: Path):
    # Caller passes the result of ``profile_home_dir(name)``, which
    # already includes the /home suffix. Helper must NOT double-append.
    profile_home = tmp_path / "profiles" / "coder" / "home"
    src = {"HOME": "/Users/real", "PATH": "/usr/bin"}

    out = scope_subprocess_env(src, profile_home=profile_home)

    assert out["HOME"] == str(profile_home), "must not double-append /home"
    assert out["XDG_CONFIG_HOME"] == str(profile_home / ".config")
    assert out["XDG_DATA_HOME"] == str(profile_home / ".local" / "share")
