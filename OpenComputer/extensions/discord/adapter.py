"""
DiscordAdapter — Discord channel adapter using discord.py.

Uses discord.py's event-driven client: on_message → MessageEvent →
gateway dispatch. Sends replies via message.channel.send. Handles the
2000-char message limit with split-on-line-boundary chunking.

Capabilities (G.12 — Tier 2.8): typing, reactions, edit, delete.
Discord supports more (file uploads, threads) but those land separately.

Connects via DISCORD_BOT_TOKEN env var. Requires the "message_content"
intent — enable it in the bot's settings on Discord Developer Portal.

Hermes-port (PR 3b.1) gating:

* ``discord.require_mention`` (default ``False``) — when ``True`` the
  bot only responds in guild channels if it was @-mentioned (either as
  a user mention or via a role it carries). DMs always pass.
* ``discord.allowed_users`` — optional allowlist of Discord user IDs
  (int or str). Empty list ⇒ no user gate.
* ``discord.allowed_roles`` — optional allowlist of role IDs the
  message author must carry at least one of. Empty ⇒ no role gate.
  ``allowed_users`` and ``allowed_roles`` use OR semantics: if either
  list is configured, the message passes when EITHER list matches.
* ``discord.allow_bots`` — ``"none"`` (default — preserves existing
  behaviour: ignore other bots), ``"mentions"`` (process bot messages
  only when we're mentioned), or ``"all"``.

Multi-bot disambiguation: if other bots are mentioned in the message
but we are not, the message is silently dropped — even when
``require_mention=False`` — so bots don't talk over each other.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import discord

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult

logger = logging.getLogger("opencomputer.ext.discord")


# PR 4.3 — Discord ``allowed_mentions`` safe defaults. By default we
# disable @everyone / @here / role pings so a runaway agent cannot
# accidentally page a whole guild. Operators can opt back in via env:
#
#   DISCORD_ALLOW_MENTION_EVERYONE  → enable @everyone / @here pings
#   DISCORD_ALLOW_MENTION_ROLES     → enable @role pings
#   DISCORD_ALLOW_MENTION_USERS     → user pings (default ON — disable
#                                     to suppress reply pings entirely)
#   DISCORD_ALLOW_MENTION_REPLIED_USER → ping the author of the
#                                        message we replied to (default ON)
#
# Each var accepts ``"1"``, ``"true"``, ``"yes"`` (case-insensitive)
# as truthy; everything else (including unset) takes the documented
# default.
_TRUTHY_ENV = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    """Parse a truthy-string env var. Unset → default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY_ENV


def _chunk_2000(text: str, limit: int = 2000) -> list[str]:
    """Split `text` so each chunk is <= limit chars, respecting line breaks."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        ll = len(line)
        # Single line > limit: hard-split
        if ll > limit:
            if current:
                chunks.append("".join(current))
                current, current_len = [], 0
            for i in range(0, ll, limit):
                chunks.append(line[i : i + limit])
            continue
        if current_len + ll > limit and current:
            chunks.append("".join(current))
            current = [line]
            current_len = ll
        else:
            current.append(line)
            current_len += ll
    if current:
        chunks.append("".join(current))
    return chunks


class DiscordAdapter(BaseChannelAdapter):
    platform = Platform.DISCORD
    max_message_length = 2000
    capabilities = (
        ChannelCapabilities.TYPING
        | ChannelCapabilities.REACTIONS
        | ChannelCapabilities.EDIT_MESSAGE
        | ChannelCapabilities.DELETE_MESSAGE
        | ChannelCapabilities.THREADS
    )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.token = config["bot_token"]
        intents = discord.Intents.default()
        intents.message_content = True  # required for reading message text
        intents.dm_messages = True
        intents.guild_messages = True
        self._client = discord.Client(intents=intents)
        self._bot_user_id: int | None = None
        self._client_task: asyncio.Task | None = None
        self._channel_cache: dict[str, discord.abc.Messageable] = {}
        self._ready_event = asyncio.Event()
        # PR 3b.1 — gating config (defaults preserve previous behaviour).
        self._require_mention: bool = bool(config.get("require_mention", False))
        self._allowed_users: set[str] = {
            str(u) for u in (config.get("allowed_users") or [])
        }
        self._allowed_roles: set[str] = {
            str(r) for r in (config.get("allowed_roles") or [])
        }
        allow_bots = str(config.get("allow_bots", "none")).lower()
        if allow_bots not in {"none", "mentions", "all"}:
            allow_bots = "none"
        self._allow_bots: str = allow_bots
        self._register_handlers()

    def _register_handlers(self) -> None:
        @self._client.event
        async def on_ready() -> None:
            self._bot_user_id = self._client.user.id if self._client.user else None
            logger.info(
                "discord: connected as %s (id=%s)",
                self._client.user,
                self._bot_user_id,
            )
            self._ready_event.set()

        @self._client.event
        async def on_message(msg: discord.Message) -> None:
            # Skip our own messages (prevents echo loops)
            if self._bot_user_id is not None and msg.author.id == self._bot_user_id:
                return
            # Skip empty / non-text
            if not msg.content:
                return
            if not self._should_process(msg):
                return
            # Cache channel for later sends
            self._channel_cache[str(msg.channel.id)] = msg.channel
            event = MessageEvent(
                platform=Platform.DISCORD,
                chat_id=str(msg.channel.id),
                user_id=str(msg.author.id),
                text=msg.content,
                timestamp=(
                    msg.created_at.timestamp() if msg.created_at else time.time()
                ),
                metadata={"message_id": msg.id, "guild_id": msg.guild.id if msg.guild else None},
            )
            await self.handle_message(event)

    # ------------------------------------------------------------------
    # Gating helpers — PR 3b.1
    # ------------------------------------------------------------------

    def _is_dm(self, msg: discord.Message) -> bool:
        """True iff this message arrived in a DM (no guild attached)."""
        return getattr(msg, "guild", None) is None

    def _bot_is_mentioned(self, msg: discord.Message) -> bool:
        """Detect whether THIS bot was mentioned.

        Combines:
          1. ``bot.user.mentioned_in(msg)`` — the canonical discord.py
             check (covers @-user mentions and @everyone / @here for
             the bot's own user).
          2. A scan of ``msg.mentions`` — catches role-mentions that
             resolve to a list of users including the bot, which
             ``mentioned_in`` does not always surface depending on
             cache state.
        """
        bot_user = getattr(self._client, "user", None)
        if bot_user is None:
            return False
        try:
            if bot_user.mentioned_in(msg):
                return True
        except Exception:  # noqa: BLE001
            pass
        for u in getattr(msg, "mentions", []) or []:
            if getattr(u, "id", None) == self._bot_user_id:
                return True
        return False

    def _other_bots_mentioned(self, msg: discord.Message) -> bool:
        """True iff another (non-self) bot was mentioned in the message."""
        for u in getattr(msg, "mentions", []) or []:
            if getattr(u, "bot", False) and getattr(u, "id", None) != self._bot_user_id:
                return True
        return False

    def _passes_user_role_allowlist(self, msg: discord.Message) -> bool:
        """OR-semantics allowlist gate.

        - If neither ``allowed_users`` nor ``allowed_roles`` is configured,
          the gate is open.
        - Otherwise the author passes if they appear in
          ``allowed_users`` OR carry at least one role in ``allowed_roles``.
        """
        if not self._allowed_users and not self._allowed_roles:
            return True
        author = getattr(msg, "author", None)
        author_id = str(getattr(author, "id", ""))
        if author_id and author_id in self._allowed_users:
            return True
        author_role_ids = {
            str(getattr(r, "id", ""))
            for r in (getattr(author, "roles", []) or [])
        }
        return bool(author_role_ids & self._allowed_roles)

    def _should_process(self, msg: discord.Message) -> bool:
        """Apply the full gating chain.

        Order:
          1. Bot-author policy (allow_bots).
          2. Multi-bot disambiguation (silently drop when another bot
             is mentioned and we are NOT).
          3. require_mention (skipped in DMs).
          4. allowed_users / allowed_roles allowlist.
        """
        author = getattr(msg, "author", None)
        author_is_bot = bool(getattr(author, "bot", False))
        is_mentioned = self._bot_is_mentioned(msg)

        if author_is_bot:
            if self._allow_bots == "none":
                return False
            if self._allow_bots == "mentions" and not is_mentioned:
                return False
            # "all" → fall through

        # Multi-bot disambiguation: if another bot is mentioned but we
        # aren't, stay silent so we don't talk over the targeted bot.
        if self._other_bots_mentioned(msg) and not is_mentioned:
            return False

        if self._require_mention and not self._is_dm(msg) and not is_mentioned:
            return False

        return self._passes_user_role_allowlist(msg)

    async def connect(self) -> bool:
        self._client_task = asyncio.create_task(self._client.start(self.token))
        # Wait briefly for on_ready (but don't block the whole gateway startup).
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=15.0)
            return True
        except TimeoutError:
            logger.warning("discord: connect timed out after 15s — continuing anyway")
            return False
        except Exception as e:  # noqa: BLE001
            logger.error("discord connect failed: %s", e)
            return False

    async def disconnect(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass
        if self._client_task is not None:
            self._client_task.cancel()
            try:
                await self._client_task
            except (asyncio.CancelledError, Exception):
                pass

    def _build_allowed_mentions(self) -> discord.AllowedMentions:
        """PR 4.3 — safe-by-default allowed_mentions for outbound posts.

        Defaults: ``everyone=False``, ``roles=False``, ``users=True``,
        ``replied_user=True``. Each can be flipped via env var:

        - ``DISCORD_ALLOW_MENTION_EVERYONE`` (default OFF)
        - ``DISCORD_ALLOW_MENTION_ROLES`` (default OFF)
        - ``DISCORD_ALLOW_MENTION_USERS`` (default ON)
        - ``DISCORD_ALLOW_MENTION_REPLIED_USER`` (default ON)
        """
        return discord.AllowedMentions(
            everyone=_env_bool("DISCORD_ALLOW_MENTION_EVERYONE", False),
            roles=_env_bool("DISCORD_ALLOW_MENTION_ROLES", False),
            users=_env_bool("DISCORD_ALLOW_MENTION_USERS", True),
            replied_user=_env_bool("DISCORD_ALLOW_MENTION_REPLIED_USER", True),
        )

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        channel = self._channel_cache.get(chat_id)
        if channel is None:
            # Try to fetch it
            try:
                channel = await self._client.fetch_channel(int(chat_id))
                self._channel_cache[chat_id] = channel
            except Exception as e:
                return SendResult(success=False, error=f"channel lookup failed: {e}")

        allowed_mentions = self._build_allowed_mentions()

        async def _do_send() -> SendResult:
            last_id: int | None = None
            try:
                for chunk in _chunk_2000(text, limit=self.max_message_length):
                    sent = await channel.send(
                        chunk, allowed_mentions=allowed_mentions
                    )
                    last_id = sent.id
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")
            return SendResult(
                success=True, message_id=str(last_id) if last_id else None
            )

        return await self._send_with_retry(_do_send)

    async def send_typing(self, chat_id: str) -> None:
        channel = self._channel_cache.get(chat_id)
        if channel is None:
            return
        try:
            await channel.typing().__aenter__()  # fire the typing indicator once
        except Exception:
            pass  # best effort

    # ------------------------------------------------------------------
    # G.12 — reactions, edit, delete (ChannelCapabilities)
    # ------------------------------------------------------------------

    async def send_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
        **kwargs: Any,
    ) -> SendResult:
        """Add an emoji reaction to a message via ``message.add_reaction``.

        Discord accepts unicode emoji directly (e.g. ``"👍"``) and custom
        guild emoji as ``"<:name:id>"``. Bot needs ``MANAGE_MESSAGES`` to
        add reactions on channels where it isn't the message author.
        """
        try:
            channel = await self._resolve_channel(chat_id)
            if channel is None:
                return SendResult(success=False, error=f"channel {chat_id} not found")
            msg = await channel.fetch_message(int(message_id))
            await msg.add_reaction(emoji)
            return SendResult(success=True)
        except discord.NotFound:
            return SendResult(success=False, error=f"message {message_id} not found")
        except discord.Forbidden as e:
            return SendResult(success=False, error=f"forbidden: {e}")
        except Exception as e:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(e).__name__}: {e}")

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        text: str,
        **kwargs: Any,
    ) -> SendResult:
        """Edit a previously-sent text message in place via ``message.edit``.

        Discord allows the bot to edit only its OWN messages; editing other
        users' messages requires admin and isn't supported here. No time
        window restriction (unlike Telegram's 48h).
        """
        async def _do_edit() -> SendResult:
            try:
                channel = await self._resolve_channel(chat_id)
                if channel is None:
                    return SendResult(
                        success=False, error=f"channel {chat_id} not found"
                    )
                msg = await channel.fetch_message(int(message_id))
                # PR 4.3 — same safe-by-default mention policy on edit.
                await msg.edit(
                    content=text[: self.max_message_length],
                    allowed_mentions=self._build_allowed_mentions(),
                )
                return SendResult(success=True, message_id=str(msg.id))
            except discord.NotFound:
                return SendResult(
                    success=False, error=f"message {message_id} not found"
                )
            except discord.Forbidden as e:
                return SendResult(
                    success=False,
                    error=f"forbidden (bot can only edit its own messages): {e}",
                )
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_edit)

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
        **kwargs: Any,
    ) -> SendResult:
        """Delete a message via ``message.delete``.

        Bots can delete their own messages without special permissions;
        deleting others' messages requires ``MANAGE_MESSAGES``.
        """
        async def _do_delete() -> SendResult:
            try:
                channel = await self._resolve_channel(chat_id)
                if channel is None:
                    return SendResult(
                        success=False, error=f"channel {chat_id} not found"
                    )
                msg = await channel.fetch_message(int(message_id))
                await msg.delete()
                return SendResult(success=True)
            except discord.NotFound:
                return SendResult(
                    success=False, error=f"message {message_id} not found"
                )
            except discord.Forbidden as e:
                return SendResult(success=False, error=f"forbidden: {e}")
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_delete)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_channel(self, chat_id: str) -> discord.abc.Messageable | None:
        """Look up a channel by id, falling back to fetch_channel + caching."""
        channel = self._channel_cache.get(chat_id)
        if channel is not None:
            return channel
        try:
            channel = await self._client.fetch_channel(int(chat_id))
            self._channel_cache[chat_id] = channel
            return channel
        except Exception:
            return None


__all__ = ["DiscordAdapter", "_chunk_2000"]
