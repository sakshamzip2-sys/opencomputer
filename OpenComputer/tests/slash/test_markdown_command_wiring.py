"""Wiring tests for user markdown commands (Recipe 1).

``install_markdown_commands`` folds discovered ``*.md`` commands into the
live slash registry: they must surface in :data:`SLASH_REGISTRY`,
resolve through :func:`resolve_command`, and dispatch to a handler that
pushes the rendered body onto the next-turn queue.

The slash registry is module-global, so every test snapshots and
restores it to stay isolated.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from opencomputer.cli_ui import slash, slash_handlers
from opencomputer.cli_ui.slash import resolve_command
from opencomputer.cli_ui.slash_handlers import (
    SlashContext,
    dispatch_slash,
    install_markdown_commands,
)


@pytest.fixture(autouse=True)
def _restore_registry():
    """Snapshot + restore the module-global slash registry."""
    reg = list(slash.SLASH_REGISTRY)
    lookup = dict(slash._LOOKUP)
    handlers = dict(slash_handlers._HANDLERS)
    yield
    slash.SLASH_REGISTRY[:] = reg
    slash._LOOKUP.clear()
    slash._LOOKUP.update(lookup)
    slash_handlers._HANDLERS.clear()
    slash_handlers._HANDLERS.update(handlers)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _FakeConsole:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.lines.append(" ".join(str(a) for a in args))


def _ctx(queue: list[str]) -> SlashContext:
    return SlashContext(
        console=_FakeConsole(),
        session_id="s1",
        config=None,
        on_clear=lambda: None,
        get_cost_summary=dict,
        get_session_list=list,
        on_queue_add=lambda text: (queue.append(text) or True),
    )


def test_install_registers_command_in_registry(tmp_path: Path) -> None:
    _write(tmp_path / "commands" / "tldr.md", "Summarize in 3 bullets.")
    cmds = install_markdown_commands(tmp_path, project_cwd=None)
    assert [c.name for c in cmds] == ["tldr"]
    resolved = resolve_command("tldr")
    assert resolved is not None
    assert resolved.name == "tldr"


def test_install_no_commands_is_noop(tmp_path: Path) -> None:
    before = len(slash.SLASH_REGISTRY)
    assert install_markdown_commands(tmp_path) == []
    assert len(slash.SLASH_REGISTRY) == before


def test_dispatch_queues_rendered_body(tmp_path: Path) -> None:
    _write(
        tmp_path / "commands" / "explain.md",
        "Explain {{args}} like I'm a junior engineer.",
    )
    install_markdown_commands(tmp_path)
    queue: list[str] = []
    result = dispatch_slash("/explain monads", _ctx(queue))
    assert result.handled is True
    assert queue == ["Explain monads like I'm a junior engineer."]


def test_frontmatter_description_reaches_registry(tmp_path: Path) -> None:
    _write(
        tmp_path / "commands" / "review.md",
        "---\ndescription: Review the working diff\n---\nReview it.",
    )
    install_markdown_commands(tmp_path)
    resolved = resolve_command("review")
    assert resolved is not None
    assert resolved.description == "Review the working diff"


def test_markdown_command_shadows_builtin(tmp_path: Path) -> None:
    """A markdown command named after a built-in replaces its row —
    one entry, not two — and dispatches to the markdown handler."""
    _write(tmp_path / "commands" / "cost.md", "Show me the money.")
    install_markdown_commands(tmp_path)
    cost_rows = [c for c in slash.SLASH_REGISTRY if c.name == "cost"]
    assert len(cost_rows) == 1
    queue: list[str] = []
    dispatch_slash("/cost", _ctx(queue))
    assert queue == ["Show me the money."]


def test_install_is_idempotent(tmp_path: Path) -> None:
    _write(tmp_path / "commands" / "tldr.md", "body")
    install_markdown_commands(tmp_path)
    install_markdown_commands(tmp_path)
    tldr_rows = [c for c in slash.SLASH_REGISTRY if c.name == "tldr"]
    assert len(tldr_rows) == 1
