"""Packaging-level command names for the OpenComputer CLI."""
from __future__ import annotations

import sys
from pathlib import Path


def test_oc_is_the_public_console_script():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")

    assert 'oc = "opencomputer.cli:main"' in text
    assert 'opencomputer = "opencomputer.cli:main"' not in text


def test_cli_has_no_oc_rejection_guard(monkeypatch):
    from opencomputer import cli

    monkeypatch.setattr(sys, "argv", [r"C:\Python\Scripts\oc.exe", "chat"])

    assert not hasattr(cli, "_reject_oc_alias")
