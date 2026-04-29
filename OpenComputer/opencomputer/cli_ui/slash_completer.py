"""Slash command + skill autocomplete for the OpenComputer TUI.

Uses :class:`UnifiedSlashSource` (when supplied) to mix commands and
skills in a single dropdown, ranked by tier + MRU recency. Falls back
to the legacy ``SLASH_REGISTRY``-only behavior when no source is
passed (preserves backward compat for the ``build_prompt_session``
caller and the test fixtures that wired it pre-skills).

Each row's display carries the source tag — ``(command)`` or
``(skill)`` — so the user can tell them apart.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from .slash import SLASH_REGISTRY, CommandDef, SlashItem
from .slash_picker_source import UnifiedSlashSource

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
        # ``//`` (double-slash) yields nothing — preserved from prior behavior.
        if prefix.startswith("/"):
            return
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
