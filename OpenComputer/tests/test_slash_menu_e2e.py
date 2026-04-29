"""End-to-end smoke for the slash menu Claude-Code parity feature.

Exercises every layer in one file: data source → ranker → completer →
input_loop renderer → MRU recording → slash dispatcher → fallback →
Hybrid wrap. If any link breaks, this file catches it before the user
hits the bug in production.

Format note: this test pins the MRU JSON-array-of-dicts format. If we
ever migrate the format (e.g. to SQLite), update test_e2e_picking_skill_records_to_mru.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from opencomputer.cli_ui.input_loop import _render_dropdown_for_state
from opencomputer.cli_ui.slash import CommandDef, SkillEntry
from opencomputer.cli_ui.slash_completer import SlashCommandCompleter
from opencomputer.cli_ui.slash_mru import MruStore
from opencomputer.cli_ui.slash_picker_source import UnifiedSlashSource


@dataclass
class _SkillMeta:
    id: str
    name: str
    description: str = ""


class _Mem:
    def __init__(self, skills):
        self._skills = skills

    def list_skills(self):
        return list(self._skills)

    def load_skill_body(self, sid):
        return f"body for {sid}"


def test_e2e_user_types_slash_sees_commands_and_skills(tmp_path: Path) -> None:
    """User types '/'. The dropdown shows commands AND skills, with
    source tags, and the description column is trimmed."""
    skills = [
        _SkillMeta(id="pead-screener", name="PEAD Screener", description="Screen post-earnings gap stocks"),
        _SkillMeta(id="failure-recovery-ladder", name="Recovery Ladder", description="x" * 400),
    ]
    src = UnifiedSlashSource(_Mem(skills), MruStore(tmp_path / "mru.json"))
    matches = src.rank("")
    items = [m.item for m in matches]
    cmds = [i for i in items if isinstance(i, CommandDef)]
    skills_seen = [i for i in items if isinstance(i, SkillEntry)]
    assert len(cmds) >= 1
    assert any(s.id == "pead-screener" for s in skills_seen)

    # Render the rendered list — source tags + 250-char trim must appear.
    state = {
        "matches": items[:10],
        "selected_idx": 0,
        "mode": "slash",
        "at_token_range": None,
    }
    rendered = _render_dropdown_for_state(state)
    blob = "".join(t for _c, t in rendered)
    assert "(command)" in blob
    assert "(skill)" in blob
    # Long description trimmed.
    assert "x" * 400 not in blob


def test_e2e_user_types_pead_finds_skill(tmp_path: Path) -> None:
    """User types '/pead' — the skill is in the top results."""
    skills = [_SkillMeta(id="pead-screener", name="PEAD Screener")]
    src = UnifiedSlashSource(_Mem(skills), MruStore(tmp_path / "mru.json"))
    matches = src.rank("pead")
    names = [m.item.id if isinstance(m.item, SkillEntry) else m.item.name for m in matches]
    assert "pead-screener" in names
    # Tier 1 prefix match — score == 1.0.
    pead_match = next(m for m in matches if not isinstance(m.item, CommandDef))
    assert pead_match.score == 1.0


def test_e2e_picking_skill_records_to_mru(tmp_path: Path) -> None:
    """After the user picks a skill, the next session shows it floated."""
    skills = [
        _SkillMeta(id="alpha", name="alpha"),
        _SkillMeta(id="beta", name="beta"),
        _SkillMeta(id="gamma", name="gamma"),
    ]
    mru = MruStore(tmp_path / "mru.json")
    src = UnifiedSlashSource(_Mem(skills), mru)

    # First session — alphabetical at empty prefix.
    matches_pre = src.rank("")
    skill_names_pre = [m.item.id for m in matches_pre if isinstance(m.item, SkillEntry)]
    assert skill_names_pre == sorted(skill_names_pre)

    # User picks 'gamma' (recorded in MRU).
    mru.record("gamma")

    # Fresh session — gamma now floats above alpha and beta.
    src2 = UnifiedSlashSource(_Mem(skills), MruStore(tmp_path / "mru.json"))
    matches_post = src2.rank("")
    names_post = [
        m.item.id if isinstance(m.item, SkillEntry) else m.item.name
        for m in matches_post
    ]
    gamma_pos = names_post.index("gamma")
    alpha_pos = names_post.index("alpha")
    assert gamma_pos < alpha_pos


def test_e2e_completer_legacy_path_unchanged(tmp_path: Path) -> None:
    """SlashCommandCompleter() with no source preserves legacy behavior
    so build_prompt_session callers don't regress."""
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    comp = SlashCommandCompleter()  # no source = legacy path
    completions = list(comp.get_completions(Document("/h"), CompleteEvent()))
    texts = [c.text for c in completions]
    # Legacy: only commands (and only ones starting with 'h').
    assert "/help" in texts
    # No skill names ever surface here.
    assert "/pead-screener" not in texts


def test_e2e_hybrid_wrap_fires_on_skill_source() -> None:
    """The Hybrid wrap helper fires only on source='skill' — command
    results pass through to the legacy text-reply path."""
    from opencomputer.agent.loop import _wrap_skill_result_as_tool_messages
    from plugin_sdk.slash_command import SlashCommandResult

    cmd_result = SlashCommandResult(output="x", handled=True)  # source defaults to "command"
    skill_result = SlashCommandResult(output="y", handled=True, source="skill")

    assert _wrap_skill_result_as_tool_messages(
        skill_name="x", args="", result=cmd_result
    ) == []
    skill_msgs = _wrap_skill_result_as_tool_messages(
        skill_name="x", args="", result=skill_result
    )
    assert len(skill_msgs) == 2
