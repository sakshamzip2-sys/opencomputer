"""Discord forum + auto-thread helpers (PR 6.1).

Standalone functions used by :class:`extensions.discord.adapter.DiscordAdapter`
to:

* Detect when a channel is a *forum parent* vs a regular text channel.
* Resolve the parent of a thread channel (so session ids derive from a
  stable parent id even when the user posts inside a transient thread).
* Create a new thread under a forum/text parent with a validated
  ``auto_archive_duration`` (Discord only accepts {60, 1440, 4320,
  10080} minutes — anything else is a 400 from the API).
* Auto-create threads for "long" messages posted directly into a forum
  parent — Discord's UX nudges users toward threading there.
* Format a friendly thread title from the inbound message.
* Compute the *effective topic* for session resolution: a thread inherits
  its parent's topic so two threads under the same forum collapse onto
  the same session unless the caller wants per-thread isolation.

The helpers are deliberately pure (no I/O of their own) wherever
possible so they can be unit-tested without spinning up a discord.py
client. Methods that DO touch discord.py (``_create_thread``) are still
free functions taking the client + channel objects as args, again to
keep tests simple.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

logger = logging.getLogger("opencomputer.ext.discord.threads")


#: Discord-API-accepted values for ``auto_archive_duration`` (minutes).
#: 60 = 1h, 1440 = 24h, 4320 = 3d, 10080 = 7d. Anything else returns
#: a 400 from the REST endpoint, so we validate up-front.
VALID_AUTO_ARCHIVE_DURATIONS: frozenset[int] = frozenset({60, 1440, 4320, 10080})


#: Threshold above which an inbound message in a forum-parent channel
#: triggers automatic thread creation. The number is intentionally
#: generous — short reactions like "lol", "+1" don't warrant a thread,
#: but anything that looks like a question / paragraph does.
LONG_MESSAGE_THRESHOLD = 280


# ---------------------------------------------------------------------------
# Channel-type detection
# ---------------------------------------------------------------------------


def _is_forum_parent(channel: Any) -> bool:
    """True iff *channel* is a Discord forum-parent channel.

    Forum parents have ``ChannelType.forum`` (or ``media``, the newer
    sibling type). They cannot be posted to directly — every message
    becomes its own thread.

    Resilient to the channel object lacking ``.type`` (some mocks
    don't set it); returns False in that case.
    """
    ct = getattr(channel, "type", None)
    if ct is None:
        return False
    try:
        return ct in (
            discord.ChannelType.forum,
            getattr(discord.ChannelType, "media", discord.ChannelType.forum),
        )
    except Exception:  # noqa: BLE001
        return False


def _is_thread(channel: Any) -> bool:
    """True iff *channel* is a thread (public, private, news, or forum-post)."""
    ct = getattr(channel, "type", None)
    if ct is None:
        # Thread instances expose a ``.parent`` attr — fall back on that.
        return getattr(channel, "parent", None) is not None and not _is_forum_parent(
            getattr(channel, "parent", None)
        )
    try:
        return ct in (
            discord.ChannelType.public_thread,
            discord.ChannelType.private_thread,
            discord.ChannelType.news_thread,
        )
    except Exception:  # noqa: BLE001
        return False


def _get_parent_channel_id(channel: Any) -> int | None:
    """Resolve the parent channel id for *channel*.

    * Thread → parent channel id (stable across thread lifecycle).
    * Forum parent → its own id.
    * Regular text channel → its own id.
    * None / no id → ``None``.
    """
    if channel is None:
        return None
    if _is_thread(channel):
        parent = getattr(channel, "parent", None)
        if parent is not None and getattr(parent, "id", None) is not None:
            return int(parent.id)
        # Some library versions surface ``parent_id`` directly.
        pid = getattr(channel, "parent_id", None)
        if pid is not None:
            try:
                return int(pid)
            except (TypeError, ValueError):
                return None
    cid = getattr(channel, "id", None)
    if cid is None:
        return None
    try:
        return int(cid)
    except (TypeError, ValueError):
        return None


def _get_effective_topic(channel: Any) -> str | None:
    """Return the topic string for *channel*, walking thread → parent.

    Used by session-id derivation when the caller wants a thread to
    *inherit* its parent's topic (so the session bucket stays stable
    across short-lived threads). For non-thread channels this is just
    ``channel.topic``.
    """
    if channel is None:
        return None
    if _is_thread(channel):
        parent = getattr(channel, "parent", None)
        if parent is not None:
            return getattr(parent, "topic", None)
    return getattr(channel, "topic", None)


# ---------------------------------------------------------------------------
# Slash-command channel resolution
# ---------------------------------------------------------------------------


def _resolve_interaction_channel(interaction: Any) -> Any:
    """Pick the canonical channel for a slash-command interaction.

    discord.py exposes ``interaction.channel`` which is typically the
    thread the user invoked the command from. For session keying we
    usually want the *thread itself* (one session per thread); but the
    PARENT id is what we use for permission / forum detection. This
    helper just returns the interaction's channel object — callers
    decide which id to pull off it.
    """
    return getattr(interaction, "channel", None)


# ---------------------------------------------------------------------------
# Thread creation
# ---------------------------------------------------------------------------


def _validate_auto_archive_duration(duration: int) -> int:
    """Raise ``ValueError`` if *duration* isn't a discord-accepted value."""
    if duration not in VALID_AUTO_ARCHIVE_DURATIONS:
        raise ValueError(
            f"auto_archive_duration must be one of "
            f"{sorted(VALID_AUTO_ARCHIVE_DURATIONS)}, got {duration!r}"
        )
    return duration


async def _create_thread_via_channel(
    parent_channel: Any,
    *,
    name: str,
    auto_archive_duration: int = 1440,
    message: Any | None = None,
) -> int:
    """Create a thread under *parent_channel* and return the new thread id.

    * For a forum parent we use ``create_thread(name=..., content=...)``
      which Discord requires (forum posts have a starter message).
    * For a regular text channel with an attached *message* we use
      ``message.create_thread(name=...)`` so the thread anchors on that
      message.
    * Otherwise we fall back to ``channel.create_thread(name=...)``.
    """
    _validate_auto_archive_duration(auto_archive_duration)
    name = _truncate_thread_name(name)
    if _is_forum_parent(parent_channel):
        # Forum parents need a starter content body.
        starter = (
            getattr(message, "content", None)
            or "Auto-created by OpenComputer."
        )
        thread = await parent_channel.create_thread(
            name=name,
            auto_archive_duration=auto_archive_duration,
            content=starter,
        )
        # Forum.create_thread returns ThreadWithMessage in newer
        # discord.py — both shapes expose .thread.id or .id.
        target = getattr(thread, "thread", thread)
        return int(target.id)
    if message is not None and hasattr(message, "create_thread"):
        thread = await message.create_thread(
            name=name,
            auto_archive_duration=auto_archive_duration,
        )
        return int(thread.id)
    thread = await parent_channel.create_thread(
        name=name,
        auto_archive_duration=auto_archive_duration,
    )
    return int(thread.id)


# ---------------------------------------------------------------------------
# Friendly thread name from message context
# ---------------------------------------------------------------------------


#: Discord caps thread titles at 100 chars.
_THREAD_NAME_MAX = 100


def _truncate_thread_name(name: str) -> str:
    """Trim *name* to Discord's 100-char limit, adding an ellipsis."""
    name = (name or "").strip()
    if not name:
        return "Conversation"
    if len(name) <= _THREAD_NAME_MAX:
        return name
    return name[: _THREAD_NAME_MAX - 1].rstrip() + "…"


def _format_thread_chat_name(
    *,
    text: str | None = None,
    author_name: str | None = None,
    fallback: str = "Conversation",
) -> str:
    """Produce a friendly thread title from inbound message context.

    Strategy:
    * Take the first line of *text* (split on ``\\n``).
    * Strip leading slash-command tokens (``/ask``, ``/background``...).
    * If empty, fall back to ``"chat with <author>"``.
    * Always truncate to Discord's 100-char limit.
    """
    raw = (text or "").strip()
    if raw:
        first_line = raw.splitlines()[0].strip()
        # Strip a leading ``/word `` slash-command token.
        if first_line.startswith("/"):
            parts = first_line.split(" ", 1)
            first_line = parts[1].strip() if len(parts) > 1 else ""
        if first_line:
            return _truncate_thread_name(first_line)
    if author_name:
        return _truncate_thread_name(f"chat with {author_name}")
    return _truncate_thread_name(fallback)


# ---------------------------------------------------------------------------
# Auto-thread heuristic
# ---------------------------------------------------------------------------


def _should_auto_thread(message: Any) -> bool:
    """Heuristic: should the adapter auto-create a thread for *message*?

    Trigger when:
    * Message landed in a forum-parent channel, AND
    * The text is "long enough" to warrant a thread (avoids spawning
      threads for one-word reactions).

    Forum parents *technically* require every post to be a thread, but
    discord.py surfaces the starter message via ``on_message`` before
    discord auto-bundles it — depending on intent setup, the bot may
    see the bare message. We auto-thread defensively in either case.
    """
    channel = getattr(message, "channel", None)
    if not _is_forum_parent(channel):
        return False
    content = getattr(message, "content", "") or ""
    return len(content) >= LONG_MESSAGE_THRESHOLD


__all__ = [
    "VALID_AUTO_ARCHIVE_DURATIONS",
    "LONG_MESSAGE_THRESHOLD",
    "_create_thread_via_channel",
    "_format_thread_chat_name",
    "_get_effective_topic",
    "_get_parent_channel_id",
    "_is_forum_parent",
    "_is_thread",
    "_resolve_interaction_channel",
    "_should_auto_thread",
    "_truncate_thread_name",
    "_validate_auto_archive_duration",
]
