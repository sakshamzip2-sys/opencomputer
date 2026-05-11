"""Tests for opencomputer.agent.trajectory_bundle — per-session flight recorder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opencomputer.agent.trajectory_bundle import (
    TrajectoryBundle,
    TrajectoryBundleError,
    open_bundle,
)


@pytest.fixture
def bundle(tmp_path: Path) -> TrajectoryBundle:
    return open_bundle("test-session-123", root_dir=tmp_path)


def _read_events(b: TrajectoryBundle) -> list[dict]:
    return [json.loads(ln) for ln in b.events_path.read_text().splitlines() if ln.strip()]


class TestBasicRecording:
    def test_record_emits_line(self, bundle: TrajectoryBundle) -> None:
        bundle.record("session.started", model="claude-opus-4-7")
        events = _read_events(bundle)
        assert len(events) == 1
        assert events[0]["type"] == "session.started"
        assert events[0]["model"] == "claude-opus-4-7"
        assert "ts" in events[0]

    def test_multiple_events_append(self, bundle: TrajectoryBundle) -> None:
        bundle.record("a")
        bundle.record("b")
        bundle.record("c")
        events = _read_events(bundle)
        assert [e["type"] for e in events] == ["a", "b", "c"]

    def test_creates_session_branch_json(self, bundle: TrajectoryBundle) -> None:
        bundle.record_branch(parent_id="parent-xyz", child_id="test-session-123")
        data = json.loads(bundle.branch_path.read_text())
        assert data["session_id"] == "test-session-123"
        assert data["parent"] == "parent-xyz"
        assert data["child"] == "test-session-123"


class TestValidation:
    def test_empty_event_type_dropped(self, bundle: TrajectoryBundle) -> None:
        bundle.record("")
        assert not bundle.events_path.exists()

    def test_non_str_event_type_dropped(self, bundle: TrajectoryBundle) -> None:
        bundle.record(None)  # type: ignore[arg-type]
        bundle.record(42)  # type: ignore[arg-type]
        assert not bundle.events_path.exists()

    def test_open_bundle_requires_session_id(self, tmp_path: Path) -> None:
        with pytest.raises(TrajectoryBundleError):
            open_bundle("", root_dir=tmp_path)
        with pytest.raises(TrajectoryBundleError):
            open_bundle(None, root_dir=tmp_path)  # type: ignore[arg-type]


class TestCaps:
    def test_max_events_cap(self, tmp_path: Path) -> None:
        b = open_bundle("s", root_dir=tmp_path, max_events=3)
        for _ in range(10):
            b.record("e")
        events = _read_events(b)
        # Exactly max_events lines written.
        assert len(events) == 3

    def test_max_bytes_cap(self, tmp_path: Path) -> None:
        b = open_bundle("s", root_dir=tmp_path, max_bytes=200)
        # Each record line is small but >50 bytes; 5 of them blow the cap.
        for i in range(20):
            b.record("e", payload="x" * 20, idx=i)
        events = _read_events(b)
        # Some events got through, but not all 20.
        assert 0 < len(events) < 20

    def test_close_stops_appends(self, bundle: TrajectoryBundle) -> None:
        bundle.record("before-close")
        bundle.close()
        bundle.record("after-close")
        events = _read_events(bundle)
        assert [e["type"] for e in events] == ["before-close"]


class TestFailureResilience:
    def test_record_doesnt_raise_on_bad_io(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        b = open_bundle("s", root_dir=tmp_path)
        # Pre-create root as a FILE so write fails.
        b.root.parent.mkdir(parents=True, exist_ok=True)
        b.root.parent.joinpath(b.session_id).write_text("not a dir")
        # Should not raise.
        b.record("e", k="v")

    def test_unserialisable_payload_dropped(self, bundle: TrajectoryBundle) -> None:
        class _Bad:
            def __repr__(self) -> str:
                raise RuntimeError("boom")

        # default=str catches most, but the json.dumps path may still fail
        # for cyclic structures; verify we don't crash either way.
        bundle.record("ok", payload=_Bad())
        # Either dropped (no file) or recorded (file exists). The key
        # invariant: no exception escaped.

    def test_open_bundle_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCOMPUTER_TRAJECTORY_DIR", str(tmp_path / "custom"))
        b = open_bundle("s")
        b.record("e")
        assert (tmp_path / "custom" / "s" / "events.jsonl").exists()
