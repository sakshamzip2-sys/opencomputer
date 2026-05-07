"""Tests for voice wake-word detector (PR-A Feature 2)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Graceful degrade when openwakeword is missing
# ---------------------------------------------------------------------------


def test_module_imports_without_openwakeword():
    """Importing wake_word must not crash even if openwakeword is absent."""
    # The module under test imports lazily inside __init__; this just
    # verifies the module file itself is importable.
    from opencomputer.voice import wake_word

    assert hasattr(wake_word, "WakeWordDetector")
    assert hasattr(wake_word, "WakeWordError")


def test_init_raises_when_openwakeword_missing(monkeypatch):
    """Constructing the detector raises WakeWordError when dep is missing."""
    from opencomputer.voice import wake_word

    # Force ImportError inside the lazy import path
    monkeypatch.setitem(sys.modules, "openwakeword", None)
    with pytest.raises(wake_word.WakeWordError, match="openwakeword"):
        wake_word.WakeWordDetector(word="hey_jarvis")


# ---------------------------------------------------------------------------
# State machine + parameters
# ---------------------------------------------------------------------------


def test_state_machine_starts_idle():
    """Newly constructed detector is in IDLE state."""
    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        from opencomputer.voice.wake_word import WakeWordDetector

        det = WakeWordDetector(word="hey_jarvis")
        assert det.state == "IDLE"


def test_threshold_default_is_half():
    """Default detection threshold is 0.5."""
    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        from opencomputer.voice.wake_word import WakeWordDetector

        det = WakeWordDetector(word="hey_jarvis")
        assert det.threshold == 0.5


def test_set_state_transitions():
    """set_state moves through the IDLE → DETECTED → SPEAKING → IDLE cycle."""
    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        from opencomputer.voice.wake_word import WakeWordDetector

        det = WakeWordDetector(word="hey_jarvis")
        for s in ("DETECTED", "SPEAKING", "IDLE"):
            det.set_state(s)  # type: ignore[arg-type]
            assert det.state == s


# ---------------------------------------------------------------------------
# Detection callback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_callback_invokes_user_callback():
    """_fire_callback triggers the user's async on_detect with the WakeDetection."""
    fake_ow = MagicMock()
    captured = []

    async def on_detect(d) -> None:
        captured.append(d)

    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        from opencomputer.voice.wake_word import WakeDetection, WakeWordDetector

        det = WakeWordDetector(word="hey_jarvis", on_detect=on_detect)
        await det._fire_callback(WakeDetection("hey_jarvis", 0.7, 1234.0))
        assert len(captured) == 1
        assert captured[0].score == 0.7
        # State returns to IDLE after callback finishes
        assert det.state == "IDLE"


@pytest.mark.asyncio
async def test_fire_callback_without_user_callback_is_noop():
    """No callback set → no error, state unchanged."""
    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        from opencomputer.voice.wake_word import WakeDetection, WakeWordDetector

        det = WakeWordDetector(word="hey_jarvis")
        await det._fire_callback(WakeDetection("hey_jarvis", 0.7, 0.0))
        assert det.state == "IDLE"


# ---------------------------------------------------------------------------
# PID singleton
# ---------------------------------------------------------------------------


def test_pid_lock_acquires_when_free(tmp_path):
    """_acquire_pid_lock writes pid file when none exists; release removes it."""
    from opencomputer.voice.wake_word import _acquire_pid_lock

    pid_file = tmp_path / "wake.pid"
    release = _acquire_pid_lock(pid_file)
    assert pid_file.exists()
    assert pid_file.read_text().strip() == str(os.getpid())
    release()
    assert not pid_file.exists()


def test_pid_lock_blocks_second_instance_with_alive_pid(tmp_path):
    """Second acquire on a live-pid file raises WakeWordError."""
    from opencomputer.voice.wake_word import (
        WakeWordError,
        _acquire_pid_lock,
    )

    pid_file = tmp_path / "wake.pid"
    release = _acquire_pid_lock(pid_file)
    try:
        with pytest.raises(WakeWordError, match="already running"):
            _acquire_pid_lock(pid_file)
    finally:
        release()


def test_pid_lock_clears_stale_pid(tmp_path):
    """Stale pid file (process not alive) is removed and re-acquired."""
    from opencomputer.voice.wake_word import _acquire_pid_lock

    pid_file = tmp_path / "wake.pid"
    # Write a pid that's almost certainly not alive
    pid_file.write_text("999999")
    # Acquire should succeed (stale pid cleared)
    release = _acquire_pid_lock(pid_file)
    assert pid_file.read_text().strip() == str(os.getpid())
    release()
