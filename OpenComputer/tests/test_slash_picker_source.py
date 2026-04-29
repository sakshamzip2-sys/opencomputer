"""Tests for UnifiedSlashSource — the picker's source-of-truth for
mixed command + skill rows."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opencomputer.cli_ui.slash import CommandDef, SkillEntry
from opencomputer.cli_ui.slash_mru import MruStore
from opencomputer.cli_ui.slash_picker_source import UnifiedSlashSource


@dataclass
class _FakeSkillMeta:
    """Minimal SkillMeta-shaped object — UnifiedSlashSource duck-types
    against `id`, `name`, `description`."""

    id: str
    name: str
    description: str = ""


class _FakeMemory:
    def __init__(self, skills: list) -> None:
        self._skills = skills

    def list_skills(self):
        return list(self._skills)


def test_iter_items_yields_commands_then_skills(tmp_path: Path) -> None:
    """All commands from SLASH_REGISTRY plus all skills from the
    fake MemoryManager appear as SlashItem rows."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="my-skill", name="My Skill", description="Hello"),
            _FakeSkillMeta(id="other", name="Other", description="World"),
        ]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    items = list(src.iter_items())
    cmds = [i for i in items if isinstance(i, CommandDef)]
    skills = [i for i in items if isinstance(i, SkillEntry)]
    assert len(cmds) >= 14  # registry size — exact value asserted in slash.py tests
    assert len(skills) == 2
    assert any(s.id == "my-skill" for s in skills)


def test_command_beats_skill_on_id_collision(tmp_path: Path) -> None:
    """If a skill happens to share an id with a command name, the
    command wins; the skill is hidden from the dropdown."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="help", name="help", description="Skill that shadows /help"),
            _FakeSkillMeta(id="unique-skill", name="Unique", description="OK"),
        ]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    items = list(src.iter_items())
    skill_ids = {i.id for i in items if isinstance(i, SkillEntry)}
    assert "help" not in skill_ids  # collided — hidden
    assert "unique-skill" in skill_ids


def test_iter_items_handles_memory_failure(tmp_path: Path) -> None:
    """If list_skills() raises, the picker still yields commands."""

    class _FailingMemory:
        def list_skills(self):
            raise RuntimeError("boom")

    src = UnifiedSlashSource(_FailingMemory(), MruStore(tmp_path / "mru.json"))
    items = list(src.iter_items())
    assert all(isinstance(i, CommandDef) for i in items)
    assert len(items) >= 14


def test_skill_with_missing_description_uses_empty_string(tmp_path: Path) -> None:
    """SkillMeta might not have a description — must default to ''
    not raise."""

    @dataclass
    class _Bare:
        id: str
        name: str

    mem = _FakeMemory([_Bare(id="bare", name="Bare")])
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    items = [i for i in src.iter_items() if isinstance(i, SkillEntry)]
    assert any(s.id == "bare" and s.description == "" for s in items)


def test_iter_items_skips_skill_with_falsy_id(tmp_path: Path) -> None:
    """BLOCKER C2 — a SkillMeta with empty / None id must not surface
    in the dropdown. Skipping protects against a malformed frontmatter."""

    @dataclass
    class _Bad:
        id: str
        name: str

    mem = _FakeMemory(
        [
            _Bad(id="", name="empty"),
            _Bad(id="valid", name="ok"),
        ]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    skill_ids = {i.id for i in src.iter_items() if isinstance(i, SkillEntry)}
    assert skill_ids == {"valid"}


def test_iter_items_skips_skill_with_unsafe_id_chars(tmp_path: Path) -> None:
    """BLOCKER B4 — skill ids with whitespace or non-shell-safe chars
    can't be invoked as `/<id>` (the picker's space-guard kicks in or
    the user can't type the id). Skip them."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="my skill", name="space"),       # space
            _FakeSkillMeta(id="bad/slash", name="slash"),       # slash
            _FakeSkillMeta(id="emoji-✓", name="emoji"),         # non-ascii
            _FakeSkillMeta(id="ok-id_2", name="alpha-numeric"),  # safe
        ]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    ids = {i.id for i in src.iter_items() if isinstance(i, SkillEntry)}
    assert "ok-id_2" in ids
    assert "my skill" not in ids
    assert "bad/slash" not in ids
    assert "emoji-✓" not in ids


def test_skill_collides_with_command_alias_hidden(tmp_path: Path) -> None:
    """BLOCKER C1 — alias collision: a skill named `quit` collides with
    the `/quit` alias of `/exit`. Command (and its aliases) win."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="quit", name="quit"),  # /quit is alias of /exit
            _FakeSkillMeta(id="reset", name="reset"),  # /reset is alias of /clear
            _FakeSkillMeta(id="legit-skill", name="ok"),
        ]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    skill_ids = {i.id for i in src.iter_items() if isinstance(i, SkillEntry)}
    assert "quit" not in skill_ids
    assert "reset" not in skill_ids
    assert "legit-skill" in skill_ids
