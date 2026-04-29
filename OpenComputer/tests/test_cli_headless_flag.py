"""--headless on the CLI must set OPENCOMPUTER_HEADLESS=1 for the duration
of the process so downstream is_headless() checks see it.

The callback ``opencomputer.cli.default`` is what handles the global
flag; we invoke it directly with a fake Typer context to verify the
side effect on the env var.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest


def _fake_ctx_with_subcommand() -> MagicMock:
    """Build a Typer context that signals 'a subcommand is invoked'.

    The default callback's ``invoke_without_command=True`` path runs an
    interactive chat loop when ``ctx.invoked_subcommand`` is None — we
    don't want that during a unit test, so set it to a string.
    """
    ctx = MagicMock()
    ctx.invoked_subcommand = "config"
    return ctx


def test_headless_flag_sets_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from opencomputer.cli import default

    monkeypatch.delenv("OPENCOMPUTER_HEADLESS", raising=False)

    default(ctx=_fake_ctx_with_subcommand(), version=False, headless=True)

    assert os.environ.get("OPENCOMPUTER_HEADLESS") == "1"


def test_headless_flag_absent_does_not_set_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opencomputer.cli import default

    monkeypatch.delenv("OPENCOMPUTER_HEADLESS", raising=False)

    default(ctx=_fake_ctx_with_subcommand(), version=False, headless=False)

    assert "OPENCOMPUTER_HEADLESS" not in os.environ
