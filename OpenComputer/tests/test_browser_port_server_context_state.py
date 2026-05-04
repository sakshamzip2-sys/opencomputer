"""Unit tests for `server_context/state.py`."""

from __future__ import annotations

from extensions.browser_control.profiles import (
    ResolvedBrowserProfile,
    resolve_browser_config,
    resolve_profile,
)
from extensions.browser_control.server_context import (
    BrowserServerState,
    ProfileRuntimeState,
    ProfileStatus,
    TabInfo,
)
from extensions.browser_control.server_context.state import (
    get_or_create_profile_state,
    known_profile_names,
    list_profile_statuses,
)


def _resolved_for_test():
    return resolve_browser_config({})


def _profile() -> ResolvedBrowserProfile:
    cfg = _resolved_for_test()
    p = resolve_profile(cfg, "opencomputer")
    assert p is not None
    return p


def test_get_or_create_profile_state_lazy() -> None:
    state = BrowserServerState(resolved=_resolved_for_test())
    p = _profile()
    assert "opencomputer" not in state.profiles
    runtime = get_or_create_profile_state(state, p)
    assert isinstance(runtime, ProfileRuntimeState)
    assert state.profiles["opencomputer"] is runtime
    # Re-call returns the same instance.
    again = get_or_create_profile_state(state, p)
    assert again is runtime


def test_known_profile_names_unions_declared_and_live() -> None:
    state = BrowserServerState(resolved=_resolved_for_test())
    # Default config declares "opencomputer" + "user".
    declared = set(state.resolved.profiles.keys())
    assert "opencomputer" in declared
    assert "user" in declared
    names = known_profile_names(state)
    assert "opencomputer" in names
    assert "user" in names
    # Order is sorted.
    assert names == sorted(names)


def test_list_profile_statuses_default_stopped() -> None:
    state = BrowserServerState(resolved=_resolved_for_test())
    statuses = list_profile_statuses(state)
    assert all(s["status"] == ProfileStatus.STOPPED.value for s in statuses)
    assert all(s["last_target_id"] is None for s in statuses)


def test_tab_info_default_type_is_page() -> None:
    info = TabInfo(target_id="T1", url="https://x.example/")
    assert info.type == "page"
    assert info.title == ""
    assert info.selected is False
