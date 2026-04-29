"""Tests for the SkillEntry + SlashItem types added in slash.py for the
unified picker source."""
from __future__ import annotations

from opencomputer.cli_ui.slash import CommandDef, SkillEntry, SlashItem


def test_skillentry_is_frozen_dataclass() -> None:
    s = SkillEntry(id="my-skill", name="My Skill", description="Hello")
    # frozen — assignment must raise
    import dataclasses
    assert dataclasses.is_dataclass(s)
    try:
        s.id = "changed"  # type: ignore[misc]
        raise AssertionError("expected FrozenInstanceError")
    except dataclasses.FrozenInstanceError:
        pass


def test_skillentry_required_fields() -> None:
    s = SkillEntry(id="x", name="X", description="")
    assert s.id == "x"
    assert s.name == "X"
    assert s.description == ""


def test_slashitem_union_accepts_both() -> None:
    # SlashItem is a type alias — usable wherever Union[CommandDef, SkillEntry] is.
    items: list[SlashItem] = []
    items.append(CommandDef(name="exit", description="Exit"))
    items.append(SkillEntry(id="my-skill", name="My Skill", description="Hello"))
    assert len(items) == 2
    # Each variant should be distinguishable by isinstance.
    assert isinstance(items[0], CommandDef)
    assert isinstance(items[1], SkillEntry)
    assert not isinstance(items[0], SkillEntry)
    assert not isinstance(items[1], CommandDef)


def test_existing_commanddef_unchanged() -> None:
    """Existing CommandDef fields + defaults must not regress."""
    c = CommandDef(name="help", description="Show help")
    assert c.name == "help"
    assert c.description == "Show help"
    assert c.category == "general"
    assert c.aliases == ()
    assert c.args_hint == ""
