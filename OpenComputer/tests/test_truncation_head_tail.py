"""D1: 70/20/10 head/tail/marker truncation (Hermes v2 production parity).

Replaces head-only truncation. Workspace-context files larger than 100KB
keep the first 70KB + the last 20KB with a marker between them showing
``kept 70,000+20,000 of N chars``.

For the agent reading a long config file, head+tail covers both the
project overview *and* the closing-section conventions (which were
silently dropped under head-only truncation).
"""
from __future__ import annotations

from pathlib import Path

from opencomputer.agent.prompt_builder import (
    _format_truncation_note,
    _truncate_head_tail,
    load_workspace_context,
)


# ─── unit tests for the helpers ───────────────────────────────────


def test_truncate_no_op_under_cap():
    out = _truncate_head_tail("short content", name="X.md", cap=100)
    assert out == "short content"


def test_truncate_keeps_head_and_tail():
    head_marker = "HEAD-START"
    tail_marker = "TAIL-END"
    body = "F" * 100_000
    content = head_marker + body + tail_marker
    out = _truncate_head_tail(content, name="AGENTS.md", cap=10_000)
    assert head_marker in out
    assert tail_marker in out
    # The marker section announces what's kept.
    assert "[...truncated AGENTS.md:" in out
    assert "kept 7,000+2,000 of" in out


def test_truncate_marker_shape():
    note = _format_truncation_note("CLAUDE.md", kept_head=14_000, kept_tail=4_000, total=25_000)
    assert "[...truncated CLAUDE.md:" in note
    assert "kept 14,000+4,000 of 25,000 chars" in note
    assert "Use file tools to read the full file." in note


# ─── integration: load_workspace_context ──────────────────────────


def test_oversized_claude_md_keeps_head_and_tail(tmp_path: Path):
    head_signature = "TOP-OF-FILE-MARKER"
    tail_signature = "BOTTOM-OF-FILE-MARKER"
    body = "x" * 200_000
    (tmp_path / "CLAUDE.md").write_text(
        head_signature + body + tail_signature, encoding="utf-8"
    )

    out = load_workspace_context(start=tmp_path)

    # Both signatures survived.
    assert head_signature in out
    assert tail_signature in out
    # The marker showed up between them.
    assert "[...truncated CLAUDE.md:" in out
    assert "kept 70,000+20,000" in out
    # Marker order: head_signature appears BEFORE the marker, tail_signature
    # appears AFTER. This proves we didn't accidentally swap segments.
    head_idx = out.index(head_signature)
    marker_idx = out.index("[...truncated CLAUDE.md:")
    tail_idx = out.index(tail_signature)
    assert head_idx < marker_idx < tail_idx


def test_just_under_cap_loads_unchanged(tmp_path: Path):
    body = "y" * 99_000  # under 100KB
    (tmp_path / "AGENTS.md").write_text(body, encoding="utf-8")
    out = load_workspace_context(start=tmp_path)
    assert "[...truncated" not in out
    # Body content shows up; the loader strips outer whitespace.
    assert body in out
