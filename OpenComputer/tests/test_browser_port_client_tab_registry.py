"""Tests for browser-control client/tab_registry.py — track / untrack /
session-cleanup with delete-from-map-first ordering and ignorable-error
swallowing."""

from __future__ import annotations

from typing import Any

import pytest
from extensions.browser_control.client.tab_registry import (
    close_tracked_browser_tabs_for_sessions,
    count_tracked_session_browser_tabs_for_tests,
    reset_tracked_session_browser_tabs_for_tests,
    track_session_browser_tab,
    untrack_session_browser_tab,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_tracked_session_browser_tabs_for_tests()
    yield
    reset_tracked_session_browser_tabs_for_tests()


class TestTrackAndUntrack:
    def test_track_and_count(self):
        track_session_browser_tab(
            session_key="s1", target_id="t1",
            base_url="http://127.0.0.1:1", profile="opencomputer",
        )
        assert count_tracked_session_browser_tabs_for_tests() == 1

    def test_track_blank_inputs_no_op(self):
        track_session_browser_tab(session_key="", target_id="t1")
        track_session_browser_tab(session_key="s1", target_id="")
        track_session_browser_tab(session_key="   ", target_id="t1")
        assert count_tracked_session_browser_tabs_for_tests() == 0

    def test_track_dedup_per_composite_key(self):
        # Same (target_id, base_url, profile) tuple → last-write-wins
        track_session_browser_tab(
            session_key="s1", target_id="t1",
            base_url="b", profile="p",
        )
        track_session_browser_tab(
            session_key="s1", target_id="t1",
            base_url="b", profile="p",
        )
        assert count_tracked_session_browser_tabs_for_tests() == 1

    def test_track_distinct_per_base_url(self):
        # Different baseUrl → independent entries
        track_session_browser_tab(
            session_key="s1", target_id="t1",
            base_url="http://a", profile="p",
        )
        track_session_browser_tab(
            session_key="s1", target_id="t1",
            base_url="http://b", profile="p",
        )
        assert count_tracked_session_browser_tabs_for_tests() == 2

    def test_session_key_case_insensitive(self):
        track_session_browser_tab(session_key="S1", target_id="t1")
        # Same session key, different case — should match
        assert untrack_session_browser_tab(session_key="s1", target_id="t1") is True

    def test_untrack_idempotent(self):
        # Untracking missing keys must not raise
        assert untrack_session_browser_tab(session_key="s1", target_id="t1") is False
        track_session_browser_tab(session_key="s1", target_id="t1")
        assert untrack_session_browser_tab(session_key="s1", target_id="t1") is True
        # Second remove returns False, no error
        assert untrack_session_browser_tab(session_key="s1", target_id="t1") is False


class TestCloseTrackedTabsForSessions:
    @pytest.mark.asyncio
    async def test_closes_all_tracked_tabs(self):
        track_session_browser_tab(session_key="s1", target_id="t1")
        track_session_browser_tab(session_key="s1", target_id="t2")
        track_session_browser_tab(session_key="s1", target_id="t3")

        closed: list[str] = []

        async def stub(*, target_id: str, base_url: str, profile: str) -> Any:
            closed.append(target_id)

        n = await close_tracked_browser_tabs_for_sessions(
            ["s1"], close_tab=stub
        )
        assert n == 3
        assert sorted(closed) == ["t1", "t2", "t3"]

    @pytest.mark.asyncio
    async def test_delete_from_map_first(self):
        """The registry MUST be cleared BEFORE the network calls so a
        concurrent cleanup sees an empty list (idempotent)."""
        track_session_browser_tab(session_key="s1", target_id="t1")

        registry_count_during_close: list[int] = []

        async def slow_close(*, target_id: str, **_kw: Any) -> Any:
            # By the time close is called, registry must already be empty
            registry_count_during_close.append(
                count_tracked_session_browser_tabs_for_tests()
            )

        await close_tracked_browser_tabs_for_sessions(["s1"], close_tab=slow_close)
        assert registry_count_during_close == [0]

    @pytest.mark.asyncio
    async def test_swallows_ignorable_errors(self):
        track_session_browser_tab(session_key="s1", target_id="t1")
        track_session_browser_tab(session_key="s1", target_id="t2")

        async def stub(*, target_id: str, **_kw: Any) -> Any:
            if target_id == "t1":
                raise RuntimeError("tab not found")
            # t2 succeeds

        warnings: list[str] = []
        n = await close_tracked_browser_tabs_for_sessions(
            ["s1"],
            close_tab=stub,
            on_warn=warnings.append,
        )
        # t2 closed; t1 swallowed silently → count is 1
        assert n == 1
        assert warnings == [], "ignorable errors must not warn"

    @pytest.mark.asyncio
    async def test_warns_on_unfamiliar_errors(self):
        track_session_browser_tab(session_key="s1", target_id="t1")

        async def stub(*, target_id: str, **_kw: Any) -> Any:
            raise RuntimeError("permission denied")

        warnings: list[str] = []
        n = await close_tracked_browser_tabs_for_sessions(
            ["s1"], close_tab=stub, on_warn=warnings.append
        )
        assert n == 0
        assert len(warnings) == 1
        assert "permission denied" in warnings[0]

    @pytest.mark.asyncio
    async def test_unknown_session_no_op(self):
        async def stub(**_kw: Any) -> Any:
            raise AssertionError("should not be called")

        n = await close_tracked_browser_tabs_for_sessions(
            ["unknown"], close_tab=stub
        )
        assert n == 0
