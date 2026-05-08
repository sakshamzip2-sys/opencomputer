"""Tests for Hermes-parity root-user gateway refusal."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from opencomputer.gateway.server import _check_not_root


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only check (no geteuid on Windows)",
)
def test_check_passes_for_non_root():
    """Non-root euid must not raise / not exit."""
    with patch("os.geteuid", return_value=1000):
        # Should return cleanly.
        _check_not_root()


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only check",
)
def test_check_refuses_root_without_override(capsys):
    """Root euid + no override env var → exit 2 with stderr explanation."""
    # Snapshot, drop override, restore on exit.
    snap = os.environ.get("OPENCOMPUTER_ALLOW_ROOT_GATEWAY")
    os.environ.pop("OPENCOMPUTER_ALLOW_ROOT_GATEWAY", None)
    try:
        with patch("os.geteuid", return_value=0):
            with pytest.raises(SystemExit) as exc_info:
                _check_not_root()
            assert exc_info.value.code == 2
            captured = capsys.readouterr()
            assert "root" in captured.err.lower()
            assert "OPENCOMPUTER_ALLOW_ROOT_GATEWAY" in captured.err
    finally:
        if snap is not None:
            os.environ["OPENCOMPUTER_ALLOW_ROOT_GATEWAY"] = snap


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only check",
)
def test_check_allows_root_with_override():
    """Root euid + override env var = "1" → no exit."""
    with patch("os.geteuid", return_value=0), \
         patch.dict(os.environ, {"OPENCOMPUTER_ALLOW_ROOT_GATEWAY": "1"}):
        _check_not_root()  # must not raise


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only check",
)
def test_check_refuses_root_with_override_other_value(capsys):
    """Override must be exactly ``1`` — ``true`` / ``yes`` not accepted (matches
    Hermes' strict-1 convention)."""
    snap = os.environ.get("OPENCOMPUTER_ALLOW_ROOT_GATEWAY")
    try:
        with patch("os.geteuid", return_value=0), \
             patch.dict(
                 os.environ,
                 {"OPENCOMPUTER_ALLOW_ROOT_GATEWAY": "true"},
             ):
            with pytest.raises(SystemExit) as exc_info:
                _check_not_root()
            assert exc_info.value.code == 2
    finally:
        if snap is not None:
            os.environ["OPENCOMPUTER_ALLOW_ROOT_GATEWAY"] = snap
