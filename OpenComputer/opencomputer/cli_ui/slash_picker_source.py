"""Unified slash picker source — the single source of truth that
``SlashCommandCompleter`` and ``read_user_input``'s ``_refilter`` both
read from.

Walks the :data:`SLASH_REGISTRY` for built-in commands and
:meth:`MemoryManager.list_skills` for installed skills, deduping on
name collision (command always wins), and exposes a ranked search via
:meth:`UnifiedSlashSource.rank` (added in Task 4).

Pure logic — no IO except via the injected ``memory_manager`` (for
skills) and ``mru_store`` (for recency bonus). Designed to be cheap to
construct per session and to call once per keystroke.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from opencomputer.cli_ui.slash import SLASH_REGISTRY, CommandDef, SkillEntry, SlashItem
from opencomputer.cli_ui.slash_mru import MruStore

_log = logging.getLogger("opencomputer.cli_ui.slash_picker_source")

#: Skill ids must be shell-safe — no whitespace, no slashes, ascii only.
#: Skills failing this filter are silently dropped from the picker
#: because the user couldn't invoke them as ``/<id>`` even if shown.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class UnifiedSlashSource:
    """Yields mixed CommandDef + SkillEntry rows for the picker.

    ``iter_items()`` returns the full deduped list (commands first, then
    skills). ``rank(prefix)`` (Task 4) returns a ranked subset.
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


__all__ = ["UnifiedSlashSource"]
