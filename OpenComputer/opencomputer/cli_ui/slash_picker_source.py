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
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from opencomputer.cli_ui import slash as _slash_mod
from opencomputer.cli_ui.slash import CommandDef, SkillEntry, SlashItem
from opencomputer.cli_ui.slash_mru import MruStore

_log = logging.getLogger("opencomputer.cli_ui.slash_picker_source")

#: Skill ids must be shell-safe — no whitespace, no slashes, ascii only.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

#: Default cap on rendered rows. Spec §3.4. Caller can override via ``top_n``.
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
        for cmd in _slash_mod.SLASH_REGISTRY:
            out.add(cmd.name)
            for alias in cmd.aliases:
                out.add(alias)
        return out

    def iter_items(self) -> Iterable[SlashItem]:
        """Yield all picker-eligible items.

        Commands are yielded first (registry order — preserves the
        order curated in ``slash.py``). Skills follow, with any that
        collide with a command name suppressed. Skills with unsafe
        ids are also skipped.

        ``SLASH_REGISTRY`` is accessed via the module attribute (not
        a captured reference) so tests can monkeypatch the registry.
        """
        yield from _slash_mod.SLASH_REGISTRY

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

        Empty prefix: MRU-recent items first (top 5), then everything
        else alphabetically. Non-empty prefix: tiered ranking + MRU
        bonus, score desc with alphabetical tie-break.
        """
        items = list(self.iter_items())
        prefix_lc = prefix.lower().strip()

        if not prefix_lc:
            # Empty prefix: MRU items first (most-recent first), then
            # alphabetical for the rest.
            mru_names = self._mru_recent_names()
            mru_top5 = mru_names[:5]
            mru_set = set(mru_top5)
            mru_floated: list[Match] = []
            for name in mru_top5:
                for i in items:
                    if self._name_of(i) == name:
                        mru_floated.append(Match(item=i, score=1.0))
                        break
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

        # Sort: score desc, then name asc.
        scored.sort(key=lambda m: (-m.score, self._name_of(m.item)))
        return scored[:top_n]

    def _mru_recent_names(self) -> list[str]:
        """Names from MRU log, most-recent first."""
        # Newest entries are appended to the end; reverse for most-recent-first.
        return [e["name"] for e in reversed(self._mru._entries)]


__all__ = ["Match", "UnifiedSlashSource"]
