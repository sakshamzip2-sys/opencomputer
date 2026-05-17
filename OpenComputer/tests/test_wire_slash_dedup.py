"""Regression — ``slash.list`` must not emit duplicate command names.

Bug (found 2026-05-17 debugging the TUI slash palette): the slash
registry maps the canonical name AND every alias to the *same* command
instance, so ``get_registered_commands()`` (which returns
``slash_commands.values()``) yields that instance once per alias. The
``slash.list`` handler emitted one wire entry per occurrence, every copy
carrying the canonical ``.name`` — duplicate names on the wire. A TUI /
dashboard rendering them keyed by name then hits React duplicate-key
warnings, which corrupt the Ink display.

The fix dedupes in ``WireServer._collect_slash_commands`` — one entry per
canonical command, aliases preserved in the ``aliases`` field.
"""

from __future__ import annotations

import pytest


class _FakeCmd:
    """Minimal stand-in for a registered slash command."""

    def __init__(
        self, name: str, description: str = "", aliases: tuple[str, ...] = ()
    ) -> None:
        self.name = name
        self.description = description
        self.aliases = list(aliases)


def test_collect_slash_commands_dedupes_alias_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An aliased command appears once, not once-per-alias."""
    from opencomputer.gateway.wire_server import WireServer

    # The registry maps "compress" and its alias "c" to the SAME instance,
    # so values() yields it twice — exactly the production bug shape.
    compress = _FakeCmd("compress", "compact the session", aliases=("c",))
    registry_values = [compress, compress, _FakeCmd("retry", "retry the turn")]
    monkeypatch.setattr(
        "opencomputer.agent.slash_commands.get_registered_commands",
        lambda: registry_values,
    )

    out = WireServer._collect_slash_commands()
    names = [c["name"] for c in out]

    assert len(names) == len(set(names)), f"duplicate command names: {names}"
    assert sorted(names) == ["compress", "retry"]
    # The alias is preserved on the surviving entry, not lost.
    compress_entry = next(c for c in out if c["name"] == "compress")
    assert "c" in compress_entry["aliases"]


def test_collect_slash_commands_degrades_on_registry_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry that raises → empty list, never an exception."""
    from opencomputer.gateway.wire_server import WireServer

    def boom() -> list:
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(
        "opencomputer.agent.slash_commands.get_registered_commands", boom
    )
    assert WireServer._collect_slash_commands() == []
