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


# ---------------------------------------------------------------------------
# hey_open_computer fallback (PR-A: requested wake-word vs available)
# ---------------------------------------------------------------------------


def test_default_word_is_hey_open_computer():
    """The conceptual default is 'hey_open_computer' (user intent)."""
    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        from opencomputer.voice.wake_word import WakeWordDetector

        det = WakeWordDetector()
        assert det.word == "hey_open_computer"


def test_resolve_word_falls_back_to_hey_jarvis_when_custom_unavailable():
    """Without model_path and a non-bundled word, _resolve_word falls back."""
    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        from opencomputer.voice.wake_word import (
            FALLBACK_BUNDLED_WORD,
            WakeWordDetector,
        )

        det = WakeWordDetector(word="hey_open_computer")
        active = det._resolve_word()
        assert active == FALLBACK_BUNDLED_WORD
        assert det.fell_back is True
        assert det.effective_word == FALLBACK_BUNDLED_WORD


def test_resolve_word_keeps_bundled_words():
    """A bundled word like 'hey_jarvis' is used as-is, no fallback."""
    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        from opencomputer.voice.wake_word import WakeWordDetector

        det = WakeWordDetector(word="hey_jarvis")
        active = det._resolve_word()
        assert active == "hey_jarvis"
        assert det.fell_back is False
        assert det.effective_word == "hey_jarvis"


def test_resolve_word_keeps_custom_when_model_path_provided(tmp_path):
    """User-supplied model_path → trust the word label."""
    fake_ow = MagicMock()
    with patch.dict(sys.modules, {"openwakeword": fake_ow}):
        from opencomputer.voice.wake_word import WakeWordDetector

        custom_model = tmp_path / "hey_open_computer.onnx"
        custom_model.write_bytes(b"fake-onnx-bytes")
        det = WakeWordDetector(
            word="hey_open_computer",
            model_path=custom_model,
        )
        active = det._resolve_word()
        assert active == "hey_open_computer"
        assert det.fell_back is False


def test_bundled_words_constant_includes_hey_jarvis():
    """Sanity: BUNDLED_WAKE_WORDS includes the documented set."""
    from opencomputer.voice.wake_word import BUNDLED_WAKE_WORDS

    assert "hey_jarvis" in BUNDLED_WAKE_WORDS
    assert "alexa" in BUNDLED_WAKE_WORDS


# ---------------------------------------------------------------------------
# Auto-discovery from <profile_home>/wake_models/<word>.onnx
# ---------------------------------------------------------------------------


def test_wake_models_dir_uses_profile_home(tmp_path, monkeypatch):
    """wake_models_dir resolves to <profile_home>/wake_models/."""
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    from opencomputer.voice.wake_word import wake_models_dir

    result = wake_models_dir()
    assert result == tmp_path / "wake_models"


def test_auto_discover_model_returns_path_when_present(tmp_path, monkeypatch):
    """_auto_discover_model returns the ONNX path when present on disk."""
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    models_dir = tmp_path / "wake_models"
    models_dir.mkdir()
    onnx = models_dir / "hey_open_computer.onnx"
    onnx.write_bytes(b"fake")
    from opencomputer.voice.wake_word import _auto_discover_model

    found = _auto_discover_model("hey_open_computer")
    assert found == onnx


def test_auto_discover_model_returns_none_when_missing(tmp_path, monkeypatch):
    """_auto_discover_model returns None when no ONNX is at the path."""
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    from opencomputer.voice.wake_word import _auto_discover_model

    assert _auto_discover_model("hey_open_computer") is None


def test_auto_discover_model_returns_none_when_empty(tmp_path, monkeypatch):
    """An empty ONNX file is treated as missing — never returned as a hit."""
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    models_dir = tmp_path / "wake_models"
    models_dir.mkdir()
    (models_dir / "hey_open_computer.onnx").write_bytes(b"")
    from opencomputer.voice.wake_word import _auto_discover_model

    assert _auto_discover_model("hey_open_computer") is None


def test_resolve_word_uses_auto_discovered_model(tmp_path, monkeypatch):
    """When custom word + no model_path + ONNX on disk, _resolve_word uses it."""
    from unittest.mock import MagicMock

    fake_ow = MagicMock()
    monkeypatch.setitem(sys.modules, "openwakeword", fake_ow)
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    models_dir = tmp_path / "wake_models"
    models_dir.mkdir()
    (models_dir / "hey_open_computer.onnx").write_bytes(b"fake")

    from opencomputer.voice.wake_word import WakeWordDetector

    det = WakeWordDetector(word="hey_open_computer")
    active = det._resolve_word()
    assert active == "hey_open_computer"
    assert det.fell_back is False
    assert det.model_path == models_dir / "hey_open_computer.onnx"


def test_resolve_word_still_falls_back_when_no_trained_model(
    tmp_path, monkeypatch,
):
    """No trained ONNX → fallback to hey_jarvis still fires."""
    from unittest.mock import MagicMock

    fake_ow = MagicMock()
    monkeypatch.setitem(sys.modules, "openwakeword", fake_ow)
    monkeypatch.setattr(
        "opencomputer.voice.wake_word._resolve_profile_home",
        lambda: tmp_path,
    )
    from opencomputer.voice.wake_word import (
        FALLBACK_BUNDLED_WORD,
        WakeWordDetector,
    )

    det = WakeWordDetector(word="hey_open_computer")
    active = det._resolve_word()
    assert active == FALLBACK_BUNDLED_WORD
    assert det.fell_back is True
