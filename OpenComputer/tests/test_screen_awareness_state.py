"""Tests for ScreenAwarenessState — per-profile plugin state file."""
from __future__ import annotations

from pathlib import Path

from extensions.screen_awareness.state import (
    ScreenAwarenessState,
    load_state,
    save_state,
)


def test_default_state_is_disabled():
    s = ScreenAwarenessState()
    assert s.enabled is False
    assert s.persist is False


def test_load_state_missing_file_returns_default(tmp_path: Path):
    s = load_state(tmp_path)
    assert s.enabled is False
    assert s.persist is False


def test_save_then_load_roundtrip(tmp_path: Path):
    save_state(tmp_path, ScreenAwarenessState(
        enabled=True,
        persist=True,
        cooldown_seconds=2.0,
        ring_size=30,
        freshness_seconds=120.0,
        max_chars=2000,
    ))
    loaded = load_state(tmp_path)
    assert loaded.enabled is True
    assert loaded.persist is True
    assert loaded.cooldown_seconds == 2.0
    assert loaded.ring_size == 30


def test_load_state_corrupt_file_returns_default(tmp_path: Path):
    (tmp_path / "screen_awareness_state.json").write_text("{not valid", encoding="utf-8")
    s = load_state(tmp_path)
    assert s.enabled is False  # fail-safe to disabled


def test_save_atomic_no_tmp_leftover(tmp_path: Path):
    save_state(tmp_path, ScreenAwarenessState(enabled=True))
    assert not (tmp_path / "screen_awareness_state.json.tmp").exists()
