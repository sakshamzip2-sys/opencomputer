"""tests/test_doctor_introspection_checks.py — T10 acceptance tests."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.doctor import (
    _check_introspection_deps,
    _check_orphan_oi_venv,
)


def test_orphan_oi_venv_detected(tmp_path: Path):
    (tmp_path / "oi_capability").mkdir()
    result = _check_orphan_oi_venv(tmp_path)
    assert not result.ok
    assert "oi_capability" in result.message
    assert result.level == "warning"


def test_no_orphan_when_absent(tmp_path: Path):
    result = _check_orphan_oi_venv(tmp_path)
    assert result.ok


def test_orphan_oi_venv_handles_nonexistent_profile(tmp_path: Path):
    bogus = tmp_path / "nonexistent"
    result = _check_orphan_oi_venv(bogus)
    assert result.ok


def test_introspection_deps_check_runs():
    results = _check_introspection_deps()
    assert len(results) >= 4
    messages = " | ".join(r.message for r in results)
    for mod in ("psutil", "mss", "pyperclip", "rapidocr_onnxruntime"):
        assert mod in messages


def test_introspection_deps_flags_missing():
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "rapidocr_onnxruntime":
            raise ImportError("No module named 'rapidocr_onnxruntime'")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        results = _check_introspection_deps()

    failed = [r for r in results if r.message.startswith("rapidocr_onnxruntime")]
    assert len(failed) == 1
    assert not failed[0].ok
    # 2026-04-28: introspection is opt-in (coding-harness lists these tools
    # under ``optional_tool_names`` and the plugin filters them at register
    # time when the extra is missing). Doctor classifies the missing dep
    # as a warning (advisory), not an error, so ``oc doctor`` exit-code
    # stays clean on machines that haven't opted into ``[introspection]``.
    # Mirrors the voice-mode pattern at :func:`_check_voice_mode_capable`.
    assert failed[0].level == "warning"
    assert "pip install" in failed[0].message


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="linux-only check")
def test_linux_clipboard_check_runs():
    results = _check_introspection_deps()
    linux_msgs = [r for r in results if "clipboard" in r.message.lower() or "xclip" in r.message.lower()]
    assert len(linux_msgs) >= 1


def test_linux_clipboard_check_is_warning_level_when_missing():
    """Simulate Linux + xclip/xsel missing; verify warning level (NOT error)."""
    with patch("opencomputer.doctor.sys") as mock_sys, \
         patch("opencomputer.doctor.shutil") as mock_shutil:
        mock_sys.platform = "linux"
        mock_shutil.which.return_value = None
        results = _check_introspection_deps()

    linux_msgs = [r for r in results if "xclip" in r.message.lower() or "xsel" in r.message.lower()]
    assert len(linux_msgs) >= 1
    assert all(r.level == "warning" for r in linux_msgs)
