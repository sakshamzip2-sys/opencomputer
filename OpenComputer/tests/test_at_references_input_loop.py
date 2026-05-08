"""Input-loop integration: at-references expand on send."""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.at_references import AtRefContext, expand


def test_input_loop_expander_called_when_at_present(tmp_path):
    """Smoke: expand() processes a message with an @ref."""
    f = tmp_path / "spec.md"
    f.write_text("# spec body")

    msg = f"please review @file:{f}"
    out = expand(msg, ctx=AtRefContext(
        cwd=str(tmp_path),
        home=str(tmp_path / "home"),
        context_window_chars=200_000,
    ))
    assert "spec body" in out
    assert "Attached Context" in out
    # Original text preserved verbatim.
    assert msg in out


def test_input_loop_expander_skipped_when_no_at(tmp_path):
    msg = "no references here"
    out = expand(msg, ctx=AtRefContext(
        cwd=str(tmp_path),
        home=str(tmp_path / "home"),
    ))
    assert out == msg


def test_maybe_expand_short_circuits_without_at():
    """The actual hook in input_loop short-circuits when no @ in text."""
    from opencomputer.cli_ui.input_loop import _maybe_expand_at_refs

    text = "no references here at all"
    assert _maybe_expand_at_refs(text) == text


def test_maybe_expand_returns_original_on_no_refs():
    """An @ that isn't a real ref still returns text unchanged."""
    from opencomputer.cli_ui.input_loop import _maybe_expand_at_refs

    text = "ping me at sak@example.com"
    assert _maybe_expand_at_refs(text) == text


def test_maybe_expand_processes_real_ref(tmp_path, monkeypatch):
    """When a real @file: ref is present, hook expands it."""
    from opencomputer.cli_ui.input_loop import _maybe_expand_at_refs

    f = tmp_path / "data.txt"
    f.write_text("MARKER-DATA-XYZ\n")

    monkeypatch.chdir(tmp_path)
    out = _maybe_expand_at_refs(f"check @file:data.txt for me")
    assert "MARKER-DATA-XYZ" in out
    assert "Attached Context" in out


def test_maybe_expand_swallows_errors(monkeypatch):
    """Any expander exception → original text returned (send never blocked)."""
    from opencomputer.cli_ui import input_loop

    def _boom(text, *, ctx):  # noqa: ANN001
        raise RuntimeError("synthetic")

    monkeypatch.setattr(
        "opencomputer.agent.at_references.expand",
        _boom,
    )
    text = "something with @file:nonsense.txt"
    # Even though @file is present, the expander raises → unchanged.
    assert input_loop._maybe_expand_at_refs(text) == text
