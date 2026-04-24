"""Phase 12b5 / Sub-project E Task E6 — demand signals are profile-scoped.

End-to-end integration test: recording a demand signal in profile A must
not leak into profile B's view of the demand state. This is not a
separate feature — it's an invariant that falls out of the existing
architecture:

- Each profile has its own ``_home()`` directory (Phase 14.A).
- ``cfg.session.db_path`` resolves to ``_home() / "sessions.db"``.
- ``PluginDemandTracker`` is constructed with an explicit ``db_path``,
  not a global connection.

So recording in profile A's DB + querying profile B's DB = no overlap.
This test proves the invariant holds in practice — a regression here
would mean either the db_path resolution broke, or the tracker started
sharing state across instances.

Paired with E2's in-DB session-scoping and E4/E5's CLI profile
resolution.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.mark.usefixtures("monkeypatch")
def test_demand_signals_are_profile_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record in profile A → query profile B → empty.

    Two separate DB files under two separate ``_home()`` roots. The
    tracker must not see across the boundary.
    """
    from opencomputer.plugins.demand_tracker import PluginDemandTracker

    # Two isolated profile homes.
    profile_a_home = tmp_path / "profile-a"
    profile_a_home.mkdir()
    profile_b_home = tmp_path / "profile-b"
    profile_b_home.mkdir()

    # Synthetic candidate that would match "Edit" in either profile.
    fake_manifest = MagicMock()
    fake_manifest.id = "demo-editor"
    fake_manifest.tool_names = ("Edit",)
    fake_candidate = MagicMock()
    fake_candidate.manifest = fake_manifest
    discover_fn = lambda: [fake_candidate]  # noqa: E731

    # --- Profile A: record 3 signals ---
    tracker_a = PluginDemandTracker(
        db_path=profile_a_home / "sessions.db",
        discover_fn=discover_fn,
        active_profile_plugins=frozenset(),  # nothing enabled in A
    )
    for turn in range(3):
        tracker_a.record_tool_not_found("Edit", session_id="s-a", turn_index=turn)

    # A's tracker sees the signals.
    a_signals = tracker_a.signals_by_plugin(session_id="s-a")
    assert "demo-editor" in a_signals
    assert len(a_signals["demo-editor"]) == 3

    # --- Profile B: fresh tracker against its own DB ---
    tracker_b = PluginDemandTracker(
        db_path=profile_b_home / "sessions.db",
        discover_fn=discover_fn,
        active_profile_plugins=frozenset(),
    )
    # B's DB doesn't even exist until the tracker first touches it, but
    # signals_by_plugin must still return cleanly (no rows).
    b_signals = tracker_b.signals_by_plugin(session_id="s-a")
    assert b_signals == {}, (
        f"profile B's tracker leaked signals from profile A's DB: {b_signals}"
    )

    # And B's recommended_plugins is also empty.
    b_recs = tracker_b.recommended_plugins(threshold=1)
    assert b_recs == [], (
        f"profile B's tracker recommended plugins from A's signals: {b_recs}"
    )

    # Sanity: A's tracker still has them — i.e. writing to B didn't
    # accidentally clobber A.
    a_recs = tracker_a.recommended_plugins(threshold=1)
    assert a_recs == [("demo-editor", 3)]

    # Verify the DB files are genuinely separate on disk.
    assert (profile_a_home / "sessions.db").exists()
    assert (profile_b_home / "sessions.db").exists()
    assert (profile_a_home / "sessions.db") != (profile_b_home / "sessions.db")
