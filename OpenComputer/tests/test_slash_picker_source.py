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


# ─── Ranking tests (Task 4) ────────────────────────────────────────


def _name_of(item) -> str:
    """Test helper — extract the rendering name from a SlashItem."""
    if isinstance(item, CommandDef):
        return item.name
    if isinstance(item, SkillEntry):
        return item.id
    raise AssertionError(f"unknown item type: {type(item)}")


def test_rank_empty_prefix_returns_all_alphabetical(tmp_path: Path) -> None:
    """Spec §3.4: empty prefix bypasses ranking and returns all items
    sorted alphabetically (post-MRU sort applied in Task 5)."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="zebra-skill", name="Zebra", description=""),
            _FakeSkillMeta(id="alpha-skill", name="Alpha", description=""),
        ]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("")
    names = [_name_of(m.item) for m in matches]
    # Alphabetical across BOTH kinds (commands + skills mixed).
    assert names == sorted(names)


def test_rank_tier1_prefix_match(tmp_path: Path) -> None:
    """Items whose canonical name starts with prefix (case-insensitive)
    score 1.0 — Tier 1."""
    mem = _FakeMemory([])
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("re")
    # All tier-1 hits — these come from SLASH_REGISTRY: rename, reload,
    # reload-mcp, resume.
    tier1_names = [_name_of(m.item) for m in matches if m.score == 1.0]
    assert "rename" in tier1_names
    assert "reload" in tier1_names
    assert "reload-mcp" in tier1_names
    assert "resume" in tier1_names


def test_rank_tier2_alias_match(tmp_path: Path) -> None:
    """Aliases that start with prefix score 0.85 — Tier 2."""
    mem = _FakeMemory([])
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("res")
    # `/clear` has alias `reset` — `res` matches alias not canonical.
    found = [m for m in matches if _name_of(m.item) == "clear"]
    assert len(found) == 1
    assert found[0].score == 0.85


def test_rank_tier3_word_boundary(tmp_path: Path) -> None:
    """Word-boundary substring matches score 0.70 — Tier 3.
    'review' inside 'code-review' starts a new word."""
    mem = _FakeMemory(
        [_FakeSkillMeta(id="code-review", name="Code Review", description="")]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("rev")
    found = [m for m in matches if _name_of(m.item) == "code-review"]
    assert len(found) == 1
    assert found[0].score == 0.70


def test_rank_tier4_anywhere_substring(tmp_path: Path) -> None:
    """Anywhere-in-name substring matches score 0.55 — Tier 4."""
    mem = _FakeMemory(
        [_FakeSkillMeta(id="learning-mode", name="learning-mode", description="")]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("ning")  # mid-word substring
    found = [m for m in matches if _name_of(m.item) == "learning-mode"]
    assert len(found) == 1
    assert found[0].score == 0.55


def test_rank_tier5_fuzzy_typo(tmp_path: Path) -> None:
    """Typo-tolerance via difflib — score in 0.40-0.50 range."""
    mem = _FakeMemory(
        [_FakeSkillMeta(id="pead-screener", name="pead-screener", description="")]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("pad-screener")  # typo: pad instead of pead
    found = [m for m in matches if _name_of(m.item) == "pead-screener"]
    assert len(found) == 1
    assert 0.40 <= found[0].score <= 0.50


def test_rank_orders_by_score_desc(tmp_path: Path) -> None:
    """Higher score wins. Tier 1 above tier 3 above tier 4."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="reckon-skill", name="reckon-skill", description=""),
            _FakeSkillMeta(id="code-review", name="code-review", description=""),
        ]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    matches = src.rank("re")
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_rank_caps_at_top_n(tmp_path: Path) -> None:
    """Default cap is 20; passing top_n overrides."""
    mem = _FakeMemory(
        [_FakeSkillMeta(id=f"skill-{i:02d}", name=f"skill-{i:02d}", description="") for i in range(50)]
    )
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    assert len(src.rank("", top_n=20)) == 20
    assert len(src.rank("", top_n=5)) == 5


def test_rank_returns_empty_when_no_items(tmp_path: Path) -> None:
    """BLOCKER C4 — defensive: if SLASH_REGISTRY is empty AND list_skills
    returns nothing, rank returns an empty list — no crash."""
    import opencomputer.cli_ui.slash as slash_mod

    mem = _FakeMemory([])
    src = UnifiedSlashSource(mem, MruStore(tmp_path / "mru.json"))
    # Monkeypatch out the registry for this assertion only — restore after.
    real_registry = slash_mod.SLASH_REGISTRY
    try:
        slash_mod.SLASH_REGISTRY = []  # type: ignore[misc]
        # Empty prefix.
        assert src.rank("") == []
        # Non-empty prefix.
        assert src.rank("foo") == []
    finally:
        slash_mod.SLASH_REGISTRY = real_registry  # type: ignore[misc]


# ─── MRU bonus + empty-prefix MRU sort (Task 5) ────────────────────


def test_rank_mru_bonus_floats_recently_used_above_alphabetical(tmp_path: Path) -> None:
    """When two items have the same tier score, the MRU-recent one
    wins. Use prefix 'll' which is anywhere-substring (tier 4) for
    both 'apple-skill' and 'banana-skill' — neither starts with 'll'
    nor has 'll' at any word boundary."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="apple-skill", name="apple-skill", description=""),
            _FakeSkillMeta(id="banana-skill", name="banana-skill", description=""),
        ]
    )
    mru = MruStore(tmp_path / "mru.json")
    mru.record("banana-skill")  # banana was used recently
    src = UnifiedSlashSource(mem, mru)
    matches = src.rank("ll")  # both contain 'll' inside 'skill' → tier 4
    names = [_name_of(m.item) for m in matches]
    # banana-skill should rank above apple-skill thanks to MRU bonus
    # (otherwise alphabetical would put apple first).
    apple_pos = names.index("apple-skill")
    banana_pos = names.index("banana-skill")
    assert banana_pos < apple_pos


def test_rank_mru_bonus_does_not_exceed_one(tmp_path: Path) -> None:
    """A tier-1 match plus MRU bonus must cap at 1.0."""
    mem = _FakeMemory([])
    mru = MruStore(tmp_path / "mru.json")
    mru.record("rename")  # rename is a tier-1 prefix hit on "re"
    src = UnifiedSlashSource(mem, mru)
    matches = src.rank("re")
    rename_match = next(m for m in matches if _name_of(m.item) == "rename")
    assert rename_match.score == 1.0  # capped, not 1.05


def test_rank_empty_prefix_floats_mru_recent_above_alphabetical(tmp_path: Path) -> None:
    """Empty prefix: MRU items show first (top 5), then alphabetical."""
    mem = _FakeMemory(
        [_FakeSkillMeta(id=f"skill-{c}", name=f"skill-{c}", description="") for c in "abcdef"]
    )
    mru = MruStore(tmp_path / "mru.json")
    mru.record("skill-e")
    mru.record("skill-c")
    src = UnifiedSlashSource(mem, mru)
    matches = src.rank("", top_n=20)
    names = [_name_of(m.item) for m in matches]
    # MRU-recent show first — order is "most recent first" so skill-c then skill-e.
    assert names[0] == "skill-c"
    assert names[1] == "skill-e"
    # The rest are alphabetical: skill-a, skill-b, skill-d, skill-f, then commands.
    rest_skills = [n for n in names[2:] if n.startswith("skill-")]
    assert rest_skills == sorted(rest_skills)


def test_rank_mru_top_5_cap(tmp_path: Path) -> None:
    """Empty prefix: only top 5 MRU items are floated; the 6th+ MRU
    items appear in the alphabetical tail."""
    mem = _FakeMemory(
        [_FakeSkillMeta(id=f"s-{i:02d}", name=f"s-{i:02d}", description="") for i in range(10)]
    )
    mru = MruStore(tmp_path / "mru.json")
    # Record 7 items (most recent last).
    for i in range(7):
        mru.record(f"s-{i:02d}")
    src = UnifiedSlashSource(mem, mru)
    matches = src.rank("", top_n=20)
    names = [_name_of(m.item) for m in matches]
    # First 5 should be MRU items (last-recorded first): s-06, s-05, s-04, s-03, s-02.
    assert names[0] == "s-06"
    assert names[1] == "s-05"
    assert names[2] == "s-04"
    assert names[3] == "s-03"
    assert names[4] == "s-02"
    # s-00 and s-01 are MRU-but-not-top-5; they appear in the alphabetical tail.
    s00_pos = names.index("s-00")
    s01_pos = names.index("s-01")
    assert s00_pos > 4
    assert s01_pos > 4


def test_rank_empty_prefix_with_uninstalled_mru_entries(tmp_path: Path) -> None:
    """BLOCKER B2 (refined) — MRU file may reference skills the user
    has since uninstalled. Empty-prefix sort silently skips them rather
    than crashing or rendering ghost rows."""
    mem = _FakeMemory(
        [_FakeSkillMeta(id="still-here", name="still-here")]
    )
    mru = MruStore(tmp_path / "mru.json")
    mru.record("uninstalled-skill")  # not in the current skill list
    mru.record("still-here")
    src = UnifiedSlashSource(mem, mru)
    matches = src.rank("")
    names = [m.item.id for m in matches if isinstance(m.item, SkillEntry)]
    assert "still-here" in names
    assert "uninstalled-skill" not in names  # silently skipped
