"""Tests for the /paste slash command — Hermes-parity clipboard-image attach.

Hermes ships ``/paste`` ("Attach clipboard image from your clipboard").
OC already has the cross-platform clipboard-image engine in
``cli_ui/clipboard.py`` and an ``/image <path>`` command, but no slash
command that pulls the image straight off the system clipboard.

The OS clipboard is a genuine external boundary — there is no way to put
an image on a CI runner's clipboard — so it is faked via monkeypatch.
The assertions exercise the handler's real branching logic, not the fake.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from rich.console import Console

from opencomputer.cli_ui.slash import SLASH_REGISTRY, resolve_command
from opencomputer.cli_ui.slash_handlers import SlashContext, dispatch_slash


def _make_ctx(on_image_attach) -> SlashContext:
    """A SlashContext wired with a spy ``on_image_attach`` callback."""
    return SlashContext(
        console=Console(record=True),
        session_id="test-session",
        config=MagicMock(),
        on_clear=lambda: None,
        get_cost_summary=lambda: {"in": 0, "out": 0},
        get_session_list=list,
        on_image_attach=on_image_attach,
    )


def test_paste_command_is_registered():
    """`/paste` resolves to a CommandDef and appears in the registry."""
    cmd = resolve_command("paste")
    assert cmd is not None
    assert cmd.name == "paste"
    assert "paste" in {c.name for c in SLASH_REGISTRY}


def test_paste_attaches_clipboard_image(monkeypatch):
    """An image on the clipboard is saved to disk and queued for the next message."""
    monkeypatch.setattr(
        "opencomputer.cli_ui.clipboard.has_clipboard_image", lambda: True
    )
    monkeypatch.setattr(
        "opencomputer.cli_ui.clipboard.save_clipboard_image", lambda *_: True
    )
    attach = MagicMock(return_value=(True, "Attached clipboard image"))
    ctx = _make_ctx(attach)

    result = dispatch_slash("/paste", ctx)

    assert result.handled is True
    attach.assert_called_once()
    # The handler hands the attach callback a real .png path on disk.
    assert attach.call_args.args[0].endswith(".png")
    assert "Attached clipboard image" in ctx.console.export_text()


def test_paste_reports_when_clipboard_has_no_image(monkeypatch):
    """No clipboard image → friendly message, and no attach attempt."""
    monkeypatch.setattr(
        "opencomputer.cli_ui.clipboard.has_clipboard_image", lambda: False
    )
    attach = MagicMock()
    ctx = _make_ctx(attach)

    result = dispatch_slash("/paste", ctx)

    assert result.handled is True
    attach.assert_not_called()
    assert "clipboard" in ctx.console.export_text().lower()


def test_paste_reports_when_extraction_fails(monkeypatch):
    """Clipboard claims an image but extraction fails → no attach, clear error."""
    monkeypatch.setattr(
        "opencomputer.cli_ui.clipboard.has_clipboard_image", lambda: True
    )
    monkeypatch.setattr(
        "opencomputer.cli_ui.clipboard.save_clipboard_image", lambda *_: False
    )
    attach = MagicMock()
    ctx = _make_ctx(attach)

    result = dispatch_slash("/paste", ctx)

    assert result.handled is True
    attach.assert_not_called()
    assert "couldn't read" in ctx.console.export_text().lower()
