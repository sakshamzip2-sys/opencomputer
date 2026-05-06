"""Tests for opencomputer.security.path_safety.

Covers the canonical helpers the doc-flagged "scattered logic"
gap consolidates around. Symlink + .. + NUL byte + non-existent root
behaviour all locked.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from opencomputer.security.path_safety import (
    UnsafePathError,
    assert_safe_path,
    is_safe_path,
)


def test_path_inside_root_is_safe(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    target = safe_root / "ok.txt"
    target.write_text("hi")
    assert is_safe_path(target, roots=[safe_root]) is True


def test_path_outside_root_is_unsafe(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    other = tmp_path / "other.txt"
    other.write_text("hi")
    assert is_safe_path(other, roots=[safe_root]) is False


def test_dotdot_traversal_blocked(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    target = safe_root / ".." / "etc-passwd"
    assert is_safe_path(target, roots=[safe_root]) is False


def test_symlink_traversal_blocked(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nuclear codes")
    link = safe_root / "back-door"
    if sys.platform == "win32":
        pytest.skip("symlink test requires admin on Windows")
    os.symlink(secret, link)
    assert is_safe_path(link, roots=[safe_root]) is False


def test_string_path_accepted(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    target = safe_root / "ok.txt"
    target.touch()
    assert is_safe_path(str(target), roots=[safe_root]) is True


def test_string_root_accepted(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    target = safe_root / "ok.txt"
    target.touch()
    assert is_safe_path(target, roots=[str(safe_root)]) is True


def test_nul_byte_rejected(tmp_path: Path) -> None:
    """NUL byte in a path is unsafe — kernels truncate on NUL."""
    bad = f"{tmp_path}/safe/file\x00.txt"
    assert is_safe_path(bad, roots=[tmp_path / "safe"]) is False


def test_empty_roots_means_nothing_safe(tmp_path: Path) -> None:
    target = tmp_path / "anything.txt"
    target.touch()
    assert is_safe_path(target, roots=[]) is False


def test_multiple_roots_match_any(tmp_path: Path) -> None:
    """A path under root B is safe even if root A is unrelated."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    target = b / "x.txt"
    target.touch()
    assert is_safe_path(target, roots=[a, b]) is True


def test_nonexistent_root_skipped(tmp_path: Path) -> None:
    """An unresolvable root is silently skipped — others may still match."""
    real = tmp_path / "real"
    real.mkdir()
    target = real / "x.txt"
    target.touch()
    bogus = tmp_path / "does" / "not" / "exist"
    assert is_safe_path(target, roots=[bogus, real]) is True


def test_assert_safe_path_returns_resolved(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    target = safe_root / "ok.txt"
    target.touch()
    out = assert_safe_path(target, roots=[safe_root])
    assert out == target.resolve()


def test_assert_safe_path_raises_unsafe(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    other = tmp_path / "other.txt"
    other.touch()
    with pytest.raises(UnsafePathError):
        assert_safe_path(other, roots=[safe_root])


def test_assert_safe_path_message_includes_path(tmp_path: Path) -> None:
    safe_root = tmp_path / "safe"
    safe_root.mkdir()
    other = tmp_path / "other.txt"
    other.touch()
    with pytest.raises(UnsafePathError, match="other.txt"):
        assert_safe_path(other, roots=[safe_root])
