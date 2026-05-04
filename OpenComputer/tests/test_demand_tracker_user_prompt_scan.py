"""Tests for PluginDemandTracker.scan_user_prompt (E7-T2)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from opencomputer.plugins.demand_tracker import (
    USER_PROMPT_KEYWORD_MARKER,
    PluginDemandTracker,
    _extract_plugin_terms,
    _tokenize_prompt,
)


@dataclass
class _FakeManifest:
    id: str
    description: str = ""
    tool_names: tuple[str, ...] = ()
    optional_tool_names: tuple[str, ...] = ()
    name: str = ""
    version: str = "0.0.0"


@dataclass
class _FakeCandidate:
    manifest: _FakeManifest


def _make_candidates() -> list[_FakeCandidate]:
    return [
        _FakeCandidate(_FakeManifest(
            id="github",
            description="Read GitHub repos issues PRs",
            tool_names=("GitHubFetch",),
        )),
        _FakeCandidate(_FakeManifest(
            id="postgres",
            description="Query PostgreSQL databases",
            tool_names=("PostgresQuery",),
        )),
        _FakeCandidate(_FakeManifest(
            id="empty",
            description="",
            tool_names=(),
        )),
    ]


@pytest.fixture
def tracker(tmp_path):
    return PluginDemandTracker(
        db_path=tmp_path / "demand.db",
        discover_fn=_make_candidates,
        active_profile_plugins=frozenset(),
    )


def test_tokenize_lowercase_and_filter_stopwords():
    out = _tokenize_prompt("How can I close a GitHub issue?")
    # 'how', 'can', 'i', 'a' are stopwords; 'close', 'github', 'issue' remain
    assert "github" in out
    assert "issue" in out
    assert "close" in out
    assert "how" not in out
    assert "the" not in out


def test_tokenize_drops_single_chars_and_digits_alone():
    out = _tokenize_prompt("a 1 ab 12 ok")
    # Single chars dropped; multi-char tokens kept
    assert "a" not in out
    assert "1" not in out
    assert "ab" in out
    assert "12" in out
    assert "ok" in out


def test_extract_plugin_terms_includes_id_and_description():
    cand = _FakeCandidate(_FakeManifest(
        id="github",
        description="Read GitHub repos issues",
        tool_names=("GitHubFetch",),
    ))
    terms = _extract_plugin_terms(cand)
    assert "github" in terms
    assert "issues" in terms
    assert "githubfetch" in terms


def test_scan_matches_when_min_matches_reached(tracker):
    """Two co-occurring keywords trigger a signal.

    Note: keyword match is exact (no stemming) — caller should use
    plural/singular forms that match the manifest description.
    """
    triggered = tracker.scan_user_prompt(
        "list github repos and read prs",  # 'github' + 'repos' + 'prs' all match
        session_id="s1",
        turn_index=0,
        min_matches=2,
    )
    assert "github" in triggered


def test_scan_does_not_trigger_below_min_matches(tracker):
    """Single keyword match doesn't trigger when min_matches=2."""
    triggered = tracker.scan_user_prompt(
        "what is github",  # only 'github' matches
        min_matches=2,
    )
    assert triggered == []


def test_scan_skips_already_enabled_plugins(tmp_path):
    """If a plugin is already enabled, no signal fires for it."""
    tracker = PluginDemandTracker(
        db_path=tmp_path / "demand.db",
        discover_fn=_make_candidates,
        active_profile_plugins=frozenset({"github"}),  # github already enabled
    )
    triggered = tracker.scan_user_prompt(
        "close a github issue",
        min_matches=2,
    )
    assert "github" not in triggered


def test_scan_writes_to_demand_table_with_marker(tracker):
    """Signal rows use the synthetic USER_PROMPT_KEYWORD_MARKER tool_name.

    Uses exact-match terms ('postgres' + 'query' + 'databases' all
    appear in the postgres plugin description).
    """
    tracker.scan_user_prompt(
        "query postgres databases please",
        session_id="s2",
        turn_index=3,
        min_matches=2,
    )
    by_plugin = tracker.signals_by_plugin()
    # postgres should have at least one signal
    assert "postgres" in by_plugin
    assert any(
        row["tool_name"] == USER_PROMPT_KEYWORD_MARKER
        for row in by_plugin["postgres"]
    )


def test_scan_returns_empty_for_empty_prompt(tracker):
    assert tracker.scan_user_prompt("") == []
    assert tracker.scan_user_prompt("    ") == []


def test_scan_returns_empty_when_no_plugins_match(tracker):
    """Prompt about something no installed plugin covers → no triggers."""
    out = tracker.scan_user_prompt(
        "what is the weather today",  # no plugin description matches
        min_matches=2,
    )
    assert out == []


def test_scan_handles_discovery_failure_gracefully(tmp_path):
    """If discover_fn raises, scan returns [] and doesn't propagate."""
    def _raises():
        raise RuntimeError("discovery broken")
    tracker = PluginDemandTracker(
        db_path=tmp_path / "demand.db",
        discover_fn=_raises,
    )
    # Should not raise
    out = tracker.scan_user_prompt("close a github issue", min_matches=2)
    assert out == []


def test_marker_is_distinct_from_real_tool_names():
    """The synthetic marker won't collide with a real tool name."""
    # Real tool names are PascalCase per project convention
    assert USER_PROMPT_KEYWORD_MARKER.startswith("__")
    assert USER_PROMPT_KEYWORD_MARKER.endswith("__")
