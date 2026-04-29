"""Headless detection — explicit flag wins, otherwise sys.stdin.isatty()."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest


def test_is_headless_true_when_force_flag_set() -> None:
    from opencomputer.headless import is_headless
    with patch.dict(os.environ, {"OPENCOMPUTER_HEADLESS": "1"}, clear=False):
        assert is_headless(force=True) is True
        assert is_headless() is True  # env reads as truthy too


def test_is_headless_false_when_stdin_is_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.headless import is_headless
    monkeypatch.delenv("OPENCOMPUTER_HEADLESS", raising=False)
    fake_stdin = type("S", (), {"isatty": lambda self: True})()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    assert is_headless() is False


def test_is_headless_true_when_stdin_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.headless import is_headless
    monkeypatch.delenv("OPENCOMPUTER_HEADLESS", raising=False)
    fake_stdin = type("S", (), {"isatty": lambda self: False})()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    assert is_headless() is True


def test_is_headless_env_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.headless import is_headless
    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("OPENCOMPUTER_HEADLESS", val)
        assert is_headless() is True, f"{val!r} should be truthy"


def test_is_headless_env_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.headless import is_headless
    fake_stdin = type("S", (), {"isatty": lambda self: True})()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    for val in ("0", "false", "no", "off"):
        monkeypatch.setenv("OPENCOMPUTER_HEADLESS", val)
        assert is_headless() is False, f"{val!r} should be falsy"
