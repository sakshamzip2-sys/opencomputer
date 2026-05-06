"""Regression: oc must not hard-crash when shell cwd was removed.

Repro: ``mkdir /tmp/zonk && cd /tmp/zonk && rm -rf /tmp/zonk``.
``os.getcwd()`` raises FileNotFoundError. Before this fix, every
``oc <subcommand>`` invocation crashed before parsing because
multiple module-load and CLI-startup paths called ``Path.cwd()``
unconditionally — most painfully ``find_workspace_overlay``,
which fires at the start of every ``oc chat`` session.

These tests pin the resilience contract for the two hot paths:

- ``opencomputer.agent.workspace.find_workspace_overlay``
- ``opencomputer.agent.prompt_builder._discover_project_md_files``
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def _raise_fnf(*_args, **_kw):  # noqa: ANN001
    raise FileNotFoundError(2, "No such file or directory")


def test_workspace_overlay_returns_none_when_cwd_deleted() -> None:
    from opencomputer.agent.workspace import find_workspace_overlay

    with patch("opencomputer.agent.workspace.Path.cwd", side_effect=_raise_fnf):
        result = find_workspace_overlay()

    assert result is None
