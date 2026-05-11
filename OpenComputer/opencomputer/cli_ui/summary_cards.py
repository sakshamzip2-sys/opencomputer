"""Pure-text Rich-friendly cards for session lifecycle events.

Ported from pi's:
* ``modes/interactive/components/branch-summary-message.ts``
* ``modes/interactive/components/compaction-summary-message.ts``

PI uses its TUI component system to render proper chat-cards when a
session forks or a compaction runs. OC's chat surface is a plain text
stream (SlashCommandResult.output → assistant message), so the port
renders ASCII / Unicode box-drawing cards that work in any terminal
that can display the rest of OC's chat output.

These functions return plain strings — the caller embeds the card in
its existing output channel. No Rich Console dependency, no new
SlashCommandResult fields, no plugin SDK changes.
"""

from __future__ import annotations


def _format_count(n: int) -> str:
    """Compact integer formatting — ``12,345`` reads better than ``12345``
    in a card and the OC chat is wide enough to fit commas."""
    return f"{n:,}"


def _truncate_title(title: str, *, max_width: int = 50) -> str:
    """Keep the card from running off-screen on long branch titles."""
    title = title.strip() or "(fork)"
    if len(title) <= max_width:
        return title
    return title[: max_width - 1].rstrip() + "…"


def render_branch_card(
    *,
    new_session_id: str,
    title: str,
    messages_copied: int,
) -> str:
    """Render the ``/branch`` outcome as a chat-friendly card.

    The card shows the fork's short id, its title, and the resume
    command. Mirrors the information surfaced by PI's
    ``BranchSummaryMessage`` component.
    """
    short_id = new_session_id[:8]
    display_title = _truncate_title(title)
    msg_phrase = (
        f"{_format_count(messages_copied)} message"
        + ("s" if messages_copied != 1 else "")
        + " copied"
    )

    lines = [
        "╭─ branch ─────────────────────────────────────╮",
        f"│ {display_title}",
        f"│   id: {short_id}…",
        f"│   {msg_phrase}",
        f"│   resume: oc chat --resume {new_session_id}",
        "╰──────────────────────────────────────────────╯",
    ]
    return "\n".join(lines)


def _savings_phrase(before: int, after: int) -> str:
    """Compact "saved 38000 (76%)" / "grew by 100 (+100%)" phrase.

    Handles edge cases: before=0 (no division), after>=before (pathological
    "compaction" that didn't save anything)."""
    if before == 0:
        return "no change" if after == 0 else f"+{_format_count(after)}"
    delta = before - after
    pct = round((delta / before) * 100)
    if delta > 0:
        return f"saved {_format_count(delta)} ({pct}%)"
    if delta == 0:
        return "no change"
    return f"grew {_format_count(-delta)} ({-pct}%)"


def render_compaction_card(
    *,
    messages_before: int,
    messages_after: int,
    tokens_before: int | None = None,
    tokens_after: int | None = None,
    reason: str,
) -> str:
    """Render a compaction event as a chat-friendly card.

    The token row is rendered only when BOTH ``tokens_before`` and
    ``tokens_after`` are concrete integers — passing ``None`` omits
    the row entirely rather than show a misleading ``0 → 0``. This
    matches the honest semantics for queued compactions where the
    caller has message counts but no token data yet.
    """
    msg_savings = _savings_phrase(messages_before, messages_after)
    lines = [
        "╭─ compaction ─────────────────────────────────╮",
        f"│ reason: {reason}",
        f"│ messages: {_format_count(messages_before)} → "
        f"{_format_count(messages_after)} ({msg_savings})",
    ]
    if tokens_before is not None and tokens_after is not None:
        tok_savings = _savings_phrase(tokens_before, tokens_after)
        lines.append(
            f"│ tokens:   {_format_count(tokens_before)} → "
            f"{_format_count(tokens_after)} ({tok_savings})"
        )
    lines.append("╰──────────────────────────────────────────────╯")
    return "\n".join(lines)
