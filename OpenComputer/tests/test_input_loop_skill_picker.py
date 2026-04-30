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


def test_refilter_capped_at_default(tmp_path: Path) -> None:
    """The dropdown caps at the picker's _DEFAULT_TOP_N. Longer matches truncate."""
    from opencomputer.cli_ui.slash_picker_source import _DEFAULT_TOP_N
    mem = _FakeMemory(
        [_FakeSkillMeta(id=f"skill-{i:03d}", name=f"skill-{i:03d}") for i in range(_DEFAULT_TOP_N + 30)]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("skill")
    assert len(matches) == _DEFAULT_TOP_N


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


# ─── Task 8: dropdown rendering ────────────────────────────────────


def test_dropdown_text_renders_skill_with_skill_tag() -> None:
    """The internal _render_dropdown_for_state helper renders SkillEntry
    rows with a (skill) tag instead of a category."""
    state = {
        "matches": [
            CommandDef(name="rename", description="Set a friendly title", category="session"),
            SkillEntry(id="my-skill", name="My Skill", description="Use when foo"),
        ],
        "selected_idx": 0,
        "mode": "slash",
        "at_token_range": None,
    }
    rendered = _render_dropdown(state)
    rendered_text = "".join(text for _cls, text in rendered)
    assert "/rename" in rendered_text
    assert "/my-skill" in rendered_text
    # Source tag for the skill must say (skill).
    assert "(skill)" in rendered_text
    assert "(command)" in rendered_text  # rename is a command


def test_dropdown_text_truncates_descriptions_at_250() -> None:
    """Long descriptions get word-boundary truncated with ellipsis."""
    long = "this is a very long description " * 20  # ~640 chars
    state = {
        "matches": [SkillEntry(id="long", name="long", description=long)],
        "selected_idx": 0,
        "mode": "slash",
        "at_token_range": None,
    }
    rendered = _render_dropdown(state)
    text = "".join(t for _c, t in rendered)
    # Original 640 chars must not appear in full.
    assert long not in text
    # Trimmed form ends in ellipsis.
    assert "…" in text


def _render_dropdown(state):
    """Tap into input_loop's lifted module-level renderer."""
    from opencomputer.cli_ui.input_loop import _render_dropdown_for_state

    return _render_dropdown_for_state(state)


def test_style_dict_includes_command_and_skill_tags() -> None:
    """The style dict must define dd.tag.command and dd.tag.skill so
    the dropdown can render them with distinct colors."""
    import inspect

    from opencomputer.cli_ui import input_loop

    src = inspect.getsource(input_loop.read_user_input)
    assert '"dd.tag.command"' in src or "'dd.tag.command'" in src
    assert '"dd.tag.skill"' in src or "'dd.tag.skill'" in src
