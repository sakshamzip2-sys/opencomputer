"""Skin-aware banner palette (Recipe 1 residual).

cli_banner.py historically hardcoded its 6-colour palette and ignored
the skin engine. ``_palette()`` now resolves the palette from the
active skin's ``banner_*`` colours, so ``oc skin set mono`` re-themes
the splash. The ``default`` skin (and no active skin) keep OC's
hardcoded pink — byte-identical to the historical output — so the
splash never silently regresses.

The legacy module names ``_TITLE`` … ``_SESSION`` (used unchanged by
the 7 render helpers) resolve live via the module ``__getattr__`` hook.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

from rich.console import Console

from opencomputer.cli_banner import _palette, build_welcome_banner


def test_palette_is_oc_pink_when_no_skin_is_active(monkeypatch):
    monkeypatch.setattr("opencomputer.cli_ui.skin.current_spec", lambda: None)
    pal = _palette()
    assert pal["title"] == "#FF3D8A"
    assert pal["border"] == "#C2185B"
    assert pal["session"] == "#8B8682"


def test_palette_is_oc_pink_under_the_default_skin(monkeypatch):
    """The default skin keeps OC pink even if its banner_* keys say otherwise."""
    default_skin = SimpleNamespace(
        name="default",
        colors={"banner_title": "#FFD700", "banner_border": "#D4AF37"},
    )
    monkeypatch.setattr(
        "opencomputer.cli_ui.skin.current_spec", lambda: default_skin
    )
    pal = _palette()
    assert pal["title"] == "#FF3D8A"  # not the gold #FFD700
    assert pal["border"] == "#C2185B"


def test_palette_follows_a_non_default_skin(monkeypatch):
    mono = SimpleNamespace(
        name="mono",
        colors={
            "banner_title": "#FFFFFF",
            "banner_accent": "#A0A0A0",
            "banner_border": "#FFFFFF",
            "banner_dim": "#808080",
            "banner_text": "#FFFFFF",
        },
    )
    monkeypatch.setattr("opencomputer.cli_ui.skin.current_spec", lambda: mono)
    pal = _palette()
    assert pal["title"] == "#FFFFFF"
    assert pal["accent"] == "#A0A0A0"
    assert pal["border"] == "#FFFFFF"
    # session_border is absent from this skin → OC-pink fallback
    assert pal["session"] == "#8B8682"


def test_palette_failure_collapses_to_pink(monkeypatch):
    """A skin lookup that raises must never crash the splash."""

    def _boom():
        raise RuntimeError("skin subsystem exploded")

    monkeypatch.setattr("opencomputer.cli_ui.skin.current_spec", _boom)
    pal = _palette()
    assert pal["title"] == "#FF3D8A"


def test_legacy_module_attr_resolves_through_the_active_skin(monkeypatch):
    """`cli_banner._TITLE` (used by every render helper) is skin-resolved."""
    import opencomputer.cli_banner as banner

    mono = SimpleNamespace(name="mono", colors={"banner_title": "#FFFFFF"})
    monkeypatch.setattr("opencomputer.cli_ui.skin.current_spec", lambda: mono)
    assert banner._TITLE == "#FFFFFF"

    monkeypatch.setattr("opencomputer.cli_ui.skin.current_spec", lambda: None)
    assert banner._TITLE == "#FF3D8A"


def test_build_welcome_banner_renders_under_a_non_default_skin(monkeypatch):
    """End-to-end: the splash renders cleanly with a non-default skin active."""
    mono = SimpleNamespace(
        name="mono",
        colors={"banner_title": "#FFFFFF", "banner_border": "#FFFFFF"},
    )
    monkeypatch.setattr("opencomputer.cli_ui.skin.current_spec", lambda: mono)
    buf = io.StringIO()
    console = Console(file=buf, width=140, force_terminal=False)
    build_welcome_banner(console, "claude-opus-4-7", "/tmp/x")
    assert "OpenComputer" in buf.getvalue()
