"""Profile-scoped subprocess environment — public plugin SDK helper.

Plugins that spawn subprocesses (git, ssh, npm, etc.) should use this
helper to ensure those subprocesses see the profile-scoped HOME / XDG_*
when an active profile is set. The opencomputer parent process keeps
its own HOME unchanged (architectural fix in PR #284); subprocess
scoping is per-spawn via env=.

Usage from a plugin tool::

    import os
    from plugin_sdk import current_profile_home, scope_subprocess_env

    profile_home = current_profile_home.get()
    env = scope_subprocess_env(os.environ.copy(), profile_home=profile_home)
    proc = await asyncio.create_subprocess_shell(cmd, env=env, ...)

When ``profile_home`` is None, returns a shallow copy of env unchanged
(subprocess inherits parent's HOME, which is the user's real home).

Convention: subprocesses see ``HOME=<profile_home>/home/``. If the
caller passes a path that already ends with ``/home`` (e.g. one
returned by ``opencomputer.profiles.profile_home_dir``), the helper
detects that and does NOT double-append.
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def scope_subprocess_env(
    env: Mapping[str, str],
    *,
    profile_home: Path | None,
) -> dict[str, str]:
    """Return a new env dict with HOME / XDG_* pointing at profile_home.

    Returns a NEW dict. Caller's env is never mutated. ``profile_home=None``
    returns a shallow copy unchanged (no profile scoping — subprocess
    inherits parent's real HOME).

    The resolved ``HOME`` is ``<profile_home>/home`` unless ``profile_home``
    already ends with ``/home`` (in which case it is used directly). This
    matches ``opencomputer.profiles.profile_home_dir`` which appends
    ``/home`` to the profile root.

    XDG paths are derived from the resolved home::

        XDG_CONFIG_HOME = <home>/.config
        XDG_DATA_HOME   = <home>/.local/share
    """
    out: dict[str, str] = dict(env)
    if profile_home is None:
        return out

    profile_home = Path(profile_home)
    home_dir = profile_home if profile_home.name == "home" else profile_home / "home"

    out["HOME"] = str(home_dir)
    out["XDG_CONFIG_HOME"] = str(home_dir / ".config")
    out["XDG_DATA_HOME"] = str(home_dir / ".local" / "share")
    return out


__all__ = ["scope_subprocess_env"]
