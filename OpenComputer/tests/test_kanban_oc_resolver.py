"""Pin _resolve_oc_executable's three-tier fallback.

Root cause for the t_1b176c7d spawn-failure: the kanban dispatcher
ran subprocess.Popen(["oc", ...]) inheriting a daemon-launch $PATH
that didn't include ~/.local/bin (or the venv's bin/), so execvp
raised FileNotFoundError. The resolver removes the PATH-only
dependency by adding two fallback layers.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

from opencomputer.kanban.db import _resolve_oc_executable


def test_tier1_uses_path_when_oc_is_findable(monkeypatch, tmp_path):
    """Tier 1: shutil.which returns -> use it."""
    fake_oc = tmp_path / "oc"
    fake_oc.write_text("#!/bin/sh\necho fake\n")
    fake_oc.chmod(fake_oc.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = _resolve_oc_executable()
    assert result == [str(fake_oc)]


def test_tier2_uses_sibling_of_sys_executable_when_path_strips_oc(monkeypatch, tmp_path):
    """Tier 2: PATH doesn't have oc, but sys.executable's sibling does."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nexec real-python\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)
    fake_oc = fake_bin / "oc"
    fake_oc.write_text("#!/bin/sh\nexec real-oc\n")
    fake_oc.chmod(fake_oc.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    # Strip PATH so shutil.which("oc") returns None.
    monkeypatch.setenv("PATH", "/nonexistent")
    with patch.object(sys, "executable", str(fake_python)):
        result = _resolve_oc_executable()
    assert result == [str(fake_oc)]


def test_tier3_falls_back_to_module_form_when_no_oc_anywhere(monkeypatch, tmp_path):
    """Tier 3: no oc on PATH AND no sibling -> [sys.executable, '-m', 'opencomputer']."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nexec real-python\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)
    # No oc next to fake_python, no oc on PATH.

    monkeypatch.setenv("PATH", "/nonexistent")
    with patch.object(sys, "executable", str(fake_python)):
        result = _resolve_oc_executable()
    assert result == [str(fake_python), "-m", "opencomputer"]


def test_tier2_skips_non_executable_sibling(monkeypatch, tmp_path):
    """A non-executable sibling file shouldn't be picked up."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nexec real-python\n")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)
    fake_oc = fake_bin / "oc"
    fake_oc.write_text("not executable\n")
    # Don't chmod +x.

    monkeypatch.setenv("PATH", "/nonexistent")
    with patch.object(sys, "executable", str(fake_python)):
        result = _resolve_oc_executable()
    # Falls through to tier 3 because sibling isn't executable.
    assert result == [str(fake_python), "-m", "opencomputer"]


def test_resolver_returns_list_for_argv_splat(monkeypatch, tmp_path):
    """Result must be a list — callers splat with [*_resolve_oc_executable(), ...]."""
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir
    fake_python = tmp_path / "fake-python"
    fake_python.write_text("")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IEXEC)
    with patch.object(sys, "executable", str(fake_python)):
        result = _resolve_oc_executable()
    assert isinstance(result, list)
    assert len(result) >= 1
