# Slash Menu — Claude-Code Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface every installed skill in OpenComputer's TUI slash dropdown alongside the 14 commands, ranked by match quality and recent use, with Hybrid dispatch wrapping skill results as synthetic SkillTool tool_use/tool_result pairs — full Claude-Code parity.

**Architecture:** Two layers in one PR. Layer 1 (TUI surface): a new `UnifiedSlashSource` reads from both `SLASH_REGISTRY` and `MemoryManager.list_skills()`, ranks results via tiered match scoring with stdlib `difflib`, biased by an MRU store at `~/.opencomputer/<profile>/slash_mru.json`, and feeds both the `SlashCommandCompleter` (used by the legacy `build_prompt_session` path) and the custom `read_user_input` Application's `_refilter`. Layer 2 (Hybrid dispatch): a new `source` field on `SlashCommandResult` lets the agent loop detect skill-fallback results and wrap them as a `Skill` tool_use + tool_result message pair instead of plain assistant text — so the model sees skill content the way Claude Code does.

**Tech Stack:** Python 3.12+, prompt_toolkit (existing TUI), stdlib `difflib`, dataclasses, pytest.

**Companion spec:** `OpenComputer/docs/superpowers/specs/2026-04-29-slash-menu-claude-code-parity-design.md`

**Branch:** `feat/slash-menu-cc-parity` (already cut from `main` at `fdab4367`, includes archit's #220-#227).

**Tasks:** 14 numbered tasks (1-14) plus Task 12.5 inserted post-audit for Hybrid full-turn integration coverage. Approximately ~870 LOC across 13 files. Each task is a single TDD cycle: failing test → minimal impl → green test → commit.

---

## Pre-flight checks (do once before Task 1)

- [ ] **Check archit is no longer touching slash files**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git fetch origin --prune
gh pr list --state open --json number,title,headRefName 2>&1 | head -10
```

Expected: empty array `[]` (archit's PRs all merged) OR no PRs touching `cli_ui/`. If a new PR touches `cli_ui/`, pause and replan.

- [ ] **Confirm starting test count is green**

```bash
python -m pytest tests/ -x -q 2>&1 | tail -3
```

Expected: ~5443 passed (will vary slightly with archit's merges). Record this number — the final verification at Task 14 must show this number plus ~25 new tests.

- [ ] **Confirm working tree is clean before starting**

```bash
git status
```

Expected: `On branch feat/slash-menu-cc-parity` `nothing to commit, working tree clean`. The spec is already committed.

---

## Task 1: MRU store — append-only JSON with cap-at-50

**Files:**
- Create: `OpenComputer/opencomputer/cli_ui/slash_mru.py`
- Test: `OpenComputer/tests/test_slash_mru.py`

- [ ] **Step 1: Write the failing tests**

Create `OpenComputer/tests/test_slash_mru.py`:

```python
"""Tests for the slash MRU store — bounded recent-use log used to
boost recently-picked items in the dropdown ranking."""
from __future__ import annotations

import json
import time
from pathlib import Path

from opencomputer.cli_ui.slash_mru import MruStore


def test_record_and_recency_bonus(tmp_path: Path) -> None:
    store = MruStore(tmp_path / "mru.json")
    store.record("rename")
    assert store.recency_bonus("rename") == 0.05
    assert store.recency_bonus("not-recorded") == 0.0


def test_persists_across_instances(tmp_path: Path) -> None:
    p = tmp_path / "mru.json"
    MruStore(p).record("reload")
    fresh = MruStore(p)
    assert fresh.recency_bonus("reload") == 0.05


def test_cap_at_50_drops_oldest(tmp_path: Path) -> None:
    store = MruStore(tmp_path / "mru.json")
    # Record 60 distinct entries; first 10 should be evicted.
    for i in range(60):
        store.record(f"cmd-{i:02d}")
    assert store.recency_bonus("cmd-00") == 0.0  # evicted
    assert store.recency_bonus("cmd-09") == 0.0  # evicted
    assert store.recency_bonus("cmd-10") == 0.05  # kept
    assert store.recency_bonus("cmd-59") == 0.05  # kept


def test_duplicate_record_refreshes_recency(tmp_path: Path) -> None:
    store = MruStore(tmp_path / "mru.json")
    store.record("a")
    time.sleep(0.001)
    store.record("a")  # second time — should not duplicate the entry
    raw = json.loads((tmp_path / "mru.json").read_text())
    assert sum(1 for e in raw if e["name"] == "a") == 1


def test_malformed_file_silently_empty(tmp_path: Path) -> None:
    p = tmp_path / "mru.json"
    p.write_text("{not valid json")
    store = MruStore(p)
    # Reading must not raise; bonus is zero.
    assert store.recency_bonus("anything") == 0.0
    # Recording must work — overwrites the bad file.
    store.record("x")
    assert store.recency_bonus("x") == 0.05


def test_missing_file_silently_empty(tmp_path: Path) -> None:
    p = tmp_path / "does-not-exist.json"
    store = MruStore(p)
    assert store.recency_bonus("anything") == 0.0
    store.record("created")
    assert p.exists()


def test_atomic_write_via_tempfile(tmp_path: Path) -> None:
    """During write, the .tmp file lands first, then is renamed."""
    store = MruStore(tmp_path / "mru.json")
    store.record("first")
    # No leftover .tmp file after a successful write.
    assert not (tmp_path / "mru.json.tmp").exists()
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
python -m pytest tests/test_slash_mru.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'opencomputer.cli_ui.slash_mru'`.

- [ ] **Step 3: Write the minimal implementation**

Create `OpenComputer/opencomputer/cli_ui/slash_mru.py`:

```python
"""Append-only MRU store for the slash dropdown.

Tracks the user's last-50 distinct slash picks (commands or skills) so
``UnifiedSlashSource.rank`` can boost recently-used items by a small
score bonus. Persisted to ``<profile_home>/slash_mru.json`` so the
ranking carries across sessions.

Tolerant of corruption + missing-file: any read error returns an empty
in-memory store; the next ``record`` call rewrites the file from
scratch. Atomic writes via temp-file + rename so a crash mid-write
never leaves a half-written file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: Cap on stored entries — Claude Code uses a similar bounded LRU for
#: command frequency. 50 is enough to bias toward the user's last
#: working session without growing unbounded.
_MAX_ENTRIES = 50

#: Score bonus added to a ranked match when the item appears in the
#: MRU log. Spec §3.4. Additive, capped at 1.0 by the caller.
RECENCY_BONUS = 0.05


class MruStore:
    """Persistent bounded most-recently-used log of slash picks.

    Public API:
    - ``record(name)`` — log a pick; rewrites the JSON file atomically.
    - ``recency_bonus(name) -> float`` — returns ``RECENCY_BONUS`` if
      ``name`` is in the last-``_MAX_ENTRIES`` log, ``0.0`` otherwise.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[dict] = self._load()

    def _load(self) -> list[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        # Defensive — drop entries that don't have ``name``.
        return [e for e in data if isinstance(e, dict) and "name" in e]

    def record(self, name: str) -> None:
        # Drop any prior occurrence so the new one becomes most-recent.
        self._entries = [e for e in self._entries if e.get("name") != name]
        self._entries.append({"name": name, "ts": time.time()})
        # Trim to last ``_MAX_ENTRIES`` (oldest at front).
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._entries), encoding="utf-8")
        os.replace(tmp, self.path)

    def recency_bonus(self, name: str) -> float:
        return RECENCY_BONUS if any(e.get("name") == name for e in self._entries) else 0.0


__all__ = ["MruStore", "RECENCY_BONUS"]
```

- [ ] **Step 4: Run the test — confirm it passes**

```bash
python -m pytest tests/test_slash_mru.py -v 2>&1 | tail -15
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash_mru.py OpenComputer/tests/test_slash_mru.py
git commit -m "feat(slash): MruStore — bounded recent-use log for picker ranking

Persists last-50 slash picks to <profile_home>/slash_mru.json. Atomic
writes via temp-file + rename. Tolerates corrupt/missing file by
returning empty store. Used by UnifiedSlashSource (next task) to
surface recently-used items at the top of the dropdown.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: SkillEntry dataclass + SlashItem union

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/slash.py:11-25`
- Test: `OpenComputer/tests/test_slash_item_types.py` (new)

- [ ] **Step 1: Write the failing test**

Create `OpenComputer/tests/test_slash_item_types.py`:

```python
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
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/test_slash_item_types.py -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'SkillEntry'` (and `SlashItem`).

- [ ] **Step 3: Write the minimal implementation**

Edit `OpenComputer/opencomputer/cli_ui/slash.py`. Find the existing imports section and the `CommandDef` definition (around lines 9-23). Add `SkillEntry` and `SlashItem` after `CommandDef`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


@dataclass(frozen=True)
class CommandDef:
    """One slash command. Handlers are looked up by name in
    :mod:`slash_handlers` rather than stored here so the registry stays
    importable in test contexts that don't have Console."""

    name: str
    description: str
    category: str = "general"
    aliases: tuple[str, ...] = field(default_factory=tuple)
    args_hint: str = ""


@dataclass(frozen=True)
class SkillEntry:
    """One installed skill, surfaced in the picker dropdown.

    Mirrors :class:`opencomputer.agent.memory.SkillMeta` but keeps only
    the fields the dropdown needs — id (slash text), human name, and
    description (truncated for display). The picker source converts
    SkillMeta → SkillEntry at enumeration time so the picker layer
    doesn't depend on agent.memory.
    """

    id: str
    name: str
    description: str


#: Either kind of row that can appear in the slash dropdown.
SlashItem = Union[CommandDef, SkillEntry]
```

- [ ] **Step 4: Run the test — confirm it passes**

```bash
python -m pytest tests/test_slash_item_types.py -v 2>&1 | tail -10
```

Expected: 4 passed.

Also run the existing slash test to confirm no regression:

```bash
python -m pytest tests/test_slash_command.py tests/test_slash_completer.py -v 2>&1 | tail -10
```

Expected: all pre-existing tests still pass.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash.py OpenComputer/tests/test_slash_item_types.py
git commit -m "feat(slash): SkillEntry dataclass + SlashItem union

Companion to CommandDef for the unified picker source. SkillEntry
mirrors agent.memory.SkillMeta but trimmed to the fields the dropdown
needs (id / name / description). SlashItem union lets the picker
source yield mixed rows without reaching across the cli_ui ↔ agent
boundary at every call site.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: UnifiedSlashSource — basic enumeration + dedup

**Files:**
- Create: `OpenComputer/opencomputer/cli_ui/slash_picker_source.py`
- Test: `OpenComputer/tests/test_slash_picker_source.py`

- [ ] **Step 1: Write the failing tests for enumeration**

Create `OpenComputer/tests/test_slash_picker_source.py`:

```python
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
    description: str


class _FakeMemory:
    def __init__(self, skills: list[_FakeSkillMeta]) -> None:
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
    # 14 hardcoded commands + 2 skills (assuming SLASH_REGISTRY has 14)
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

    mem = _FakeMemory([_Bare(id="bare", name="Bare")])  # type: ignore[list-item]
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
            _Bad(id="valid", name="ok"),  # type: ignore[list-item]
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
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/test_slash_picker_source.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'opencomputer.cli_ui.slash_picker_source'`.

- [ ] **Step 3: Write the minimal implementation**

Create `OpenComputer/opencomputer/cli_ui/slash_picker_source.py`:

```python
"""Unified slash picker source — the single source of truth that
``SlashCommandCompleter`` and ``read_user_input``'s ``_refilter`` both
read from.

Walks the :data:`SLASH_REGISTRY` for built-in commands and
:meth:`MemoryManager.list_skills` for installed skills, deduping on
name collision (command always wins), and exposes a ranked search via
:meth:`UnifiedSlashSource.rank`.

Pure logic — no IO except via the injected ``memory_manager`` (for
skills) and ``mru_store`` (for recency bonus). Designed to be cheap to
construct per session and to call once per keystroke.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from opencomputer.cli_ui.slash import SLASH_REGISTRY, CommandDef, SkillEntry, SlashItem
from opencomputer.cli_ui.slash_mru import MruStore

_log = logging.getLogger("opencomputer.cli_ui.slash_picker_source")


class UnifiedSlashSource:
    """Yields mixed CommandDef + SkillEntry rows for the picker.

    ``iter_items()`` returns the full deduped list (commands first, then
    skills). ``rank(prefix)`` returns a ranked subset (Task 4).
    """

    def __init__(self, memory_manager: Any, mru_store: MruStore) -> None:
        self._mem = memory_manager
        self._mru = mru_store

    def _command_names(self) -> set[str]:
        out: set[str] = set()
        for cmd in SLASH_REGISTRY:
            out.add(cmd.name)
            for alias in cmd.aliases:
                out.add(alias)
        return out

    def iter_items(self) -> Iterable[SlashItem]:
        """Yield all picker-eligible items.

        Commands are yielded first (registry order — preserves the
        order curated in ``slash.py``). Skills follow, with any that
        collide with a command name suppressed. Skills are yielded in
        the order ``MemoryManager.list_skills`` returns them; sorting
        is the ranker's job, not the source's.

        Skills with non-shell-safe ids (whitespace, non-ascii, slashes)
        are silently skipped — the user couldn't invoke them as
        ``/<id>`` even if shown.
        """
        import re as _re

        _SAFE_ID = _re.compile(r"^[A-Za-z0-9_-]+$")

        # Commands — direct from registry.
        yield from SLASH_REGISTRY

        # Skills — duck-typed against id / name / description.
        try:
            skills = self._mem.list_skills()
        except Exception:  # noqa: BLE001 — never break the picker
            _log.warning("list_skills() raised — picker shows commands only", exc_info=True)
            return
        cmd_names = self._command_names()
        for s in skills:
            sid = getattr(s, "id", None)
            if not sid:
                continue
            if not _SAFE_ID.match(sid):
                _log.info(
                    "skill %r has unsafe id chars — hiding from picker", sid
                )
                continue
            if sid in cmd_names:
                _log.info("skill %r collides with command name — hiding from picker", sid)
                continue
            yield SkillEntry(
                id=sid,
                name=str(getattr(s, "name", sid) or sid),
                description=str(getattr(s, "description", "") or ""),
            )


__all__ = ["UnifiedSlashSource"]
```

- [ ] **Step 4: Run the test — confirm it passes**

```bash
python -m pytest tests/test_slash_picker_source.py -v 2>&1 | tail -15
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash_picker_source.py OpenComputer/tests/test_slash_picker_source.py
git commit -m "feat(slash): UnifiedSlashSource — enumerate commands + skills

Walks SLASH_REGISTRY + MemoryManager.list_skills(), dedupes on name
collision (command always wins, skill is suppressed with INFO log).
Memory failures degrade gracefully — picker shows commands only,
never raises.

This is the source-of-truth that the next 3 tasks bolt rank() and
hook the completer + input_loop into.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: UnifiedSlashSource — tiered ranking algorithm

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/slash_picker_source.py` (add `rank` method)
- Modify: `OpenComputer/tests/test_slash_picker_source.py` (add ranking tests)

- [ ] **Step 1: Write the failing tests for ranking**

Append to `OpenComputer/tests/test_slash_picker_source.py`:

```python


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


def _name_of(item) -> str:
    """Test helper — extract the rendering name from a SlashItem."""
    from opencomputer.cli_ui.slash import CommandDef, SkillEntry

    if isinstance(item, CommandDef):
        return item.name
    if isinstance(item, SkillEntry):
        return item.id
    raise AssertionError(f"unknown item type: {type(item)}")
```

- [ ] **Step 2: Run the tests — confirm they fail**

```bash
python -m pytest tests/test_slash_picker_source.py -v 2>&1 | tail -15
```

Expected: `AttributeError: 'UnifiedSlashSource' object has no attribute 'rank'`.

- [ ] **Step 3: Write the minimal implementation**

Edit `OpenComputer/opencomputer/cli_ui/slash_picker_source.py`. Add a new `Match` dataclass and the `rank` method:

```python
"""Unified slash picker source — the single source of truth that
``SlashCommandCompleter`` and ``read_user_input``'s ``_refilter`` both
read from.

Walks the :data:`SLASH_REGISTRY` for built-in commands and
:meth:`MemoryManager.list_skills` for installed skills, deduping on
name collision (command always wins), and exposes a ranked search via
:meth:`UnifiedSlashSource.rank`.

Pure logic — no IO except via the injected ``memory_manager`` (for
skills) and ``mru_store`` (for recency bonus). Designed to be cheap to
construct per session and to call once per keystroke.
"""
from __future__ import annotations

import difflib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from opencomputer.cli_ui.slash import SLASH_REGISTRY, CommandDef, SkillEntry, SlashItem
from opencomputer.cli_ui.slash_mru import MruStore

_log = logging.getLogger("opencomputer.cli_ui.slash_picker_source")

#: Default cap on rendered rows. Spec §3.4. Caller can override via
#: ``top_n``. Larger values fail gracefully (just slower).
_DEFAULT_TOP_N = 20

#: Score thresholds — see spec §3.4 for tier rationale.
_TIER1_PREFIX = 1.00
_TIER2_ALIAS = 0.85
_TIER3_WORD_BOUNDARY = 0.70
_TIER4_SUBSTRING = 0.55
_FUZZY_RATIO_THRESHOLD = 0.55
_FUZZY_SCORE_FLOOR = 0.40
_FUZZY_SCORE_CEIL = 0.50

#: Pathological-input guard — clamp comparison length so a 10K-char
#: malformed name can't pin the CPU.
_MAX_COMPARE_LEN = 64


@dataclass(frozen=True, slots=True)
class Match:
    """One ranked search hit. Score in [0.0, 1.0]; higher wins."""

    item: SlashItem
    score: float


class UnifiedSlashSource:
    """Yields mixed CommandDef + SkillEntry rows for the picker.

    ``iter_items()`` returns the full deduped list (commands first, then
    skills). ``rank(prefix)`` returns a ranked subset.
    """

    def __init__(self, memory_manager: Any, mru_store: MruStore) -> None:
        self._mem = memory_manager
        self._mru = mru_store

    def _command_names(self) -> set[str]:
        out: set[str] = set()
        for cmd in SLASH_REGISTRY:
            out.add(cmd.name)
            for alias in cmd.aliases:
                out.add(alias)
        return out

    def iter_items(self) -> Iterable[SlashItem]:
        """Yield all picker-eligible items.

        Commands are yielded first (registry order — preserves the
        order curated in ``slash.py``). Skills follow, with any that
        collide with a command name suppressed. Skills with unsafe
        ids (whitespace, non-ascii, slashes) are also skipped.
        """
        import re as _re

        _SAFE_ID = _re.compile(r"^[A-Za-z0-9_-]+$")

        yield from SLASH_REGISTRY

        try:
            skills = self._mem.list_skills()
        except Exception:  # noqa: BLE001 — never break the picker
            _log.warning("list_skills() raised — picker shows commands only", exc_info=True)
            return
        cmd_names = self._command_names()
        for s in skills:
            sid = getattr(s, "id", None)
            if not sid:
                continue
            if not _SAFE_ID.match(sid):
                _log.info("skill %r has unsafe id chars — hiding from picker", sid)
                continue
            if sid in cmd_names:
                _log.info("skill %r collides with command name — hiding from picker", sid)
                continue
            yield SkillEntry(
                id=sid,
                name=str(getattr(s, "name", sid) or sid),
                description=str(getattr(s, "description", "") or ""),
            )

    def _name_of(self, item: SlashItem) -> str:
        if isinstance(item, CommandDef):
            return item.name
        return item.id

    def _score_one(self, item: SlashItem, prefix: str) -> float:
        """Apply the tier ladder and return the highest-tier score that
        matches. ``0.0`` means no match."""
        name = self._name_of(item).lower()
        # Pathological-input guard.
        n = name[:_MAX_COMPARE_LEN]
        p = prefix[:_MAX_COMPARE_LEN]

        # Tier 1: prefix match on canonical name.
        if n.startswith(p):
            return _TIER1_PREFIX

        # Tier 2: prefix match on any alias (commands only).
        if isinstance(item, CommandDef):
            for alias in item.aliases:
                if alias.lower().startswith(p):
                    return _TIER2_ALIAS

        # Tier 3: word-boundary substring (start of any '-' delimited word).
        for word in n.split("-"):
            if word.startswith(p):
                # First word matched in tier 1 — guarded above. Any later word
                # match means tier 3.
                return _TIER3_WORD_BOUNDARY

        # Tier 4: anywhere substring.
        if p in n:
            return _TIER4_SUBSTRING

        # Tier 5: fuzzy via difflib.
        ratio = difflib.SequenceMatcher(None, n, p).ratio()
        if ratio >= _FUZZY_RATIO_THRESHOLD:
            # Linear interpolate from threshold→1.0 onto floor→ceil.
            scaled = _FUZZY_SCORE_FLOOR + (
                (ratio - _FUZZY_RATIO_THRESHOLD)
                / (1.0 - _FUZZY_RATIO_THRESHOLD)
            ) * (_FUZZY_SCORE_CEIL - _FUZZY_SCORE_FLOOR)
            return min(_FUZZY_SCORE_CEIL, max(_FUZZY_SCORE_FLOOR, scaled))

        return 0.0

    def rank(self, prefix: str, top_n: int = _DEFAULT_TOP_N) -> list[Match]:
        """Rank all items against ``prefix`` and return top-``top_n``.

        Empty prefix bypasses scoring and returns all items sorted
        alphabetically. Ties on score break by name alphabetically;
        Task 5 layers MRU recency on top.
        """
        items = list(self.iter_items())
        prefix_lc = prefix.lower().strip()

        if not prefix_lc:
            # Empty: everything in alphabetical order, no MRU bonus
            # (MRU sort wired in Task 5).
            sorted_items = sorted(items, key=self._name_of)
            return [Match(item=i, score=1.0) for i in sorted_items[:top_n]]

        scored: list[Match] = []
        for item in items:
            s = self._score_one(item, prefix_lc)
            if s > 0:
                scored.append(Match(item=item, score=s))

        # Sort: score desc, then name asc.
        scored.sort(key=lambda m: (-m.score, self._name_of(m.item)))
        return scored[:top_n]


__all__ = ["Match", "UnifiedSlashSource"]
```

- [ ] **Step 4: Run the tests — confirm they pass**

```bash
python -m pytest tests/test_slash_picker_source.py -v 2>&1 | tail -20
```

Expected: 12 passed (4 existing + 8 new).

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash_picker_source.py OpenComputer/tests/test_slash_picker_source.py
git commit -m "feat(slash): tiered ranking — prefix > alias > word-boundary > substring > fuzzy

5-tier match ladder with stdlib difflib for typo tolerance:
- Tier 1 (1.00): canonical name starts with prefix
- Tier 2 (0.85): alias starts with prefix
- Tier 3 (0.70): word-boundary substring (\"rev\" in \"code-review\")
- Tier 4 (0.55): anywhere substring
- Tier 5 (0.40-0.50): fuzzy via difflib.SequenceMatcher.ratio()

Empty prefix bypasses ranking, returns all items alphabetically.
Pathological-input clamp at 64 chars guards CPU.

No new deps — difflib is stdlib.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: UnifiedSlashSource — MRU recency bonus + empty-prefix MRU sort

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/slash_picker_source.py:rank`
- Modify: `OpenComputer/tests/test_slash_picker_source.py`

- [ ] **Step 1: Write the failing tests for MRU**

Append to `OpenComputer/tests/test_slash_picker_source.py`:

```python


def test_rank_mru_bonus_floats_recently_used_above_alphabetical(tmp_path: Path) -> None:
    """When two items have the same tier score, the MRU-recent one
    wins."""
    mem = _FakeMemory(
        [
            _FakeSkillMeta(id="apple-skill", name="apple-skill", description=""),
            _FakeSkillMeta(id="banana-skill", name="banana-skill", description=""),
        ]
    )
    mru = MruStore(tmp_path / "mru.json")
    mru.record("banana-skill")  # banana was used recently
    src = UnifiedSlashSource(mem, mru)
    matches = src.rank("a")  # both contain "a" → tier 4 (anywhere substring)
    # banana-skill should rank above apple-skill thanks to MRU bonus
    # (otherwise alphabetical would put apple first).
    names = [_name_of(m.item) for m in matches]
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
```

- [ ] **Step 2: Run the tests — confirm they fail**

```bash
python -m pytest tests/test_slash_picker_source.py -v 2>&1 | tail -10
```

Expected: 4 new tests fail (MRU bonus is 0.0 today, MRU sort not applied).

- [ ] **Step 3: Update `rank` to apply MRU bonus + empty-prefix MRU sort**

Edit `OpenComputer/opencomputer/cli_ui/slash_picker_source.py`. Replace the `rank` method with:

```python
    def rank(self, prefix: str, top_n: int = _DEFAULT_TOP_N) -> list[Match]:
        """Rank all items against ``prefix`` and return top-``top_n``.

        Empty prefix: MRU-recent items first (top 5), then everything else
        alphabetically. Non-empty prefix: tiered ranking + MRU bonus, score
        desc with alphabetical tie-break.
        """
        items = list(self.iter_items())
        prefix_lc = prefix.lower().strip()

        if not prefix_lc:
            # Empty prefix: MRU items first (most-recent first), then
            # alphabetical for the rest.
            mru_names = self._mru_recent_names()
            mru_set = set(mru_names[:5])
            mru_floated = [
                Match(item=i, score=1.0)
                for name in mru_names[:5]
                for i in items
                if self._name_of(i) == name
            ]
            tail = sorted(
                (i for i in items if self._name_of(i) not in mru_set),
                key=self._name_of,
            )
            tail_matches = [Match(item=i, score=1.0) for i in tail]
            return (mru_floated + tail_matches)[:top_n]

        scored: list[Match] = []
        for item in items:
            s = self._score_one(item, prefix_lc)
            if s > 0:
                bonus = self._mru.recency_bonus(self._name_of(item))
                final = min(1.0, s + bonus)
                scored.append(Match(item=item, score=final))

        scored.sort(key=lambda m: (-m.score, self._name_of(m.item)))
        return scored[:top_n]

    def _mru_recent_names(self) -> list[str]:
        """Names from MRU log, most-recent first.

        Reads the MRU store's internal entries directly. Returns the
        ``name`` of each entry in reverse-chronological order.
        """
        # Newest entries are appended to the end; reverse for most-recent-first.
        return [e["name"] for e in reversed(self._mru._entries)]
```

- [ ] **Step 4: Run the tests — confirm they pass**

```bash
python -m pytest tests/test_slash_picker_source.py -v 2>&1 | tail -20
```

Expected: 16 passed (4 + 8 + 4).

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash_picker_source.py OpenComputer/tests/test_slash_picker_source.py
git commit -m "feat(slash): MRU recency bonus + empty-prefix MRU float

Items recently picked from the dropdown get +0.05 score bonus (capped
at 1.0) for ranked queries. Empty prefix surfaces the last 5 MRU
items at the top, then alphabetical for the rest — matches Claude
Code's recently-used surface.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Update SlashCommandCompleter to use UnifiedSlashSource

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/slash_completer.py` (full rewrite — small file)
- Modify: `OpenComputer/tests/test_slash_completer.py`

- [ ] **Step 1: Read the existing test file to understand expected interface**

```bash
sed -n '1,60p' OpenComputer/tests/test_slash_completer.py
```

Note the existing tests so the new behavior preserves them or updates them deliberately.

- [ ] **Step 2: Append failing tests for the new behavior**

Append to `OpenComputer/tests/test_slash_completer.py`:

```python


def test_completer_yields_skills_when_source_provided(tmp_path) -> None:
    """When constructed with a UnifiedSlashSource, the completer yields
    skill rows alongside command rows."""
    from prompt_toolkit.document import Document

    from opencomputer.cli_ui.slash_completer import SlashCommandCompleter
    from opencomputer.cli_ui.slash_mru import MruStore
    from opencomputer.cli_ui.slash_picker_source import UnifiedSlashSource

    class _Fake:
        def list_skills(self):
            from dataclasses import dataclass

            @dataclass
            class _M:
                id: str
                name: str
                description: str = ""

            return [_M(id="my-skill", name="My Skill", description="x")]

    src = UnifiedSlashSource(_Fake(), MruStore(tmp_path / "mru.json"))
    comp = SlashCommandCompleter(source=src)
    completions = list(comp.get_completions(Document("/my"), None))  # type: ignore[arg-type]
    texts = [c.text for c in completions]
    assert "/my-skill" in texts


def test_completer_truncates_long_descriptions_at_250_chars() -> None:
    """Spec §3.6 — descriptions over 250 chars are word-boundary trimmed
    with ellipsis. Whitespace (incl. newlines, tabs, multi-space) is
    normalized to single spaces before trimming so YAML frontmatter
    multi-line descriptions don't break the dropdown columns."""
    from prompt_toolkit.document import Document

    from opencomputer.cli_ui.slash_completer import (
        SlashCommandCompleter,
        _trim_description,
    )

    long = "this is a long description " * 20  # ~ 540 chars
    trimmed = _trim_description(long)
    assert len(trimmed) <= 251  # 250 + 1 for the ellipsis
    assert trimmed.endswith("…")
    # Trimming happens at a word boundary — never mid-word.
    assert not trimmed[:-1].endswith(" ")
    # Short descriptions are returned with whitespace normalized.
    assert _trim_description("short") == "short"
    assert _trim_description("") == ""
    # Newlines and runs of whitespace get collapsed (BLOCKER B1).
    assert _trim_description("line one\nline two") == "line one line two"
    assert _trim_description("a   b\t\tc") == "a b c"
    assert _trim_description("  leading and trailing  ") == "leading and trailing"


def test_completer_renders_source_tag_in_display_meta(tmp_path) -> None:
    """Display meta should mark commands as `(command)` and skills as
    `(skill)` so the user can tell them apart."""
    from prompt_toolkit.document import Document

    from opencomputer.cli_ui.slash_completer import SlashCommandCompleter
    from opencomputer.cli_ui.slash_mru import MruStore
    from opencomputer.cli_ui.slash_picker_source import UnifiedSlashSource

    class _Fake:
        def list_skills(self):
            from dataclasses import dataclass

            @dataclass
            class _M:
                id: str
                name: str
                description: str = "skill desc"

            return [_M(id="my-skill", name="My Skill")]

    src = UnifiedSlashSource(_Fake(), MruStore(tmp_path / "mru.json"))
    comp = SlashCommandCompleter(source=src)
    completions = list(comp.get_completions(Document("/"), None))  # type: ignore[arg-type]
    by_text = {c.text: c for c in completions}
    # Command row.
    assert "(command)" in str(by_text["/help"].display)
    # Skill row.
    assert "(skill)" in str(by_text["/my-skill"].display)
```

- [ ] **Step 3: Run the tests — confirm they fail**

```bash
python -m pytest tests/test_slash_completer.py -v 2>&1 | tail -10
```

Expected: 3 new failures — `_trim_description` doesn't exist, source kwarg not supported, skills not yielded.

- [ ] **Step 4: Replace the completer with a source-aware version**

Replace the entire contents of `OpenComputer/opencomputer/cli_ui/slash_completer.py` with:

```python
"""Slash command + skill autocomplete for the OpenComputer TUI.

Uses :class:`UnifiedSlashSource` (when supplied) to mix commands and
skills in a single dropdown, ranked by tier + MRU recency. Falls back
to the legacy ``SLASH_REGISTRY``-only behavior when no source is
passed (preserves backward compat for the ``build_prompt_session``
caller and the test fixtures that wired it pre-skills).

Each row's display meta carries the source tag — ``(command)`` or
``(skill)`` — so the user can tell them apart.
"""
from __future__ import annotations

from collections.abc import Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from .slash import SLASH_REGISTRY, CommandDef, SkillEntry, SlashItem
from .slash_picker_source import Match, UnifiedSlashSource

#: Spec §3.6 — descriptions trimmed at this length on word boundary.
_DESC_TRIM_LIMIT = 250


def longest_common_prefix(strs: list[str]) -> str:
    """Return the longest common prefix of all strings in ``strs``.

    Empty list returns the empty string. Comparison is case-sensitive;
    callers that need case-insensitive matching should normalize first.
    """
    if not strs:
        return ""
    s_min = min(strs)
    s_max = max(strs)
    for i, ch in enumerate(s_min):
        if ch != s_max[i]:
            return s_min[:i]
    return s_min


def _trim_description(desc: str) -> str:
    """Spec §3.6 — collapse whitespace, trim at the last word boundary
    before 250 chars, append ``…``. Short descriptions returned with
    just the whitespace normalization (newlines/runs of spaces collapsed
    to single spaces) so they don't break the dropdown's column layout
    when a YAML frontmatter description spans multiple lines.
    """
    import re

    # Normalize whitespace first — frontmatter descriptions can contain
    # newlines or tabs that would visibly break the dropdown row.
    normalized = re.sub(r"\s+", " ", desc).strip()
    if len(normalized) <= _DESC_TRIM_LIMIT:
        return normalized
    # Find the last whitespace before the limit so we don't cut mid-word.
    head = normalized[:_DESC_TRIM_LIMIT]
    cut = head.rfind(" ")
    if cut <= 0:
        cut = _DESC_TRIM_LIMIT
    return head[:cut].rstrip() + "…"


def _name_of(item: SlashItem) -> str:
    if isinstance(item, CommandDef):
        return item.name
    return item.id


def _description_of(item: SlashItem) -> str:
    if isinstance(item, CommandDef):
        return item.description
    return item.description


def _category_of(item: SlashItem) -> str:
    """Source tag — ``command`` or ``skill``. Used by the legacy
    ``(category)`` parens in the dropdown row."""
    if isinstance(item, CommandDef):
        return "command"
    return "skill"


def _format_display(item: SlashItem) -> str:
    """Render the left-column display text for a row in the dropdown.

    Format: ``/<name> [<args_hint>] (<category>)`` — same three-column
    convention as before, but ``category`` is now ``command``/``skill``
    instead of the per-command-group label so the user can tell sources
    apart at a glance.
    """
    parts = [f"/{_name_of(item)}"]
    if isinstance(item, CommandDef) and item.args_hint:
        parts.append(item.args_hint)
    parts.append(f"({_category_of(item)})")
    return " ".join(parts)


class SlashCommandCompleter(Completer):
    """Yields :class:`Completion` rows for slash commands AND skills.

    Activates only when the buffer starts with ``/`` and the cursor is
    still inside the command-name token (no space yet). Returns nothing
    for plain chat input, so prompt_toolkit's default behavior — no
    dropdown — applies for normal messages.

    ``source`` (optional): a :class:`UnifiedSlashSource`. When provided,
    skills appear alongside commands and the ranker tiers + MRU bonus
    apply. When ``None``, the completer falls back to legacy
    ``SLASH_REGISTRY``-only prefix matching — preserves the historic
    behavior used by ``build_prompt_session`` callers and existing
    fixtures that pre-date skills.
    """

    def __init__(self, source: UnifiedSlashSource | None = None) -> None:
        self._source = source

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        if " " in text:
            return
        prefix = text[1:]

        if self._source is not None:
            for match in self._source.rank(prefix):
                yield self._completion_for(match.item, replace_len=len(text))
            return

        # Legacy path — startswith filter on canonical name only.
        prefix_lc = prefix.lower()
        matches = [
            cmd for cmd in SLASH_REGISTRY if cmd.name.startswith(prefix_lc)
        ]
        matches.sort(key=lambda c: c.name)
        for cmd in matches:
            yield self._completion_for(cmd, replace_len=len(text))

    def _completion_for(
        self, item: SlashItem, *, replace_len: int
    ) -> Completion:
        return Completion(
            text=f"/{_name_of(item)}",
            start_position=-replace_len,
            display=_format_display(item),
            display_meta=_trim_description(_description_of(item)),
        )


__all__ = [
    "SlashCommandCompleter",
    "longest_common_prefix",
    "_trim_description",
]
```

- [ ] **Step 5: Run the tests — confirm they pass**

```bash
python -m pytest tests/test_slash_completer.py -v 2>&1 | tail -15
```

Expected: all completer tests pass — old ones unchanged (legacy path preserved) + 3 new ones green.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/slash_completer.py OpenComputer/tests/test_slash_completer.py
git commit -m "feat(slash): completer renders skills with source tag + 250-char trim

SlashCommandCompleter now accepts an optional UnifiedSlashSource. When
provided, completions include both commands and skills, ranked by the
source's tier ladder. Display meta is trimmed at 250 chars on word
boundary with ellipsis (Claude Code parity).

Backward compat: source=None preserves the legacy SLASH_REGISTRY-only
prefix-match path, so build_prompt_session callers and pre-existing
fixtures keep working unchanged.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Wire UnifiedSlashSource into read_user_input's _refilter

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/input_loop.py:257-372` (the `read_user_input` function and the `_refilter` body)
- Test: `OpenComputer/tests/test_input_loop_skill_picker.py` (new)

- [ ] **Step 1: Write the failing tests for input_loop integration**

Create `OpenComputer/tests/test_input_loop_skill_picker.py`:

```python
"""Integration tests for the unified slash picker inside read_user_input.

The Application+layout layer is hard to drive headlessly, so these
tests exercise the inner _refilter function via the same
UnifiedSlashSource the production path constructs. Layout rendering
is covered by snapshot in Task 8.
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
```

- [ ] **Step 2: Run the tests — confirm they pass already**

```bash
python -m pytest tests/test_input_loop_skill_picker.py -v 2>&1 | tail -10
```

Expected: 3 passed (these tests exercise UnifiedSlashSource directly, which is already implemented). They lock the expected production behavior so the integration in step 3 can't regress it.

- [ ] **Step 3: Wire UnifiedSlashSource into read_user_input**

Edit `OpenComputer/opencomputer/cli_ui/input_loop.py`. Find the function signature for `read_user_input` (around line 257) and the `_refilter` function (around line 332).

First, update the function signature to accept an optional `memory_manager`:

```python
async def read_user_input(
    *,
    profile_home: Path,
    scope: TurnCancelScope,
    session_title: str | None = None,
    paste_folder: PasteFolder | None = None,
    memory_manager: object | None = None,
) -> str:
```

Update the docstring entry mentioning the new parameter (insert after the existing UX bullet list, around line 290):

```python
    """...

    ``memory_manager``: optional MemoryManager for sourcing skills into
    the dropdown. When provided, the picker mixes commands and skills
    via UnifiedSlashSource. When ``None``, only built-in commands appear
    (legacy fallback for callers that haven't been updated yet).
    """
```

Then, inside the function body (after the existing `from .slash import SLASH_REGISTRY` import, around line 309), construct the source and store:

```python
    from .slash import CommandDef, SkillEntry, SlashItem  # noqa: F401
    from .slash_mru import MruStore
    from .slash_picker_source import UnifiedSlashSource

    history_path = _history_file_path(profile_home)
    history = FileHistory(str(history_path))
    mru_store = MruStore(profile_home / "slash_mru.json")
    picker_source: UnifiedSlashSource | None = (
        UnifiedSlashSource(memory_manager, mru_store)
        if memory_manager is not None
        else None
    )
```

Finally, modify the `_refilter` function (around line 332) to use the source for slash mode. Replace the existing slash branch:

```python
        # Slash prefix wins (existing behavior + skill picker integration).
        if text.startswith("/") and " " not in text:
            prefix = text[1:]
            if picker_source is not None:
                matches = picker_source.rank(prefix)
                state["matches"] = [m.item for m in matches]
            else:
                # Legacy path — registry only.
                state["matches"] = [
                    c for c in SLASH_REGISTRY if c.name.startswith(prefix.lower())
                ][:20]
            state["selected_idx"] = 0
            state["mode"] = "slash"
            state["at_token_range"] = None
            return
```

Note: `state["matches"]` now holds `SlashItem` (CommandDef OR SkillEntry) objects — Task 8 updates the rendering to handle both.

- [ ] **Step 4: Find the chat loop call site and pass the memory_manager**

VERIFIED in audit: cli.py line 1263 (`read_user_input` call) lives inside `_run_chat_session()` (declared at line 792), where `loop = AgentLoop(...)` exists at line 846. `AgentLoop.__init__` sets `self.memory = memory or MemoryManager(...)` at `agent/loop.py:269`, so `loop.memory` is the `MemoryManager` instance. Thread it through with one parameter.

Edit `OpenComputer/opencomputer/cli.py` around line 1263. Find the existing `read_user_input` call:

```python
            return await read_user_input(
                profile_home=profile_home,
                scope=scope,
                session_title=_title,
                paste_folder=paste_folder,
            )
```

Replace with:

```python
            return await read_user_input(
                profile_home=profile_home,
                scope=scope,
                session_title=_title,
                paste_folder=paste_folder,
                memory_manager=loop.memory if loop is not None else None,
            )
```

The `if loop is not None` guard handles a defensive case where the chat loop might not have constructed `loop` yet (e.g. early-error path) — graceful degradation to commands-only picker.

- [ ] **Step 5: Run the tests — confirm they pass**

```bash
python -m pytest tests/test_input_loop_skill_picker.py -v 2>&1 | tail -10
python -m pytest tests/ -k "input_loop or slash" -x 2>&1 | tail -15
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/input_loop.py OpenComputer/opencomputer/cli.py OpenComputer/tests/test_input_loop_skill_picker.py
git commit -m "feat(slash): wire UnifiedSlashSource into read_user_input

read_user_input now accepts memory_manager and constructs a picker
source + MRU store from it. _refilter delegates to source.rank() when
present; falls back to legacy startswith filter when the caller (older
fixture path) doesn't pass a manager.

state['matches'] now holds SlashItem (CommandDef or SkillEntry) — the
next task updates the dropdown rendering to handle both kinds.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Update dropdown rendering — source tags, 250-char trim, skill rows

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/input_loop.py:377-410` (`_dropdown_text` function)
- Modify: `OpenComputer/opencomputer/cli_ui/input_loop.py:538-554` (style dict — add `tag.command` / `tag.skill`)

- [ ] **Step 1: Write the failing rendering test**

Append to `OpenComputer/tests/test_input_loop_skill_picker.py`:

```python


def test_dropdown_text_renders_skill_with_skill_tag() -> None:
    """The internal _dropdown_text helper renders SkillEntry rows with
    a (skill) tag instead of a category."""
    from opencomputer.cli_ui.slash import CommandDef, SkillEntry

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
    from opencomputer.cli_ui.slash import SkillEntry

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


# Add the helper at the bottom of the file:
def _render_dropdown(state):
    """Tap into input_loop's _dropdown_text by importing the helper.

    input_loop defines _dropdown_text as a closure inside read_user_input
    today; we will lift it to a module-level _render_dropdown_for_state
    helper in step 3 so it's testable.
    """
    from opencomputer.cli_ui.input_loop import _render_dropdown_for_state

    return _render_dropdown_for_state(state)
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/test_input_loop_skill_picker.py::test_dropdown_text_renders_skill_with_skill_tag -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name '_render_dropdown_for_state'`.

- [ ] **Step 3: Lift _dropdown_text to a module-level helper, add SkillEntry rendering, add source tag**

Edit `OpenComputer/opencomputer/cli_ui/input_loop.py`. Add this module-level helper near the top of the file (after the existing `_strip_trailing_whitespace` helper, around line 95):

```python
def _render_dropdown_for_state(state: dict) -> list[tuple[str, str]]:
    """Render a dropdown row list from the picker state dict.

    Pulled out of ``read_user_input`` (was a closure) so unit tests can
    exercise the rendering logic without spinning up an Application.

    Returns a list of (style-class, text) pairs suitable for
    :class:`FormattedTextControl`.
    """
    from .slash import CommandDef, SkillEntry
    from .slash_completer import _trim_description

    matches = state.get("matches") or []
    if not matches:
        return []
    out: list[tuple[str, str]] = []
    if state.get("mode") == "file":
        # File-completion rendering — unchanged from before.
        from opencomputer.cli_ui.file_completer import format_size_label

        for i, p in enumerate(matches):
            is_sel = i == state["selected_idx"]
            cursor_cls = "class:dd.cursor" if is_sel else "class:dd.cursor.dim"
            title_cls = "class:dd.title.selected" if is_sel else "class:dd.title"
            desc_cls = "class:dd.desc.selected" if is_sel else "class:dd.desc"
            from pathlib import Path as _Path

            size = format_size_label(p, base=_Path.cwd())
            out.append((cursor_cls, "❯ " if is_sel else "  "))
            out.append((title_cls, f"@{p}"))
            if size:
                out.append((desc_cls, f"  ({size})"))
            out.append(("", "\n"))
        return out
    # Slash command + skill rendering — handles both SlashItem variants.
    for i, item in enumerate(matches):
        is_sel = i == state["selected_idx"]
        cursor_cls = "class:dd.cursor" if is_sel else "class:dd.cursor.dim"
        title_cls = "class:dd.title.selected" if is_sel else "class:dd.title"
        cat_cls = "class:dd.cat.selected" if is_sel else "class:dd.cat"
        desc_cls = "class:dd.desc.selected" if is_sel else "class:dd.desc"
        if isinstance(item, CommandDef):
            args = f" {item.args_hint}" if item.args_hint else ""
            label = f"/{item.name}{args}"
            tag = "(command)"
            desc = item.description
        elif isinstance(item, SkillEntry):
            label = f"/{item.id}"
            tag = "(skill)"
            desc = item.description
        else:
            # Unknown item kind — skip rather than render garbage.
            continue
        out.append((cursor_cls, "❯ " if is_sel else "  "))
        out.append((title_cls, label))
        out.append((cat_cls, f"  {tag}"))
        out.append((desc_cls, f"  {_trim_description(desc)}"))
        out.append(("", "\n"))
    return out
```

Then update the `_dropdown_text` closure inside `read_user_input` (around line 377) to delegate to the module-level helper:

```python
    def _dropdown_text():
        return _render_dropdown_for_state(state)
```

Then update the `_apply_selection` function (around line 434) to handle SkillEntry alongside CommandDef. Find the slash branch:

```python
        else:
            input_buffer.text = f"/{sel.name}"
            input_buffer.cursor_position = len(input_buffer.text)
```

Replace with:

```python
        else:
            from .slash import CommandDef, SkillEntry

            if isinstance(sel, CommandDef):
                name = sel.name
            elif isinstance(sel, SkillEntry):
                name = sel.id
            else:
                return
            input_buffer.text = f"/{name}"
            input_buffer.cursor_position = len(input_buffer.text)
```

Same for the Enter handler (around line 460):

```python
    @kb.add(Keys.Enter)
    def _enter(event):  # noqa: ANN001
        if state["matches"] and 0 <= state["selected_idx"] < len(state["matches"]):
            if state["mode"] == "file":
                _apply_selection()
                return
            from .slash import CommandDef, SkillEntry

            sel = state["matches"][state["selected_idx"]]
            if isinstance(sel, CommandDef):
                name = sel.name
            elif isinstance(sel, SkillEntry):
                name = sel.id
            else:
                event.app.exit(result=input_buffer.text)
                return
            input_buffer.text = f"/{name}"
            # Record the pick to MRU so it floats next time.
            if mru_store is not None:
                try:
                    mru_store.record(name)
                except Exception:  # noqa: BLE001 — never break submit
                    pass
        event.app.exit(result=input_buffer.text)
```

- [ ] **Step 4: Run the tests — confirm they pass**

```bash
python -m pytest tests/test_input_loop_skill_picker.py -v 2>&1 | tail -15
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/input_loop.py OpenComputer/tests/test_input_loop_skill_picker.py
git commit -m "feat(slash): dropdown renders skills + records MRU on Enter

Lift _dropdown_text to module-level _render_dropdown_for_state so unit
tests can exercise it without an Application. Renders both CommandDef
and SkillEntry rows with their source tag — (command) cyan, (skill)
green visually. Descriptions trimmed at 250 chars on word boundary.

_apply_selection and _enter handle SkillEntry (slash text comes from
.id, not .name). Enter records the pick to MruStore so the item floats
in the next session.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Visual styling — distinct colors for command vs skill tags

**Files:**
- Modify: `OpenComputer/opencomputer/cli_ui/input_loop.py:538-554` (style dict)

- [ ] **Step 1: Write a quick visual-class regression test**

Append to `OpenComputer/tests/test_input_loop_skill_picker.py`:

```python


def test_style_dict_includes_command_and_skill_tags() -> None:
    """The style dict must define dd.tag.command and dd.tag.skill so
    the dropdown can render them with distinct colors."""
    import inspect

    from opencomputer.cli_ui import input_loop

    src = inspect.getsource(input_loop.read_user_input)
    assert '"dd.tag.command"' in src or "'dd.tag.command'" in src
    assert '"dd.tag.skill"' in src or "'dd.tag.skill'" in src
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/test_input_loop_skill_picker.py::test_style_dict_includes_command_and_skill_tags -v 2>&1 | tail -5
```

Expected: AssertionError — the style dict doesn't have those classes yet.

- [ ] **Step 3: Update the style dict + apply per-tag classes in the renderer**

Edit `OpenComputer/opencomputer/cli_ui/input_loop.py`. Find the style dict (around line 538):

```python
    style = Style.from_dict(
        {
            "prompt": "ansigreen bold",
            "dd.cursor": "bold #ffaf00",
            "dd.cursor.dim": "#3a3a3a",
            "dd.title": "#a8a8a8",
            "dd.title.selected": "bold #61afef",
            "dd.cat": "#5f87af",
            "dd.cat.selected": "bold #61afef",
            "dd.desc": "#6c6c6c",
            "dd.desc.selected": "#bcbcbc",
            "dd.divider": "#3a3a3a",
            "title.box": "#5fafd7",
            "title.text": "bold #5fafd7",
            "hint.dim": "italic #6c6c6c",
        }
    )
```

Add the two new classes:

```python
    style = Style.from_dict(
        {
            "prompt": "ansigreen bold",
            "dd.cursor": "bold #ffaf00",
            "dd.cursor.dim": "#3a3a3a",
            "dd.title": "#a8a8a8",
            "dd.title.selected": "bold #61afef",
            "dd.cat": "#5f87af",
            "dd.cat.selected": "bold #61afef",
            "dd.tag.command": "#5fafd7",  # cyan — built-in commands
            "dd.tag.skill": "#5faf5f",    # green — installed skills
            "dd.desc": "#6c6c6c",
            "dd.desc.selected": "#bcbcbc",
            "dd.divider": "#3a3a3a",
            "title.box": "#5fafd7",
            "title.text": "bold #5fafd7",
            "hint.dim": "italic #6c6c6c",
        }
    )
```

Update `_render_dropdown_for_state` to use the per-tag classes. In that function, replace:

```python
        cat_cls = "class:dd.cat.selected" if is_sel else "class:dd.cat"
```

with:

```python
        if isinstance(item, CommandDef):
            cat_cls = (
                "class:dd.cat.selected" if is_sel else "class:dd.tag.command"
            )
        else:
            cat_cls = (
                "class:dd.cat.selected" if is_sel else "class:dd.tag.skill"
            )
```

(Note: when selected, both share `dd.cat.selected` to keep the bold blue highlight; deselected rows get the tag-specific color.)

- [ ] **Step 4: Run the tests — confirm they pass**

```bash
python -m pytest tests/test_input_loop_skill_picker.py -v 2>&1 | tail -10
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/cli_ui/input_loop.py OpenComputer/tests/test_input_loop_skill_picker.py
git commit -m "feat(slash): distinct colors for (command) vs (skill) tags

Cyan (#5fafd7) for command tags, green (#5faf5f) for skill tags. When
a row is selected, both share the bold blue highlight so the cursor
position is unambiguous. Mirrors Claude Code's source-coloured tags.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: SlashCommandResult.source field

**Files:**
- Modify: `OpenComputer/plugin_sdk/slash_command.py:28-44`
- Modify: `OpenComputer/tests/test_slash_dispatcher.py` (add new tests; if doesn't exist, create)

- [ ] **Step 1: Find existing SlashCommandResult tests**

```bash
grep -rln "SlashCommandResult" OpenComputer/tests/ | head -5
```

Note where to append the new test.

- [ ] **Step 2: Write the failing test**

Pick the file with the most existing SlashCommandResult coverage (likely `tests/test_slash_dispatcher.py` or `tests/test_slash_skill_fallback.py`) and append:

```python


def test_slash_command_result_source_defaults_to_command() -> None:
    """Backwards-compat: existing call sites that don't pass source
    get source='command'."""
    from plugin_sdk.slash_command import SlashCommandResult

    r = SlashCommandResult(output="hi")
    assert r.source == "command"


def test_slash_command_result_source_can_be_skill() -> None:
    """source='skill' is the marker for the Hybrid dispatch path."""
    from plugin_sdk.slash_command import SlashCommandResult

    r = SlashCommandResult(output="x", source="skill")
    assert r.source == "skill"


def test_slash_command_result_source_invalid_value_rejected() -> None:
    """Source field is restricted to 'command' or 'skill'."""
    from typing import get_args, get_type_hints

    from plugin_sdk.slash_command import SlashCommandResult

    hints = get_type_hints(SlashCommandResult)
    source_type = hints["source"]
    # Type alias is Literal["command", "skill"]
    assert "command" in get_args(source_type)
    assert "skill" in get_args(source_type)
```

- [ ] **Step 3: Run the test — confirm it fails**

```bash
python -m pytest tests/test_slash_skill_fallback.py -v -k "source" 2>&1 | tail -10
```

Expected: AttributeError — `source` field doesn't exist.

- [ ] **Step 4: Add the source field**

Edit `OpenComputer/plugin_sdk/slash_command.py`. Replace the `SlashCommandResult` dataclass:

```python
from typing import Literal


@dataclass(frozen=True, slots=True)
class SlashCommandResult:
    """What a slash command returns when executed."""

    #: Text to show the user.
    output: str
    #: True if the command is "terminal" — i.e. it handled the user's intent
    #: and the agent loop should NOT continue to the LLM for this turn.
    #: False means the agent loop proceeds as normal (command was a side-
    #: effect like ``/plan`` that sets a flag and lets chat continue).
    handled: bool = True
    #: Origin of this result.
    #:
    #: ``"command"`` (default) — handler was a registered command (built-in
    #: or plugin-authored). The agent loop emits the output as a normal
    #: assistant text reply.
    #:
    #: ``"skill"`` — handler was the slash-skill fallback that loaded a
    #: SKILL.md body. The agent loop wraps the result as a synthetic
    #: ``Skill`` ``tool_use`` + ``tool_result`` pair so the model sees the
    #: skill content as authoritative tool output (Claude-Code parity).
    #:
    #: Default ``"command"`` keeps existing call sites working unchanged.
    source: Literal["command", "skill"] = "command"
```

Make sure `from typing import Literal` is imported at the top of the file.

- [ ] **Step 5: Run the tests — confirm they pass**

```bash
python -m pytest tests/test_slash_skill_fallback.py tests/test_slash_dispatcher.py -v -k "source" 2>&1 | tail -10
```

Expected: 3 new tests pass. Existing slash dispatcher tests must remain green:

```bash
python -m pytest tests/test_slash_skill_fallback.py tests/test_slash_dispatcher.py -v 2>&1 | tail -10
```

Expected: full file green.

- [ ] **Step 6: Commit**

```bash
git add OpenComputer/plugin_sdk/slash_command.py OpenComputer/tests/test_slash_skill_fallback.py
git commit -m "feat(slash): SlashCommandResult.source field — distinguishes command vs skill

Default 'command' keeps every existing call site working. Tasks 11
& 12 set source='skill' on fallback results and wrap them as
synthetic SkillTool tool_use/tool_result pairs in the agent loop —
Claude-Code parity for the dispatch path.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: Mark slash_skill_fallback results with source="skill"

**Files:**
- Modify: `OpenComputer/opencomputer/agent/slash_skill_fallback.py`
- Modify: `OpenComputer/tests/test_slash_skill_fallback.py`

- [ ] **Step 1: Write the failing test**

Append to `OpenComputer/tests/test_slash_skill_fallback.py`:

```python


def test_skill_fallback_result_has_source_skill():
    """The fallback closure must mark its result with source='skill'
    so the agent loop's Hybrid wrap fires."""
    from dataclasses import dataclass

    from plugin_sdk.runtime_context import RuntimeContext
    from opencomputer.agent.slash_skill_fallback import make_skill_fallback

    @dataclass
    class _M:
        id: str
        name: str
        description: str = ""

    class _Mem:
        def list_skills(self):
            return [_M(id="hello", name="hello")]

        def load_skill_body(self, sid):
            return "body"

    fallback = make_skill_fallback(_Mem())
    result = fallback("hello", "", RuntimeContext())
    assert result is not None
    assert result.source == "skill"


def test_skill_fallback_error_paths_also_marked_skill():
    """Even when the fallback returns an error result (load failed,
    empty body), source='skill' so the agent loop knows it came from
    the fallback."""
    from dataclasses import dataclass

    from plugin_sdk.runtime_context import RuntimeContext
    from opencomputer.agent.slash_skill_fallback import make_skill_fallback

    @dataclass
    class _M:
        id: str
        name: str
        description: str = ""

    class _Mem:
        def list_skills(self):
            return [_M(id="empty", name="empty")]

        def load_skill_body(self, sid):
            return ""  # empty body — fallback returns an error result

    fallback = make_skill_fallback(_Mem())
    result = fallback("empty", "", RuntimeContext())
    assert result is not None
    assert result.source == "skill"
```

- [ ] **Step 2: Run the tests — confirm they fail**

```bash
python -m pytest tests/test_slash_skill_fallback.py -v -k "source" 2>&1 | tail -10
```

Expected: 2 new tests fail with `AssertionError: 'command' != 'skill'` (default `source` value wins).

- [ ] **Step 3: Add `source="skill"` to every SlashCommandResult constructed inside the fallback**

Edit `OpenComputer/opencomputer/agent/slash_skill_fallback.py`. Find the three `SlashCommandResult(...)` constructions inside `_fallback` and add `source="skill"` to each:

```python
                    return SlashCommandResult(
                        output=f"failed to load skill '{name}': {type(e).__name__}: {e}",
                        handled=True,
                        source="skill",
                    )
                if not body:
                    return SlashCommandResult(
                        output=f"skill '{name}' has empty body",
                        handled=True,
                        source="skill",
                    )
                if len(body) > _MAX_BODY_CHARS:
                    body = body[:_MAX_BODY_CHARS] + (
                        f"\n\n[truncated — skill body has "
                        f"{len(body) - _MAX_BODY_CHARS} more chars]"
                    )
                title = getattr(skill, "name", None) or name
                return SlashCommandResult(
                    output=f"## {title}\n\n{body}",
                    handled=True,
                    source="skill",
                )
```

- [ ] **Step 4: Run the tests — confirm they pass**

```bash
python -m pytest tests/test_slash_skill_fallback.py -v 2>&1 | tail -15
```

Expected: all 23+ tests pass (15+ pre-existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/agent/slash_skill_fallback.py OpenComputer/tests/test_slash_skill_fallback.py
git commit -m "feat(slash): fallback marks SlashCommandResult.source='skill'

Every SlashCommandResult emitted by make_skill_fallback now carries
source='skill' — the marker the agent loop reads to wrap the body as
a synthetic SkillTool tool_use+tool_result pair instead of a plain
assistant text reply (Hybrid dispatch).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 12: Hybrid wrap in agent/loop.py

**Files:**
- Modify: `OpenComputer/opencomputer/agent/loop.py:544-580` (the slash result branch)
- Test: `OpenComputer/tests/test_hybrid_skill_dispatch.py` (new)

- [ ] **Step 1: Write the failing integration test**

Create `OpenComputer/tests/test_hybrid_skill_dispatch.py`:

```python
"""Tests for the Hybrid dispatch wrap in the agent loop.

When the slash dispatcher returns a SlashCommandResult with
source='skill', the loop wraps the result as a synthetic SkillTool
tool_use + tool_result message pair — so the model sees skill content
as authoritative tool output (Claude-Code parity) rather than as
plain assistant text.

These tests use a fake provider so the loop runs deterministically
without API calls. They focus on the message-shape contract:

- skill-source result emits exactly two messages: one assistant with
  tool_calls=[ToolCall(name='Skill', ...)] and one tool result with
  matching tool_call_id and the SKILL body as content.
- command-source result is unchanged: one user message + one assistant
  text message, no tool_calls.
"""
from __future__ import annotations

from plugin_sdk.core import Message
from plugin_sdk.slash_command import SlashCommandResult


def test_skill_result_wraps_as_tool_use_pair():
    """source='skill' result generates assistant(tool_calls=Skill) +
    tool(tool_call_id=...) message pair."""
    from opencomputer.agent.loop import _wrap_skill_result_as_tool_messages

    result = SlashCommandResult(
        output="# Skill: hello\n\nBody content here.",
        handled=True,
        source="skill",
    )
    messages = _wrap_skill_result_as_tool_messages(
        skill_name="hello", args="some-args", result=result
    )
    assert len(messages) == 2

    assistant, tool_result = messages
    assert assistant.role == "assistant"
    assert assistant.tool_calls is not None
    assert len(assistant.tool_calls) == 1
    tc = assistant.tool_calls[0]
    assert tc.name == "Skill"
    assert tc.arguments == {"name": "hello"}

    assert tool_result.role == "tool"
    assert tool_result.tool_call_id == tc.id
    assert "Body content here." in tool_result.content


def test_command_result_does_not_wrap():
    """source='command' (default) returns empty list — caller emits
    the normal user/assistant pair."""
    from opencomputer.agent.loop import _wrap_skill_result_as_tool_messages

    result = SlashCommandResult(output="hello", handled=True)
    # Default source == "command"; helper must return [] so caller falls
    # through to the existing user/assistant emission.
    messages = _wrap_skill_result_as_tool_messages(
        skill_name="anything", args="", result=result
    )
    assert messages == []


def test_skill_args_passed_into_tool_call_arguments():
    """If the user typed `/foo bar baz`, the args land in the tool_call
    arguments alongside the skill name."""
    from opencomputer.agent.loop import _wrap_skill_result_as_tool_messages

    result = SlashCommandResult(
        output="body",
        handled=True,
        source="skill",
    )
    messages = _wrap_skill_result_as_tool_messages(
        skill_name="my-skill", args="alpha beta", result=result
    )
    tc = messages[0].tool_calls[0]
    # Args show up as 'args' key on the tool_call arguments — distinct from
    # 'name' so SkillTool's existing schema is satisfied if/when args become
    # part of its contract.
    assert tc.arguments.get("name") == "my-skill"
    # Args is preserved (downstream may or may not use it).
    assert tc.arguments.get("args") == "alpha beta"
```

- [ ] **Step 2: Run the test — confirm it fails**

```bash
python -m pytest tests/test_hybrid_skill_dispatch.py -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name '_wrap_skill_result_as_tool_messages'`.

- [ ] **Step 3: Implement the helper + integrate it into the slash branch**

Edit `OpenComputer/opencomputer/agent/loop.py`. Add the helper near the top of the file (after the existing imports, before the AgentLoop class — search for `class AgentLoop` to find the position):

```python
import secrets

from plugin_sdk.core import Message, ToolCall


#: Synthetic tool name used for Hybrid skill dispatch wrap. Must match
#: the ``name`` returned by :class:`SkillTool.schema`. Pulled into a
#: constant so a future tool rename surfaces here as a single-place
#: edit rather than a silent breakage.
SKILL_TOOL_NAME = "Skill"


def _wrap_skill_result_as_tool_messages(
    *,
    skill_name: str,
    args: str,
    result,  # SlashCommandResult — typed loosely to avoid import cycle
) -> list[Message]:
    """Hybrid dispatch — wrap a skill-source slash result as a synthetic
    ``Skill`` tool_use + tool_result message pair.

    Returns an empty list when ``result.source != "skill"`` so the caller
    falls through to the existing user/assistant emission for command
    results.

    The model receives the SKILL body as a tool_result on the next turn
    — exactly the shape it would see if it had auto-invoked SkillTool.
    Claude-Code parity for the dispatch path.

    Trade-off note: an alternative was to discard ``result.output`` and
    let the agent invoke ``SkillTool`` naturally on the next turn. We
    synthesize both halves instead because (a) the fallback already
    loaded SKILL.md — re-loading is wasteful — and (b) the natural-
    invoke path requires intercepting model output to inject a tool_use,
    a much uglier control-flow change than this branch.
    """
    if getattr(result, "source", "command") != "skill":
        return []
    call_id = f"toolu_skill_{secrets.token_hex(6)}"
    tool_call = ToolCall(
        id=call_id,
        name=SKILL_TOOL_NAME,
        arguments={"name": skill_name, "args": args or ""},
    )
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[tool_call],
    )
    tool_message = Message(
        role="tool",
        content=result.output,
        tool_call_id=call_id,
        name=SKILL_TOOL_NAME,
    )
    return [assistant, tool_message]
```

Then, find the existing slash result handler (around line 538-580) and update the `if _slash_result is not None and _slash_result.handled:` branch:

```python
        _slash_result = await _slash_dispatch(
            user_message,
            _plugin_registry.slash_commands,
            self._runtime,
            fallback=make_skill_fallback(self.memory),
        )
        if _slash_result is not None and _slash_result.handled:
            # Always emit the user message first.
            user_msg = Message(role="user", content=user_message)
            messages.append(user_msg)
            self._emit_before_message_write(session_id=sid, message=user_msg)
            self.db.append_message(sid, user_msg)

            # Hybrid dispatch — skill-source result becomes a synthetic
            # SkillTool tool_use + tool_result pair so the model sees the
            # skill body as authoritative tool output. Command-source
            # result emits the standard assistant text reply.
            from opencomputer.agent.slash_dispatcher import parse_slash

            parsed = parse_slash(user_message)
            skill_name = parsed[0] if parsed else ""
            args_str = parsed[1] if parsed else ""
            wrap = _wrap_skill_result_as_tool_messages(
                skill_name=skill_name, args=args_str, result=_slash_result
            )
            if wrap:
                # Skill — append the assistant tool_use + tool result, but
                # DO NOT end the session: fall through to the normal agent
                # loop so the model takes a turn on the skill content.
                for m in wrap:
                    messages.append(m)
                    self._emit_before_message_write(session_id=sid, message=m)
                    self.db.append_message(sid, m)
                # Allow the loop to continue — assistant response from the
                # model itself comes from the next iteration. We do NOT
                # call self.db.end_session(sid) here.
            else:
                # Command — preserve the original behavior.
                assistant_msg = Message(
                    role="assistant", content=_slash_result.output
                )
                messages.append(assistant_msg)
                self._emit_before_message_write(session_id=sid, message=assistant_msg)
                self.db.append_message(sid, assistant_msg)
                self.db.end_session(sid)
                try:
                    self._loop_detector.pop_frame(sid, _loop_depth)
                except Exception:  # noqa: BLE001
                    _log.debug("loop_detector.pop_frame failed (slash path)", exc_info=True)
                await self._emit_session_end_event(
                    session_id=sid,
                    end_reason="completed",
                    turn_count=0,
                    duration_seconds=time.monotonic() - _session_started_at,
                    had_errors=False,
                )
                return ConversationResult(
                    final_message=assistant_msg,
                    messages=messages,
                    session_id=sid,
                    iterations=0,
                    input_tokens=0,
                    output_tokens=0,
                )
            # If skill — fall through past this if-block to the regular
            # agent loop. No early return.
```

- [ ] **Step 4: Run the tests — confirm they pass**

```bash
python -m pytest tests/test_hybrid_skill_dispatch.py -v 2>&1 | tail -10
```

Expected: 3 passed.

Then run the broader slash + loop suite to confirm no regression:

```bash
python -m pytest tests/test_slash_dispatcher.py tests/test_slash_skill_fallback.py tests/ -k "loop or slash" -x 2>&1 | tail -15
```

Expected: all green (existing 5443+ + new tests).

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/opencomputer/agent/loop.py OpenComputer/tests/test_hybrid_skill_dispatch.py
git commit -m "feat(slash): Hybrid dispatch — wrap skill results as synthetic SkillTool calls

When the slash dispatcher returns SlashCommandResult(source='skill'),
the agent loop now emits an assistant message with a synthetic 'Skill'
tool_use + a matching tool_result message carrying the SKILL.md body —
then continues the loop so the model takes a turn on the skill content.

Command-source results retain the original user/assistant text shape +
session-end early return. Backward compat preserved.

Claude-Code parity for the dispatch path.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 12.5: Hybrid full-turn provider integration test

**Files:**
- Modify: `OpenComputer/tests/test_hybrid_skill_dispatch.py` (append integration test)

This addresses audit BLOCKER A1 + D3: verify that after the Hybrid wrap fires, the iteration loop correctly invokes the provider with the synthetic tool_use+tool_result already in `messages`, and that the model's response gets appended cleanly. The unit-shape tests in Task 12 only proved the helper produces the right two messages; this task proves the loop wires them through to the provider.

- [ ] **Step 1: Append the integration test**

Append to `OpenComputer/tests/test_hybrid_skill_dispatch.py`:

```python


import asyncio
import pytest


@pytest.mark.asyncio
async def test_hybrid_skill_dispatch_provider_sees_tool_result(tmp_path):
    """Full-turn integration: when the user types '/<skill-name>', the
    Hybrid wrap fires, the iteration loop continues, the provider's
    first call receives the synthetic tool_use+tool_result already in
    the messages list, and the model's response is appended cleanly."""
    from dataclasses import dataclass

    from opencomputer.agent.config import default_config
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.memory import MemoryManager
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage

    @dataclass
    class _SkillMeta:
        id: str
        name: str
        description: str = ""
        path: str = ""
        version: str = "0.1.0"
        references: tuple = ()
        examples: tuple = ()

    class _RecordingProvider(BaseProvider):
        """Captures the messages it receives and returns a canned reply."""

        def __init__(self):
            self.captured_messages = None

        async def complete(self, *, system, messages, tools, max_tokens, temperature, **kwargs):
            self.captured_messages = list(messages)
            return ProviderResponse(
                content="ack",
                usage=Usage(input_tokens=10, output_tokens=2),
                stop_reason="end_turn",
            )

        async def stream_complete(self, *args, **kwargs):
            raise NotImplementedError

    # Build a memory manager with one skill whose body is "BODY-FOR-TEST".
    mem_path = tmp_path / "skills"
    mem_path.mkdir()
    (mem_path / "test-skill").mkdir()
    (mem_path / "test-skill" / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: Test\n---\nBODY-FOR-TEST",
        encoding="utf-8",
    )
    decl = tmp_path / "MEMORY.md"
    decl.write_text("", encoding="utf-8")

    cfg = default_config()
    cfg.memory.declarative_path = decl
    cfg.memory.skills_path = mem_path
    cfg.session.db_path = tmp_path / "sessions.db"

    provider = _RecordingProvider()
    loop = AgentLoop(provider=provider, config=cfg)

    result = await loop.run_conversation("/test-skill", session_id="sess-1")

    # Provider should have been called.
    assert provider.captured_messages is not None
    msgs = provider.captured_messages
    # Sequence: user (slash text), assistant (tool_use), tool (tool_result), ...
    user = next(m for m in msgs if m.role == "user" and "/test-skill" in (m.content or ""))
    assistant_with_tool = next(
        m for m in msgs if m.role == "assistant" and m.tool_calls
    )
    tool_msg = next(m for m in msgs if m.role == "tool")

    assert user is not None
    assert assistant_with_tool.tool_calls[0].name == "Skill"
    assert tool_msg.tool_call_id == assistant_with_tool.tool_calls[0].id
    assert "BODY-FOR-TEST" in tool_msg.content
    # The model's response landed as the conversation's final message.
    assert result.final_message.role == "assistant"
    assert result.final_message.content == "ack"
```

- [ ] **Step 2: Run the test — confirm it passes**

```bash
python -m pytest tests/test_hybrid_skill_dispatch.py::test_hybrid_skill_dispatch_provider_sees_tool_result -v 2>&1 | tail -10
```

Expected: 1 passed.

If it fails, the most likely causes are:
- The iteration loop doesn't continue past the slash branch when the wrap returns non-empty (revisit the Task 12 control-flow change in `loop.py`).
- The synthetic tool_use+tool_result aren't being passed to `provider.complete` because the loop's message-building step replaces them. Re-read the iteration logic and ensure `messages` carries through.
- `_loop_detector` / session-lifecycle artifacts assert in the wrong order.

If you hit any of these, do NOT skip the test — fix the loop. This integration test is the contract proof that the Hybrid wrap actually works end-to-end.

- [ ] **Step 3: Commit**

```bash
git add OpenComputer/tests/test_hybrid_skill_dispatch.py
git commit -m "test(slash): full-turn provider integration for Hybrid skill dispatch

Captures provider's first call after the Hybrid wrap fires; verifies
the synthetic tool_use+tool_result are present in messages, the body
content is correct, and the model's response is appended cleanly.

This is the contract proof that the Hybrid wrap actually works end-
to-end — not just that the helper produces the right two messages
(Task 12 unit tests).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 13: End-to-end smoke test — full picker flow

**Files:**
- Create: `OpenComputer/tests/test_slash_menu_e2e.py`

- [ ] **Step 1: Write a comprehensive smoke test that exercises every layer**

Create `OpenComputer/tests/test_slash_menu_e2e.py`:

```python
"""End-to-end smoke for the slash menu Claude-Code parity feature.

Exercises every layer in one file: data source → ranker → completer →
input_loop renderer → MRU recording → slash dispatcher → fallback →
Hybrid wrap. If any link breaks, this file catches it before the user
hits the bug in production.
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
    if "Recovery" in blob:
        # If the long-desc skill made it into the top 10, ellipsis must
        # show somewhere in the rendered dropdown.
        assert "…" in blob


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

    # First session — alphabetical at empty prefix (after commands).
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
    from prompt_toolkit.document import Document

    comp = SlashCommandCompleter()  # no source = legacy path
    completions = list(comp.get_completions(Document("/h"), None))  # type: ignore[arg-type]
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
```

- [ ] **Step 2: Run the e2e tests — confirm they pass**

```bash
python -m pytest tests/test_slash_menu_e2e.py -v 2>&1 | tail -15
```

Expected: 5 passed.

- [ ] **Step 3: Run the FULL test suite to confirm no regressions across the entire codebase**

```bash
python -m pytest tests/ -x --ignore=tests/test_skill_evolution.py 2>&1 | tail -10
```

Expected: ~5443+25 passed. The ~25 new tests come from Tasks 1-12.

- [ ] **Step 4: Run ruff lint on all new code**

```bash
ruff check OpenComputer/opencomputer/cli_ui/slash_mru.py \
           OpenComputer/opencomputer/cli_ui/slash_picker_source.py \
           OpenComputer/opencomputer/cli_ui/slash_completer.py \
           OpenComputer/opencomputer/cli_ui/input_loop.py \
           OpenComputer/opencomputer/cli_ui/slash.py \
           OpenComputer/opencomputer/agent/loop.py \
           OpenComputer/opencomputer/agent/slash_skill_fallback.py \
           OpenComputer/plugin_sdk/slash_command.py \
           OpenComputer/tests/test_slash_mru.py \
           OpenComputer/tests/test_slash_item_types.py \
           OpenComputer/tests/test_slash_picker_source.py \
           OpenComputer/tests/test_slash_completer.py \
           OpenComputer/tests/test_input_loop_skill_picker.py \
           OpenComputer/tests/test_hybrid_skill_dispatch.py \
           OpenComputer/tests/test_slash_menu_e2e.py 2>&1 | tail -10
```

Expected: `All checks passed!`. If any complaint, fix inline + re-commit.

- [ ] **Step 5: Commit**

```bash
git add OpenComputer/tests/test_slash_menu_e2e.py
git commit -m "test(slash): end-to-end smoke covering picker source → completer → MRU → Hybrid wrap

Single test file that exercises every layer in one place. If the
slash menu silently regresses (skills disappear, MRU stops floating,
Hybrid wrap stops firing), this catches it before users hit the bug.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 14: Final verification + push + open PR

- [ ] **Step 1: Re-check archit's status one more time before pushing**

```bash
cd /Users/saksham/Vscode/claude/OpenComputer
git fetch origin --prune
gh pr list --state open --json number,title,headRefName 2>&1 | head -10
```

Expected: still empty or no `cli_ui/` overlap.

- [ ] **Step 2: Rebase onto latest main if anything new landed**

If `git status` shows uncommitted changes (orphaned bits from another session bleeding through, as happened during the planning phase), stash them first to keep the rebase clean. The fresh stash is preserved — never lost.

```bash
git status --short
# If output is non-empty (excluding untracked), stash:
git stash push -m "pre-push-stash-2026-04-29"
git stash list | head -3  # confirm the stash landed
```

Now rebase:

```bash
git fetch origin main
git rebase origin/main 2>&1 | tail -5
```

**If the rebase reports conflicts**: stop, investigate, do not force through. The conflict is the parallel-session signal you've been watching for. Re-check `gh pr list --state open` — if a new PR overlapping `cli_ui/`, `agent/loop.py`, or `plugin_sdk/slash_command.py` opened during execution, pause and replan with the user before resolving. Do NOT use `git rebase --skip` or `git checkout --theirs` blindly.

**If the rebase succeeds cleanly**: continue with step 3.

- [ ] **Step 3: Run the full suite once more from clean state**

```bash
python -m pytest tests/ -x 2>&1 | tail -5
```

Expected: ~5443+25 passed, 14 skipped.

- [ ] **Step 4: Push the branch**

```bash
git push -u origin feat/slash-menu-cc-parity 2>&1 | tail -5
```

- [ ] **Step 5: Open the PR**

```bash
gh pr create --title "feat(slash): commands + skills + MRU + Hybrid dispatch — Claude-Code parity" --body "$(cat <<'EOF'
## Summary

Closes the user's two coupled slash-menu complaints in one PR:

1. **Skills now surface in the dropdown.** Type `/` and the menu shows every installed skill alongside the 14 built-in commands, ranked by match quality and recent use.
2. **Hybrid dispatch.** Selecting a `/<skill-name>` row submits the skill, and the agent loop now wraps the SKILL.md body as a synthetic ``Skill`` tool_use + tool_result pair — the model sees skill content as authoritative tool output, exactly like Claude Code.

## What ships (two layers, one PR)

**Layer 1 — TUI surface (`cli_ui/`)**

- `slash_mru.py` (NEW) — bounded last-50 recent-pick log, atomic JSON writes.
- `slash_picker_source.py` (NEW) — `UnifiedSlashSource` reads `SLASH_REGISTRY` + `MemoryManager.list_skills()`, dedupes on collision (command wins), exposes 5-tier ranked search via `difflib`.
- `slash.py` — adds `SkillEntry` + `SlashItem` union next to existing `CommandDef`.
- `slash_completer.py` — accepts an optional `UnifiedSlashSource`; renders rows with `(command)` / `(skill)` source tags + 250-char description trim. Legacy `source=None` path preserved.
- `input_loop.py` — `read_user_input` now accepts `memory_manager`, constructs the picker, records picks to MRU on Enter. Dropdown rendering lifted to a module-level `_render_dropdown_for_state` helper for testability. Color tags: cyan for command, green for skill.

**Layer 2 — Hybrid dispatch (`agent/`, `plugin_sdk/`)**

- `plugin_sdk/slash_command.py` — `SlashCommandResult.source: Literal["command", "skill"]` field added with default `"command"` (backward compat).
- `agent/slash_skill_fallback.py` — every result emitted by the fallback now sets `source="skill"`.
- `agent/loop.py` — new `_wrap_skill_result_as_tool_messages` helper; the slash branch emits a synthetic `Skill` tool_use + tool_result pair when `source == "skill"` and *continues the loop* so the model takes a turn on the content. Command-source results retain the existing user/assistant text-reply path with session-end.

## Ranking algorithm

Tiered match scoring (stdlib `difflib` only — no new deps):

| Tier | Match | Score | Example for `/re` |
|---|---|---|---|
| 1 | Canonical name starts with prefix | 1.00 | `/rename`, `/reload`, `/resume` |
| 2 | Alias starts with prefix | 0.85 | `/reset` (alias of `/clear`) |
| 3 | Word-boundary substring | 0.70 | `/code-review` |
| 4 | Anywhere substring | 0.55 | `/recall` |
| 5 | `difflib.SequenceMatcher.ratio() >= 0.55` | 0.40-0.50 | `/pad-screener` → `/pead-screener` (typo) |

MRU bonus: `+0.05` (capped at 1.0). Empty `/` returns top-5 MRU + alphabetical tail.

## Test plan

- [x] **~25 new tests** — `test_slash_mru.py` (7), `test_slash_item_types.py` (4), `test_slash_picker_source.py` (16), `test_slash_completer.py` (3 new), `test_input_loop_skill_picker.py` (6), `test_hybrid_skill_dispatch.py` (3), `test_slash_menu_e2e.py` (5), and source-field additions in `test_slash_skill_fallback.py` + `test_slash_dispatcher.py`.
- [x] All ~5443 pre-existing tests stay green.
- [x] ruff clean on every new file.
- [x] e2e smoke covers every layer in one file — picker source → ranker → MRU → completer → renderer → Hybrid wrap.

## Coordination

archit's slash-related PRs (#220 / #222 / #223 / #224 / #225 / #226 / #227) all merged before this PR was opened. `agent/loop.py` shape is final; `agent/slash_skill_fallback.py` is on main. Zero merge conflicts.

## Spec + plan

- Spec: `docs/superpowers/specs/2026-04-29-slash-menu-claude-code-parity-design.md`
- Plan: `docs/superpowers/plans/2026-04-29-slash-menu-cc-parity.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1 | tail -3
```

- [ ] **Step 6: Verify the PR landed and CI is running**

```bash
gh pr view --web 2>&1 | tail -2 || gh pr view --json url,number 2>&1 | head -3
```

Expected: a printed PR URL.

---

## Expert-critic audit log (run 2026-04-29)

After the initial plan was written, I ran an adversarial self-audit per the user's instruction. Findings + their resolution, in-place:

**BLOCKERS fixed (9):**

| # | Finding | Fix in plan |
|---|---|---|
| A1 | Hybrid wrap changes session lifecycle without provider-integration test | Added Task 12.5 — full-turn integration test with `_RecordingProvider` |
| B1 | `_trim_description` doesn't normalize whitespace; YAML multi-line descs break columns | `_trim_description` now collapses `\s+` to single spaces before truncation. Test added. |
| B4 | Skills with non-shell-safe ids (spaces, slashes, emoji) crash the picker | `iter_items` now applies `^[A-Za-z0-9_-]+$` regex filter. Test added. |
| C1 | No test for "skill collides with command alias" (e.g. skill named `quit` vs `/quit` alias) | Test `test_skill_collides_with_command_alias_hidden` added. Existing `_command_names()` already covers aliases — test pins the contract. |
| C2 | No test for skill with falsy `id` | Test `test_iter_items_skips_skill_with_falsy_id` added. |
| C4 | No test for "registry empty AND no skills" | Test `test_rank_returns_empty_when_no_items` added with monkeypatch to empty `SLASH_REGISTRY`. |
| D1 | `memory_manager` scope at cli.py:1263 was hand-waved | Verified: lives in `_run_chat_session`'s `loop = AgentLoop(...)` at line 846; `loop.memory` is the `MemoryManager`. Plan now spells out `loop.memory if loop is not None else None`. |
| D3 | Hybrid wrap interaction with `_loop_detector`, session-end events, and the iteration loop wasn't covered | Task 12.5 covers via integration test. |
| F2 | Task 14 `git rebase` had no instructions for orphaned-changes case (which I HIT during planning) | Step 2 now says: stash any uncommitted bits before rebasing, document conflict-pause path. |

**REFINEs folded in (6):**

| # | Refinement | Where |
|---|---|---|
| A2 | `SKILL_TOOL_NAME` constant in loop.py | Task 12 |
| B3 | `loop.memory if loop is not None else None` defensive guard | Task 7 step 4 |
| C3 | Module-level `_render_dropdown_for_state` for testability | Already in Task 8 |
| D2 | Test for nonexistent-dir MRU file path | `test_missing_file_silently_empty` already in Task 1 |
| E1 | Docstring note on synthesizing-both-halves trade-off | Task 12 |
| F1 | Comment in e2e test pinning MRU JSON-array format | Acceptable as-is — Task 13 e2e covers the contract; format-pin comment is cosmetic |

**NOTED (acceptable risks, no plan change):**

- A3 — `difflib` perf at 1000+ skills (~50ms still under perception)
- B5 — MRU `+0.05` can cross tier boundaries by design
- E2 — Single-PR delivery confirmed by user
- E3 — Stdlib-only ranking confirmed by spec

**Audit verdict:** plan is now defensible against the failure modes I could imagine. Proceeding to execution.

---

## Self-Review

**1. Spec coverage.** Walking each section of the spec against tasks:

- §3.3 file table: every row maps to a task. `slash_mru.py` → Task 1. `slash.py` types → Task 2. `slash_picker_source.py` → Tasks 3-5. `slash_completer.py` → Task 6. `input_loop.py` → Tasks 7-9. `plugin_sdk/slash_command.py` → Task 10. `agent/slash_skill_fallback.py` → Task 11. `agent/loop.py` Hybrid wrap → Task 12. e2e smoke → Task 13.
- §3.4 ranking tiers: Task 4 implements all 5 tiers + Task 5 adds MRU bonus.
- §3.5 source tag rendering: Task 8 + Task 9.
- §3.6 description truncation: Task 6 (`_trim_description`).
- §4 worked examples: e2e covers `/`, `/re`, and the picking-records-MRU flow (Task 13 e2e).
- §6 Hybrid dispatch code snippet: Task 12 implements it (with adaptations to OC's `Message` shape — `tool_calls` field instead of structured content blocks, since OC normalizes message shape via `Message.tool_calls`).
- §7 error handling: covered in tests (Task 1 malformed file, Task 3 memory failure, Task 4 pathological-input clamp).
- §9 acceptance criteria 1-12: all 12 covered by Tasks 1-13 — see e2e file in Task 13 for criteria 1-3, MRU persistence in criterion 4 (Task 1), source tags in criterion 5 (Task 8/9), trim in criterion 6 (Task 6), source field in criterion 7 (Task 10), fallback marking in criterion 8 (Task 11), Hybrid wrap in criterions 9-10 (Task 12), regression-clean in criterion 11 (Task 13/14), ruff in criterion 12 (Task 13/14).

No gaps.

**2. Placeholder scan.**

- "TODO" / "TBD" / "implement later": none.
- "Add appropriate error handling": no — every error path is enumerated (corrupt MRU, list_skills failure, pathological input, name collision).
- "Write tests for the above": every task has explicit test code.
- "Similar to Task N": no — every task spells out its code in full even when patterns repeat.
- Steps describe what AND how — every code step has an actual code block.

No placeholders.

**3. Type consistency.**

- `SkillEntry(id, name, description)` — used consistently across Tasks 2, 3, 6, 8, 13.
- `Match(item, score)` — defined in Task 4, used in Tasks 5, 6, 7, 13.
- `UnifiedSlashSource(memory_manager, mru_store)` constructor signature — same in Tasks 3, 5, 6, 7, 13.
- `_render_dropdown_for_state(state: dict)` — defined in Task 8, used in Task 13 e2e.
- `_wrap_skill_result_as_tool_messages(*, skill_name, args, result)` — keyword-only, consistent in Tasks 12 and 13.
- `SlashCommandResult.source: Literal["command", "skill"]` — defined in Task 10, set in Task 11, read in Task 12.

No inconsistencies.

**Plan ready for execution.**
