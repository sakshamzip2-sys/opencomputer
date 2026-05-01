"""PluginAPI consumers must read profile-aware paths lazily.

Pass-2 F8 fix: ensure that when a plugin reads ``api.session_db_path``
or ``api.profile_home`` under ``set_profile(b)``, it gets b's path —
not the boot-time default that was current when ``PluginRegistry.api()``
was first called.

Before the fix, ``PluginRegistry.api()`` called ``default_config()`` and
captured ``cfg.session.db_path`` into the ``PluginAPI`` instance at boot
time. The shared ``PluginAPI`` lives forever and is handed to
non-default-profile loops, so any plugin that read
``api.session_db_path`` after a ``set_profile(b)`` got the WRONG path
(the default profile's, not b's). The fix makes both attributes lazy
``@property`` accessors that re-resolve through ``_home()`` per-call,
so the active profile's ContextVar binding wins.
"""
from __future__ import annotations

from pathlib import Path

from plugin_sdk.profile_context import set_profile


def test_pluginapi_session_db_path_is_profile_aware(tmp_path: Path) -> None:
    """A PluginAPI attribute that reads ``_home()`` must reflect the
    currently-active profile, not the boot-time default.

    Pass-2 F8 regression: with the eager-capture bug, this assertion
    fails — ``api.session_db_path`` would point under the default
    ``~/.opencomputer`` (or whatever ``OPENCOMPUTER_HOME`` was at the
    time ``api()`` ran) rather than under ``profile_b``.
    """
    from opencomputer.plugins.registry import registry as plugin_registry

    api = plugin_registry.api()  # built at boot-time under default

    profile_b = tmp_path / "profile_b"
    profile_b.mkdir()

    with set_profile(profile_b):
        path_under_b = api.session_db_path
        assert path_under_b is not None
        assert path_under_b.parent == profile_b, (
            f"PluginAPI session_db_path frozen at boot time; got {path_under_b}, "
            f"expected under {profile_b}"
        )


def test_pluginapi_profile_home_is_profile_aware(tmp_path: Path) -> None:
    """``api.profile_home`` must resolve under the active profile.

    Pass-2 F8 added ``profile_home`` as a lazy property because
    ``screen-awareness`` reads it during ``register()`` to find its
    state file. Without lazy resolution, screen-awareness loads state
    from the default profile even when registered under a different one.
    """
    from opencomputer.plugins.registry import registry as plugin_registry

    api = plugin_registry.api()

    profile_b = tmp_path / "profile_b"
    profile_b.mkdir()

    with set_profile(profile_b):
        home_under_b = api.profile_home
        assert home_under_b == profile_b, (
            f"PluginAPI profile_home frozen at boot time; got {home_under_b}, "
            f"expected {profile_b}"
        )


def test_pluginapi_paths_change_across_profiles(tmp_path: Path) -> None:
    """One PluginAPI instance must yield different paths under different
    ``set_profile(...)`` scopes — the F8 multi-profile contract.
    """
    from opencomputer.plugins.registry import registry as plugin_registry

    api = plugin_registry.api()

    profile_a = tmp_path / "profile_a"
    profile_a.mkdir()
    profile_b = tmp_path / "profile_b"
    profile_b.mkdir()

    with set_profile(profile_a):
        a_db = api.session_db_path
        a_home = api.profile_home

    with set_profile(profile_b):
        b_db = api.session_db_path
        b_home = api.profile_home

    assert a_db != b_db, (
        f"session_db_path did not change between profiles: a={a_db}, b={b_db}"
    )
    assert a_home != b_home, (
        f"profile_home did not change between profiles: a={a_home}, b={b_home}"
    )
    assert a_db is not None and a_db.parent == profile_a
    assert b_db is not None and b_db.parent == profile_b
    assert a_home == profile_a
    assert b_home == profile_b


def test_pluginapi_explicit_session_db_path_override_still_works(
    tmp_path: Path,
) -> None:
    """Tests + callers that pin a path via the constructor must still see
    that exact path — the lazy property only kicks in when no override
    is provided. This protects existing callers in
    ``test_provider_config_schema``, ``test_runtime_contract``,
    ``test_phase12b5_tool_names_field``, and ``test_plugin_teardown``.
    """
    from opencomputer.hooks.engine import HookEngine
    from opencomputer.plugins.loader import PluginAPI
    from opencomputer.tools.registry import ToolRegistry

    pinned = tmp_path / "explicit-session.sqlite"
    api = PluginAPI(
        tool_registry=ToolRegistry(),
        hook_engine=HookEngine(),
        provider_registry={},
        channel_registry={},
        session_db_path=pinned,
    )

    # Outside any set_profile scope, the override wins.
    assert api.session_db_path == pinned

    # Even under set_profile, an explicit override is honoured — the
    # caller's intent (test isolation) overrides the lazy default.
    profile_b = tmp_path / "profile_b"
    profile_b.mkdir()
    with set_profile(profile_b):
        assert api.session_db_path == pinned, (
            "explicit session_db_path override was overridden by lazy "
            "resolution — this would break test fixtures that pin a path"
        )
