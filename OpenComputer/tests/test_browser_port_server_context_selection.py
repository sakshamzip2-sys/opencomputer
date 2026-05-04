"""Unit tests for `server_context/selection.py` — last_target_id fallback chain."""

from __future__ import annotations

import pytest
from extensions.browser_control.profiles import (
    resolve_browser_config,
    resolve_profile,
)
from extensions.browser_control.server_context import (
    AmbiguousTargetIdError,
    ProfileRuntimeState,
    TabInfo,
    TabNotFoundError,
    resolve_target_id_from_tabs,
    select_target_id,
)


def _runtime() -> ProfileRuntimeState:
    cfg = resolve_browser_config({})
    p = resolve_profile(cfg, "opencomputer")
    assert p is not None
    return ProfileRuntimeState(profile=p)


# ─── resolve_target_id_from_tabs ──────────────────────────────────────


def test_resolve_exact_match() -> None:
    tabs = [TabInfo("T1", "https://a/"), TabInfo("T2", "https://b/")]
    res = resolve_target_id_from_tabs("T1", tabs)
    assert res.kind == "ok"
    assert res.target_id == "T1"


def test_resolve_case_insensitive_prefix_unique() -> None:
    tabs = [TabInfo("ABC123", "https://a/"), TabInfo("XYZ999", "https://b/")]
    res = resolve_target_id_from_tabs("abc", tabs)
    assert res.kind == "ok"
    assert res.target_id == "ABC123"


def test_resolve_ambiguous_prefix() -> None:
    tabs = [TabInfo("ABC1", "https://a/"), TabInfo("ABC2", "https://b/")]
    res = resolve_target_id_from_tabs("abc", tabs)
    assert res.kind == "ambiguous"
    assert set(res.candidates) == {"ABC1", "ABC2"}


def test_resolve_not_found() -> None:
    tabs = [TabInfo("ABC1", "https://a/")]
    res = resolve_target_id_from_tabs("XYZ", tabs)
    assert res.kind == "not_found"


def test_resolve_empty_input() -> None:
    res = resolve_target_id_from_tabs(None, [])
    assert res.kind == "not_found"
    res2 = resolve_target_id_from_tabs("   ", [])
    assert res2.kind == "not_found"


# ─── select_target_id ─────────────────────────────────────────────────


def test_select_explicit_request_resolves() -> None:
    runtime = _runtime()
    tabs = [TabInfo("T1", "https://a/"), TabInfo("T2", "https://b/")]
    chosen = select_target_id(runtime, tabs=tabs, requested="T2")
    assert chosen == "T2"
    # Stamps last_target_id by default.
    assert runtime.last_target_id == "T2"


def test_select_explicit_ambiguous_raises() -> None:
    runtime = _runtime()
    tabs = [TabInfo("ABC1", "https://a/"), TabInfo("ABC2", "https://b/")]
    with pytest.raises(AmbiguousTargetIdError):
        select_target_id(runtime, tabs=tabs, requested="abc")


def test_select_explicit_not_found_raises() -> None:
    runtime = _runtime()
    tabs = [TabInfo("T1", "https://a/")]
    with pytest.raises(TabNotFoundError):
        select_target_id(runtime, tabs=tabs, requested="XYZ")


def test_select_no_request_uses_sticky_last_target() -> None:
    runtime = _runtime()
    runtime.last_target_id = "T2"
    tabs = [TabInfo("T1", "https://a/"), TabInfo("T2", "https://b/")]
    chosen = select_target_id(runtime, tabs=tabs)
    assert chosen == "T2"


def test_select_falls_back_to_first_page_when_sticky_invalid() -> None:
    runtime = _runtime()
    runtime.last_target_id = "stale-id"
    tabs = [
        TabInfo("WORKER1", "https://a/", type="service_worker"),
        TabInfo("PAGE1", "https://b/", type="page"),
    ]
    chosen = select_target_id(runtime, tabs=tabs)
    assert chosen == "PAGE1"
    assert runtime.last_target_id == "PAGE1"


def test_select_falls_back_to_first_tab_when_no_pages() -> None:
    runtime = _runtime()
    tabs = [
        TabInfo("WORKER1", "https://a/", type="service_worker"),
        TabInfo("WORKER2", "https://b/", type="service_worker"),
    ]
    chosen = select_target_id(runtime, tabs=tabs)
    assert chosen == "WORKER1"


def test_select_no_tabs_raises() -> None:
    runtime = _runtime()
    with pytest.raises(TabNotFoundError, match="open about:blank"):
        select_target_id(runtime, tabs=[])


def test_select_does_not_update_last_when_disabled() -> None:
    runtime = _runtime()
    runtime.last_target_id = "T1"
    tabs = [TabInfo("T1", "https://a/"), TabInfo("T2", "https://b/")]
    chosen = select_target_id(runtime, tabs=tabs, requested="T2", update_last=False)
    assert chosen == "T2"
    assert runtime.last_target_id == "T1"
