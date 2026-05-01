"""Regression — `get_default_root()` must be immune to ``$HOME`` mutation.

`_apply_profile_override` (cli.py) sets HOME to ``<profile>/home/`` for
subprocess credential isolation. Before the fix, `get_default_root()`
called ``Path.home()`` which honors the mutated HOME — so any
subsequent profile path resolution (active_profile lookup, profile
create/use/delete, etc.) would resolve relative to the profile's home/
dir instead of the user's actual home, producing nested nonsense like
``~/.opencomputer/profiles/coding/home/.opencomputer/profiles/coding``.

Symptoms before the fix:
- `opencomputer profile use <name>` from within an active profile would
  report ``profile '<name>' does not exist at <nested path>`` even when
  the profile was right there.
- `opencomputer profile use default` would print "active profile cleared"
  but the real ``~/.opencomputer/active_profile`` file was untouched
  (because the ``unlink`` targeted the nested path, not the real one).

The fix: `get_default_root()` uses ``pwd.getpwuid(os.getuid()).pw_dir``
(via `_real_user_home()`) instead of ``Path.home()``. ``pwd`` reads
``/etc/passwd`` and is unaffected by ``$HOME``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from opencomputer.profiles import get_default_root, get_profile_dir


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only — uses pwd")
def test_get_default_root_ignores_home_env_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting HOME to a profile-scoped path must NOT shift the root."""
    # Capture the baseline (real user home).
    monkeypatch.delenv("OPENCOMPUTER_HOME_ROOT", raising=False)
    real_root = get_default_root()

    # Simulate _apply_profile_override mutating HOME for subprocess scoping.
    monkeypatch.setenv("HOME", "/tmp/some-profile-scoped-home")

    # Root must still resolve to the user's actual ~/.opencomputer/.
    assert get_default_root() == real_root, (
        f"get_default_root() shifted under HOME mutation: {get_default_root()!r} "
        f"(expected {real_root!r}) — pwd-based real-home lookup regressed?"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only — uses pwd")
def test_get_profile_dir_immune_to_home_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile directory paths must not nest under a mutated HOME."""
    monkeypatch.delenv("OPENCOMPUTER_HOME_ROOT", raising=False)
    expected = get_profile_dir("coder")

    monkeypatch.setenv("HOME", "/tmp/active-profile-home")

    actual = get_profile_dir("coder")
    assert actual == expected, (
        f"get_profile_dir('coder') nested under mutated HOME: {actual!r} "
        f"(expected {expected!r})"
    )
    # Belt-and-suspenders: explicitly check the "nested nonsense" pattern
    # that would have shown up before the fix.
    assert "/home/.opencomputer/" not in str(actual), (
        f"get_profile_dir resolved into a nested HOME path: {actual!r}"
    )


def test_opencomputer_home_root_override_still_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OPENCOMPUTER_HOME_ROOT override must keep working for tests."""
    monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", "/tmp/something-else-entirely")

    assert get_default_root() == tmp_path
    assert get_profile_dir("coder") == tmp_path / "profiles" / "coder"
