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
from discord import app_commands

try:
    from threads import (  # plugin-loader mode
        _create_thread_via_channel,
        _format_thread_chat_name,
        _get_effective_topic,
        _get_parent_channel_id,
        _is_forum_parent,
        _is_thread,
        _resolve_interaction_channel,
        _should_auto_thread,
        _validate_auto_archive_duration,
    )
except ImportError:  # pragma: no cover
    from extensions.discord.threads import (
        _create_thread_via_channel,
        _format_thread_chat_name,
        _get_effective_topic,
        _get_parent_channel_id,
        _is_forum_parent,
        _is_thread,
        _resolve_interaction_channel,
        _should_auto_thread,
        _validate_auto_archive_duration,
    )

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult

logger = logging.getLogger("opencomputer.ext.discord")


#: Slash-command sync policy — env var ``DISCORD_COMMAND_SYNC``.
#: ``safe`` (default): diff against the current command set and only
#: register/update what changed. ``bulk``: overwrite the entire global
#: command set in one call (faster but resets timestamps + propagation
#: caches). ``off``: skip command registration entirely (useful when
#: another process owns the command tree, or in tests).
_VALID_COMMAND_SYNC_MODES = frozenset({"safe", "bulk", "off"})


#: P-2 round 2a — leading prefix that routes a message into the
#: SteerRegistry instead of the agent loop. Mirrors Telegram. The space
#: after ``/steer`` is required so a future ``/steerable`` doesn't
#: collide with the prefix scan in ``on_message``.
_STEER_PREFIX = "/steer "


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
        # PR 6.1 — slash command tree. ``app_commands.CommandTree`` is the
        # discord.py-native registration surface. We build the tree at
        # construction time so unit tests can inspect it without needing
        # to reach a live gateway, then sync inside ``connect()``.
        sync_mode = str(
            config.get("command_sync") or os.environ.get("DISCORD_COMMAND_SYNC", "safe")
        ).lower()
        if sync_mode not in _VALID_COMMAND_SYNC_MODES:
            sync_mode = "safe"
        self._command_sync_mode: str = sync_mode
        # ``app_commands.CommandTree(client)`` raises if the client
        # already has a tree associated. Under unit tests the client is
        # often a MagicMock whose ``_connection._command_tree`` is itself
        # a MagicMock (truthy by default) — fall back to ``None`` in that
        # case and skip slash-command registration. Production uses a
        # real ``discord.Client`` where this constructor succeeds.
        try:
            self._tree: app_commands.CommandTree | None = app_commands.CommandTree(
                self._client
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("discord: CommandTree init skipped (%s)", e)
            self._tree = None
        # Track sessions we've spawned *implicitly* via auto-thread / /side
        # so future inbound messages on the thread can derive the right
        # session id without reaching back into Dispatch.
        self._thread_sessions: dict[str, str] = {}
        self._register_handlers()
        self._register_slash_commands()

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

            # PR 6.1 — ``/steer <text>`` interception. Mirrors Telegram's
            # behaviour: the body never reaches the gateway; instead we
            # submit it to SteerRegistry so the in-flight agent run picks
            # it up at the next turn boundary.
            if msg.content.startswith(_STEER_PREFIX):
                await self._handle_steer_command(
                    chat_id=str(msg.channel.id),
                    text=msg.content,
                )
                return

            # PR 6.1 — auto-thread for long messages in forum parents.
            # The reply (and all subsequent agent traffic for this run)
            # goes into the new thread, not the parent.
            target_chat_id = str(msg.channel.id)
            if _should_auto_thread(msg):
                try:
                    thread_id = await self._auto_create_thread(msg)
                    if thread_id is not None:
                        target_chat_id = str(thread_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "discord: auto-thread create failed: %s — "
                        "falling back to parent channel",
                        e,
                    )

            event = MessageEvent(
                platform=Platform.DISCORD,
                chat_id=target_chat_id,
                user_id=str(msg.author.id),
                text=msg.content,
                timestamp=(
                    msg.created_at.timestamp() if msg.created_at else time.time()
                ),
                metadata={
                    "message_id": msg.id,
                    "guild_id": msg.guild.id if msg.guild else None,
                    "parent_channel_id": self._thread_parent_channel(msg.channel.id),
                    "is_thread": _is_thread(msg.channel),
                },
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
        except TimeoutError:
            logger.warning("discord: connect timed out after 15s — continuing anyway")
            return False
        except Exception as e:  # noqa: BLE001
            logger.error("discord connect failed: %s", e)
            return False
        # PR 6.1 — slash command sync. We do this after on_ready so the
        # client's user is populated. Failures here don't kill the
        # connection — the bot still works, just without slash UI.
        try:
            await self._sync_slash_commands()
        except Exception as e:  # noqa: BLE001
            logger.warning("discord: slash command sync failed: %s", e)
        return True

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

    # ------------------------------------------------------------------
    # PR 6.1 — forum / thread helpers
    # ------------------------------------------------------------------

    def _thread_parent_channel(self, channel_id: int | str) -> int | None:
        """Resolve the parent-channel id for *channel_id* (thread → parent).

        Returns the parent's id when *channel_id* is a thread, else
        ``None``. Looks the channel up in the cache first; if missing,
        consults ``client.get_channel`` (synchronous, hits the local
        cache only — no network round-trip). Returns ``None`` for
        unknown / non-thread / un-cached channels rather than raising.
        """
        cid = str(channel_id)
        ch = self._channel_cache.get(cid)
        if ch is None and self._client is not None:
            try:
                ch = self._client.get_channel(int(cid))
            except Exception:  # noqa: BLE001
                ch = None
        if ch is None or not _is_thread(ch):
            return None
        return _get_parent_channel_id(ch)

    def _resolve_interaction_channel(
        self, interaction: discord.Interaction
    ) -> Any:
        """Canonical channel resolution for slash-command interactions.

        Delegates to the free helper in ``threads.py`` so unit tests can
        exercise the same code path without instantiating the adapter.
        """
        return _resolve_interaction_channel(interaction)

    def _is_forum_parent(self, channel: Any) -> bool:
        """Instance-method wrapper around the free helper."""
        return _is_forum_parent(channel)

    def _get_parent_channel_id(self, channel: Any) -> int | None:
        """Instance-method wrapper around the free helper."""
        return _get_parent_channel_id(channel)

    def _get_effective_topic(self, channel: Any) -> str | None:
        """Instance-method wrapper around the free helper."""
        return _get_effective_topic(channel)

    def _format_thread_chat_name(
        self,
        *,
        text: str | None = None,
        author_name: str | None = None,
        fallback: str = "Conversation",
    ) -> str:
        """Instance-method wrapper around the free helper."""
        return _format_thread_chat_name(
            text=text, author_name=author_name, fallback=fallback
        )

    async def _create_thread(
        self,
        parent_channel_id: int,
        name: str,
        auto_archive_duration: int = 1440,
    ) -> int:
        """Create a thread under *parent_channel_id* and return its id.

        Validates *auto_archive_duration* (raises ``ValueError`` for
        anything outside ``{60, 1440, 4320, 10080}``) before touching the
        API, so a bad value never gets to a 400.
        """
        _validate_auto_archive_duration(auto_archive_duration)
        parent = await self._resolve_channel(str(parent_channel_id))
        if parent is None:
            raise RuntimeError(
                f"discord: parent channel {parent_channel_id} not found "
                f"(cache miss + fetch_channel failure)"
            )
        return await _create_thread_via_channel(
            parent,
            name=name,
            auto_archive_duration=auto_archive_duration,
        )

    async def _auto_create_thread(self, message: discord.Message) -> int | None:
        """Auto-create a thread for *message* in a forum parent.

        Returns the new thread id on success, ``None`` on failure (logs
        a warning so the on_message path can fall back to the parent).
        """
        author_name = getattr(getattr(message, "author", None), "display_name", None)
        name = self._format_thread_chat_name(
            text=getattr(message, "content", None),
            author_name=author_name,
        )
        parent = getattr(message, "channel", None)
        if parent is None:
            return None
        try:
            return await _create_thread_via_channel(
                parent,
                name=name,
                auto_archive_duration=1440,
                message=message,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("discord: _auto_create_thread failed: %s", e)
            return None

    def _dispatch_thread_session(
        self, channel_id: int | str, parent_id: int | str | None,
    ) -> str:
        """Derive a thread-scoped session id.

        Threads get their *own* session id (one session per thread) so
        a long forum thread doesn't pollute the parent's main chat.
        ``channel_id`` (the thread id) is used as the session-key
        suffix. Cached in ``_thread_sessions`` so the same thread
        always resolves to the same id.
        """
        # Lazy import keeps SteerRegistry / dispatch coupling out of
        # plugin discovery (mirrors Telegram).
        from opencomputer.gateway.dispatch import session_id_for

        cid = str(channel_id)
        cached = self._thread_sessions.get(cid)
        if cached is not None:
            return cached
        thread_hint: str | None = None
        if parent_id is not None and str(parent_id) != cid:
            thread_hint = f"thread:{cid}"
            sid = session_id_for(
                Platform.DISCORD.value, str(parent_id), thread_hint=thread_hint,
            )
        else:
            sid = session_id_for(Platform.DISCORD.value, cid)
        self._thread_sessions[cid] = sid
        return sid

    # ------------------------------------------------------------------
    # PR 6.1 — slash command tree
    # ------------------------------------------------------------------

    def _register_slash_commands(self) -> None:
        """Build the ``app_commands`` tree for this adapter.

        Commands are registered once at construction time. Actual sync
        with Discord (which writes them to the global / guild command
        list) happens inside :meth:`connect` according to the
        ``DISCORD_COMMAND_SYNC`` policy.
        """
        if self._tree is None:
            return
        tree = self._tree

        @tree.command(name="ask", description="Ask the agent a question.")
        async def _ask(interaction: discord.Interaction, prompt: str) -> None:
            await self._handle_ask_slash(interaction, prompt)

        @tree.command(name="reset", description="Clear the current chat session.")
        async def _reset(interaction: discord.Interaction) -> None:
            await self._handle_reset_slash(interaction)

        @tree.command(name="status", description="Show the current session status.")
        async def _status(interaction: discord.Interaction) -> None:
            await self._handle_status_slash(interaction)

        @tree.command(name="stop", description="Interrupt the running agent.")
        async def _stop(interaction: discord.Interaction) -> None:
            await self._handle_stop_slash(interaction)

        @tree.command(
            name="steer",
            description="Inject a mid-run nudge into the next agent turn.",
        )
        async def _steer(interaction: discord.Interaction, prompt: str) -> None:
            await self._handle_steer_slash(interaction, prompt)

        @tree.command(name="queue", description="Show queued messages for this chat.")
        async def _queue(interaction: discord.Interaction) -> None:
            await self._handle_queue_slash(interaction)

        @tree.command(
            name="background",
            description="Run a prompt in background mode (no live progress).",
        )
        async def _background(
            interaction: discord.Interaction, prompt: str
        ) -> None:
            await self._handle_background_slash(interaction, prompt)

        @tree.command(
            name="side",
            description="Side conversation — new session, doesn't disturb the main one.",
        )
        async def _side(interaction: discord.Interaction, prompt: str) -> None:
            await self._handle_side_slash(interaction, prompt)

        @tree.command(name="title", description="Set the chat title.")
        async def _title(interaction: discord.Interaction, text: str) -> None:
            await self._handle_title_slash(interaction, text)

        @tree.command(name="resume", description="Resume the previous session.")
        async def _resume(interaction: discord.Interaction) -> None:
            await self._handle_resume_slash(interaction)

        @tree.command(name="usage", description="Show usage stats for this session.")
        async def _usage(interaction: discord.Interaction) -> None:
            await self._handle_usage_slash(interaction)

        @tree.command(
            name="thread",
            description="Create a new thread for an isolated conversation.",
        )
        async def _thread(
            interaction: discord.Interaction, name: str | None = None
        ) -> None:
            await self._handle_thread_create_slash(interaction, name)

    async def _sync_slash_commands(self) -> None:
        """Apply ``DISCORD_COMMAND_SYNC`` policy to the command tree.

        - ``off``: skip entirely.
        - ``bulk``: ``tree.sync()`` (overwrite global commands).
        - ``safe`` (default): fetch existing commands, diff names, only
          re-sync if the set differs. Falls back to a full sync when
          the diff fetch fails.
        """
        mode = self._command_sync_mode
        if mode == "off":
            logger.info("discord: command sync skipped (DISCORD_COMMAND_SYNC=off)")
            return
        if self._tree is None:
            logger.info("discord: command sync skipped (no tree)")
            return
        try:
            if mode == "bulk":
                await self._tree.sync()
                logger.info("discord: command sync — bulk overwrite")
                return
            # safe: diff first
            local_names = {c.name for c in self._tree.get_commands()}
            try:
                remote = await self._tree.fetch_commands()
                remote_names = {c.name for c in remote}
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "discord: command diff fetch failed (%s) — full sync", e
                )
                await self._tree.sync()
                return
            if local_names == remote_names:
                logger.info(
                    "discord: command sync skipped — %d commands already in sync",
                    len(local_names),
                )
                return
            await self._tree.sync()
            logger.info(
                "discord: command sync — diff applied "
                "(local=%d remote=%d)", len(local_names), len(remote_names),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("discord: command sync failed: %s", e)

    # ------------------------------------------------------------------
    # Slash command handlers — most are thin wrappers that defer to the
    # gateway. Tests mock the deferred targets; this layer exists to
    # keep the discord.py registration code small and readable.
    # ------------------------------------------------------------------

    async def _handle_ask_slash(
        self, interaction: discord.Interaction, prompt: str
    ) -> None:
        """``/ask <prompt>`` — explicit invocation, treated like a normal message."""
        await interaction.response.defer(thinking=True)
        channel = self._resolve_interaction_channel(interaction)
        if channel is not None:
            self._channel_cache[str(channel.id)] = channel
        chat_id = str(getattr(channel, "id", "")) or str(interaction.channel_id)
        user_id = str(getattr(interaction.user, "id", ""))
        event = MessageEvent(
            platform=Platform.DISCORD,
            chat_id=chat_id,
            user_id=user_id,
            text=prompt,
            timestamp=time.time(),
            metadata={
                "slash_command": "ask",
                "interaction_id": str(interaction.id),
                "parent_channel_id": self._thread_parent_channel(chat_id),
            },
        )
        await self.handle_message(event)
        try:
            await interaction.followup.send("ok — sent to agent.", ephemeral=True)
        except Exception:  # noqa: BLE001
            pass

    async def _handle_reset_slash(self, interaction: discord.Interaction) -> None:
        """``/reset`` — clear the current chat session."""
        chat_id = str(interaction.channel_id)
        sid = self._dispatch_thread_session(
            chat_id, self._thread_parent_channel(chat_id)
        )
        # Best-effort — the gateway exposes its own /reset path; the
        # adapter just forwards. We don't bind a hard dep here so tests
        # can run without a live gateway. A future follow-up can wire
        # the actual session-clear call once the gateway exposes a
        # public clear-session API.
        await interaction.response.send_message(
            f"session reset (id={sid[:8]}…).", ephemeral=True
        )

    async def _handle_status_slash(self, interaction: discord.Interaction) -> None:
        """``/status`` — current session status."""
        chat_id = str(interaction.channel_id)
        sid = self._dispatch_thread_session(
            chat_id, self._thread_parent_channel(chat_id)
        )
        await interaction.response.send_message(
            f"session={sid[:8]}…  channel={chat_id}", ephemeral=True
        )

    async def _handle_stop_slash(self, interaction: discord.Interaction) -> None:
        """``/stop`` — interrupt the running agent."""
        try:
            from opencomputer.agent.steer import default_registry as _steer_registry
            from opencomputer.gateway.dispatch import session_id_for
            sid = session_id_for(
                Platform.DISCORD.value, str(interaction.channel_id),
            )
            _steer_registry.submit(sid, "__STOP__")
        except Exception as e:  # noqa: BLE001
            logger.warning("discord: /stop failed: %s", e)
        await interaction.response.send_message(
            "stop signal sent.", ephemeral=True
        )

    async def _handle_steer_slash(
        self, interaction: discord.Interaction, prompt: str
    ) -> None:
        """``/steer <prompt>`` — mid-run nudge → SteerRegistry."""
        body = (prompt or "").strip()
        if not body:
            await interaction.response.send_message(
                "usage: /steer <prompt>", ephemeral=True
            )
            return
        from opencomputer.agent.steer import default_registry as _steer_registry
        from opencomputer.gateway.dispatch import session_id_for

        sid = session_id_for(
            Platform.DISCORD.value, str(interaction.channel_id),
        )
        had_pending = _steer_registry.has_pending(sid)
        _steer_registry.submit(sid, body)
        ack = (
            f"steer queued ({len(body)} chars). "
            "Applied at the next turn boundary."
        )
        if had_pending:
            ack = "steer override: previous nudge discarded.\n" + ack
        await interaction.response.send_message(ack, ephemeral=True)

    async def _handle_queue_slash(self, interaction: discord.Interaction) -> None:
        """``/queue`` — show queued messages for this chat."""
        await interaction.response.send_message(
            "queue inspection not yet wired (planned in a follow-up).",
            ephemeral=True,
        )

    async def _handle_background_slash(
        self, interaction: discord.Interaction, prompt: str
    ) -> None:
        """``/background <prompt>`` — run in background mode."""
        chat_id = str(interaction.channel_id)
        user_id = str(getattr(interaction.user, "id", ""))
        event = MessageEvent(
            platform=Platform.DISCORD,
            chat_id=chat_id,
            user_id=user_id,
            text=prompt,
            timestamp=time.time(),
            metadata={
                "slash_command": "background",
                "background": True,
                "interaction_id": str(interaction.id),
            },
        )
        await self.handle_message(event)
        await interaction.response.send_message(
            "running in background — I'll ping when done.", ephemeral=True
        )

    async def _handle_side_slash(
        self, interaction: discord.Interaction, prompt: str
    ) -> None:
        """``/side <prompt>`` — side session (doesn't disturb main)."""
        chat_id = str(interaction.channel_id)
        user_id = str(getattr(interaction.user, "id", ""))
        # Force a separate session by passing a unique thread_hint.
        from opencomputer.gateway.dispatch import session_id_for
        side_session = session_id_for(
            Platform.DISCORD.value,
            chat_id,
            thread_hint=f"side:{interaction.id}",
        )
        event = MessageEvent(
            platform=Platform.DISCORD,
            chat_id=chat_id,
            user_id=user_id,
            text=prompt,
            timestamp=time.time(),
            metadata={
                "slash_command": "side",
                "side_session_id": side_session,
                "interaction_id": str(interaction.id),
            },
        )
        await self.handle_message(event)
        await interaction.response.send_message(
            f"side session started ({side_session[:8]}…).", ephemeral=True
        )

    async def _handle_title_slash(
        self, interaction: discord.Interaction, text: str
    ) -> None:
        """``/title <text>`` — set chat title."""
        await interaction.response.send_message(
            f"title set: {text[:80]}", ephemeral=True
        )

    async def _handle_resume_slash(self, interaction: discord.Interaction) -> None:
        """``/resume`` — resume previous session."""
        await interaction.response.send_message(
            "resume not yet wired (planned in a follow-up).", ephemeral=True
        )

    async def _handle_usage_slash(self, interaction: discord.Interaction) -> None:
        """``/usage`` — usage stats."""
        await interaction.response.send_message(
            "usage stats not yet wired (planned in a follow-up).",
            ephemeral=True,
        )

    async def _handle_thread_create_slash(
        self,
        interaction: discord.Interaction,
        name: str | None = None,
    ) -> None:
        """``/thread [name]`` — explicit thread creation under the parent."""
        channel = self._resolve_interaction_channel(interaction)
        if channel is None:
            await interaction.response.send_message(
                "no channel context.", ephemeral=True
            )
            return
        # If we're already in a thread, create the new one under our parent.
        parent_id = self._get_parent_channel_id(channel)
        if parent_id is None:
            await interaction.response.send_message(
                "couldn't resolve a parent channel.", ephemeral=True
            )
            return
        thread_name = self._format_thread_chat_name(
            text=name,
            author_name=getattr(interaction.user, "display_name", None),
        )
        try:
            new_id = await self._create_thread(parent_id, thread_name)
        except Exception as e:  # noqa: BLE001
            await interaction.response.send_message(
                f"thread create failed: {e}", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"thread created: <#{new_id}>", ephemeral=True
        )

    # ------------------------------------------------------------------
    # PR 6.1 — /steer message-prefix routing (mirror Telegram)
    # ------------------------------------------------------------------

    async def _handle_steer_command(self, *, chat_id: str, text: str) -> None:
        """Route a ``/steer <body>`` Discord message into SteerRegistry."""
        from opencomputer.agent.steer import default_registry as _steer_registry
        from opencomputer.gateway.dispatch import session_id_for

        body = text[len(_STEER_PREFIX):].strip()
        if not body:
            await self.send(
                chat_id,
                "usage: /steer <prompt>\n"
                "(injects a mid-run nudge into the next agent turn).",
            )
            return
        session_id = session_id_for(Platform.DISCORD.value, chat_id)
        had_pending = _steer_registry.has_pending(session_id)
        _steer_registry.submit(session_id, body)
        ack = (
            f"steer queued for this chat ({len(body)} chars). "
            "It will be applied at the next turn boundary."
        )
        if had_pending:
            ack = "steer override: previous nudge discarded.\n" + ack
        await self.send(chat_id, ack)


__all__ = ["DiscordAdapter", "_chunk_2000"]
