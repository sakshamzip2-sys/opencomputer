"""tests/test_voice_mode_doctor.py — T6 voice-mode preflight checks.

Covers the five distinct paths through ``_check_voice_mode_capable``:

1. ``sounddevice`` not installed → warning + install hint.
2. ``sounddevice`` installed but no audio input device → warning
   (headless / SSH guard).
3. ``webrtcvad`` not installed → warning + install hint.
4. None of OPENAI_API_KEY / mlx-whisper / pywhispercpp present → warning
   pointing at all three install paths.
5. Happy path → ``ok=True`` and the message lists detected backends.

We mock ``builtins.__import__`` rather than the modules themselves so
the test works regardless of which optional wheels happen to be in the
test runner's environment (e.g. a dev box with mlx-whisper installed
must still see the "missing" path when we simulate it).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from opencomputer.doctor import _check_voice_mode_capable


def _import_blocker(blocked: set[str]):
    """Return a fake __import__ that raises ImportError for ``blocked`` names.

    Other imports are delegated to the real ``builtins.__import__`` so the
    function under test can still load os, etc.
    """
    real_import = __import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name in blocked:
            raise ImportError(f"No module named '{name}' (test mock)")
        return real_import(name, globals, locals, fromlist, level)

    return fake


def test_sounddevice_missing():
    """When sounddevice can't import, the check warns with an install hint."""
    with patch(
        "builtins.__import__",
        side_effect=_import_blocker({"sounddevice"}),
    ):
        result = _check_voice_mode_capable()
    assert not result.ok
    assert result.level == "warning"
    assert "sounddevice" in result.message
    assert "opencomputer[voice]" in result.message


def test_no_input_device(monkeypatch: pytest.MonkeyPatch):
    """When sounddevice loads but reports zero input-capable devices, warn."""

    class FakeSd:
        @staticmethod
        def query_devices():
            # All devices are output-only — typical of a headless host.
            return [
                {"name": "BuiltInSpeakers", "max_input_channels": 0},
                {"name": "HDMI Out", "max_input_channels": 0},
            ]

    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name == "sounddevice":
            return FakeSd()
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=fake_import):
        result = _check_voice_mode_capable()

    assert not result.ok
    assert result.level == "warning"
    assert "no audio input device" in result.message.lower()


def test_webrtcvad_missing():
    """sounddevice + input device OK, but webrtcvad missing → warn."""

    class FakeSd:
        @staticmethod
        def query_devices():
            return [{"name": "BuiltInMic", "max_input_channels": 1}]

    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name == "sounddevice":
            return FakeSd()
        if name == "webrtcvad":
            raise ImportError("No module named 'webrtcvad' (test mock)")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=fake_import):
        result = _check_voice_mode_capable()

    assert not result.ok
    assert result.level == "warning"
    assert "webrtcvad" in result.message
    assert "opencomputer[voice]" in result.message


def test_no_stt_backend(monkeypatch: pytest.MonkeyPatch):
    """sounddevice + webrtcvad OK but zero STT backends → warn with all hints."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class FakeSd:
        @staticmethod
        def query_devices():
            return [{"name": "BuiltInMic", "max_input_channels": 1}]

    class FakeVad:
        pass

    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name == "sounddevice":
            return FakeSd()
        if name == "webrtcvad":
            return FakeVad()
        if name in ("mlx_whisper", "pywhispercpp"):
            raise ImportError(f"No module named '{name}' (test mock)")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=fake_import):
        result = _check_voice_mode_capable()

    assert not result.ok
    assert result.level == "warning"
    assert "no STT backend" in result.message
    # Hint must point the user at all three install paths.
    assert "OPENAI_API_KEY" in result.message
    assert "voice-mlx" in result.message
    assert "voice-local" in result.message


def test_all_good_with_api_key(monkeypatch: pytest.MonkeyPatch):
    """API key set + sounddevice + vad available → ok and message lists backend."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    class FakeSd:
        @staticmethod
        def query_devices():
            return [{"name": "BuiltInMic", "max_input_channels": 1}]

    class FakeVad:
        pass

    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: A002
        if name == "sounddevice":
            return FakeSd()
        if name == "webrtcvad":
            return FakeVad()
        # Local backends both unavailable to confirm the message correctly
        # lists only "openai-api".
        if name in ("mlx_whisper", "pywhispercpp"):
            raise ImportError(f"No module named '{name}' (test mock)")
        return real_import(name, globals, locals, fromlist, level)

    with patch("builtins.__import__", side_effect=fake_import):
        result = _check_voice_mode_capable()

    assert result.ok
    assert result.level == "info"
    assert "voice-mode ready" in result.message
    assert "openai-api" in result.message
    # Local backends were intentionally absent — make sure they don't appear.
    assert "mlx-whisper" not in result.message
    assert "whisper-cpp" not in result.message
