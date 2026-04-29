"""Integration tests for the unified slash picker inside read_user_input.

The Application+layout layer is hard to drive headlessly, so these
tests exercise the inner _refilter function via the same
UnifiedSlashSource the production path constructs. Layout rendering
is covered by snapshot-style tests in Task 8 (via the lifted-to-
module-level _render_dropdown_for_state helper).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opencomputer.cli_ui.slash import CommandDef, SkillEntry
from opencomputer.cli_ui.slash_mru import MruStore
from opencomputer.cli_ui.slash_picker_source import UnifiedSlashSource


@dataclass
class _FakeSkillMeta:
    id: str
    name: str
    description: str = ""


class _FakeMemory:
    def __init__(self, skills):
        self._skills = skills

    def list_skills(self):
        return list(self._skills)


def test_refilter_empty_slash_returns_commands_and_skills(tmp_path: Path) -> None:
    """Typing just '/' shows everything — commands AND skills mixed."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="skill-a", name="Skill A"),
            _FakeSkillMeta(id="skill-b", name="Skill B"),
        ]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("")
    items = [m.item for m in matches]
    cmds = [i for i in items if isinstance(i, CommandDef)]
    skills = [i for i in items if isinstance(i, SkillEntry)]
    assert len(cmds) >= 1, "commands missing from empty-prefix dropdown"
    assert len(skills) == 2, "skills missing from empty-prefix dropdown"


def test_refilter_capped_at_20(tmp_path: Path) -> None:
    """The dropdown caps at 20 visible rows. Longer matches truncate."""
    mem = _FakeMemory(
        [_FakeSkillMeta(id=f"skill-{i:03d}", name=f"skill-{i:03d}") for i in range(50)]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("skill")
    assert len(matches) == 20


def test_refilter_re_ranks_correctly(tmp_path: Path) -> None:
    """`/re` shows tier-1 commands above word-boundary skill matches."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="code-review", name="Code Review"),
            _FakeSkillMeta(id="never-uses-re", name="never-uses-re"),
        ]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("re")
    names = [m.item.name if isinstance(m.item, CommandDef) else m.item.id for m in matches]
    # Tier-1 commands appear before tier-3 skills.
    rename_pos = names.index("rename")
    code_review_pos = names.index("code-review")
    assert rename_pos < code_review_pos
