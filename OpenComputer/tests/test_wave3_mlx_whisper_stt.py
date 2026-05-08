"""Wave 3 — mlx-whisper STT backend (Apple Silicon).

Most assertions run on every platform (CI is Linux); transcribe path
is gated behind a skip + soft import to keep the suite portable.
"""

from __future__ import annotations

import platform

import pytest

from opencomputer.voice import stt_mlx_whisper

_IS_APPLE_SILICON = (
    platform.system() == "Darwin" and platform.machine() == "arm64"
)


def test_is_available_on_non_apple_silicon():
    if _IS_APPLE_SILICON:
        pytest.skip("only runs on non-Apple-Silicon hosts")
    assert stt_mlx_whisper.is_available() is False


def test_is_available_returns_bool_on_apple_silicon():
    """On Apple Silicon, returns True if mlx_whisper installed, False otherwise."""
    if not _IS_APPLE_SILICON:
        pytest.skip("Apple Silicon only")
    result = stt_mlx_whisper.is_available()
    assert isinstance(result, bool)


def test_transcribe_on_non_apple_silicon_raises():
    if _IS_APPLE_SILICON:
        pytest.skip("only runs on non-Apple-Silicon hosts")
    from pathlib import Path

    # Use a non-existent file — the platform check should fire FIRST
    # (before file-existence) so we get the friendly architecture error.
    with pytest.raises((RuntimeError, FileNotFoundError)):
        stt_mlx_whisper.transcribe_audio(Path("/tmp/nonexistent.wav"))


def test_transcribe_missing_file_raises_filenotfound(tmp_path):
    if not _IS_APPLE_SILICON:
        pytest.skip("Apple Silicon only — non-AS hosts hit the platform error first")
    missing = tmp_path / "does-not-exist.wav"
    with pytest.raises(FileNotFoundError):
        stt_mlx_whisper.transcribe_audio(missing)


def test_default_model_constant():
    assert stt_mlx_whisper._DEFAULT_MODEL == "mlx-community/whisper-large-v3-turbo"


def test_module_exports():
    assert "is_available" in stt_mlx_whisper.__all__
    assert "transcribe_audio" in stt_mlx_whisper.__all__
