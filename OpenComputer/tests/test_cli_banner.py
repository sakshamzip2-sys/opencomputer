"""Tests for cli_banner.py — banner assembly + helpers."""
from __future__ import annotations

from pathlib import Path


def test_format_banner_version_label_includes_version_string():
    from opencomputer import __version__
    from opencomputer.cli_banner import format_banner_version_label

    label = format_banner_version_label()
    assert __version__ in label
    assert "OpenComputer" in label


def test_format_banner_version_label_includes_git_sha_when_available(monkeypatch):
    from opencomputer.cli_banner import format_banner_version_label

    monkeypatch.setattr(
        "opencomputer.cli_banner._git_short_sha", lambda: "deadbeef"
    )
    assert "deadbeef" in format_banner_version_label()


def test_format_banner_version_label_omits_git_sha_when_unavailable(monkeypatch):
    from opencomputer.cli_banner import format_banner_version_label

    monkeypatch.setattr("opencomputer.cli_banner._git_short_sha", lambda: None)
    label = format_banner_version_label()
    assert "None" not in label


def test_ascii_art_constants_exist():
    from opencomputer.cli_banner_art import (
        OPENCOMPUTER_LOGO,
        OPENCOMPUTER_LOGO_FALLBACK,
        SIDE_GLYPH,
    )

    assert isinstance(OPENCOMPUTER_LOGO, str)
    # Logo is figlet-style art, so the literal "OPENCOMPUTER" text won't
    # appear character-by-character — but we sanity-check it has multiple
    # lines and substantial width (>50 chars on at least one line).
    lines = OPENCOMPUTER_LOGO.strip("\n").splitlines()
    assert len(lines) >= 5, "Logo is at least 5 lines tall"
    assert any(len(line) >= 50 for line in lines), \
        "Logo has at least one line >= 50 chars wide"

    assert OPENCOMPUTER_LOGO_FALLBACK == "OPENCOMPUTER"

    assert isinstance(SIDE_GLYPH, str)
    assert len(SIDE_GLYPH.splitlines()) >= 6, "Side glyph is at least 6 lines"
