"""Minimal IRC (RFC 1459) channel adapter.

Asyncio-based — uses ``asyncio.open_connection`` directly rather than
pulling in the ``irc`` library. Keeps the code surface small (~200 LOC)
and avoids dragging in extra deps.

Protocol coverage:
  - Connect with NICK + USER
  - Join configured channels
  - Send PRIVMSG to channel/nick
  - Receive PRIVMSG (parsed → MessageEvent → handle_message)
  - Respond to server PING with PONG
  - Reconnect on dropped connection (delegated to gateway supervisor)

Out of scope (deferred follow-ups):
  - SASL auth (NickServ password auth works via PASS or services)
  - DCC, CTCP, multi-line server messages, color codes
  - Channel mode tracking, operator commands
  - Bigger-than-512-byte messages (RFC limit)

Config:
  IRC_SERVER     — host:port (e.g. ``irc.libera.chat:6697`` for TLS).
                   Default: ``irc.libera.chat:6667``.
  IRC_NICK       — nickname to register. Default: ``opencomputer``.
  IRC_CHANNELS   — comma-separated channels to join (e.g. ``#opencomputer,#test``).
  IRC_REALNAME   — GECOS / realname. Default: same as nick.
  IRC_PASSWORD   — optional server PASS (NickServ-friendly).
  IRC_TLS        — ``"1"`` to use TLS connection. Default: ``"1"``.
"""
from __future__ import annotations

import asyncio
import os
import ssl
from typing import Any

from plugin_sdk.channel_contract import (
    BaseChannelAdapter,
    ChannelCapabilities,
    SendResult,
)
from plugin_sdk.core import MessageEvent, Platform


def _parse_irc_line(line: str) -> tuple[str | None, str, list[str]]:
    """Parse an IRC line into (prefix, command, params).

    >>> _parse_irc_line(":alice!a@host PRIVMSG #foo :hi there")
    ('alice!a@host', 'PRIVMSG', ['#foo', 'hi there'])
    """
    line = line.rstrip("\r\n")
    prefix: str | None = None
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    # Trailing param (after " :") preserves spaces
    head, _, trailing = line.partition(" :")
    parts = head.split()
    if not parts:
        return prefix, "", []
    command = parts[0]
    params = parts[1:]
    if trailing:
        params.append(trailing)
    return prefix, command, params


def _nick_from_prefix(prefix: str | None) -> str:
    """``alice!a@host`` → ``alice``. None → ``""``."""
    if not prefix:
        return ""
    return prefix.split("!", 1)[0]


class IRCAdapter(BaseChannelAdapter):
    """RFC 1459 IRC adapter."""

    platform = Platform.IRC
    max_message_length = 480  # IRC line limit is 512; reserve room for PRIVMSG prefix
    capabilities = ChannelCapabilities.NONE

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config or {})
        cfg_server = self.config.get("server") or os.environ.get(
            "IRC_SERVER", "irc.libera.chat:6667",
        )
        host_part, _, port_part = cfg_server.partition(":")
        self._host: str = host_part
        self._port: int = int(port_part) if port_part else 6667
        self._nick: str = (
            self.config.get("nick") or os.environ.get("IRC_NICK") or "opencomputer"
        )
        self._realname: str = (
            self.config.get("realname")
            or os.environ.get("IRC_REALNAME")
            or self._nick
        )
        self._password: str | None = (
            self.config.get("password") or os.environ.get("IRC_PASSWORD")
        )
        channels_raw: str = (
            self.config.get("channels") or os.environ.get("IRC_CHANNELS") or ""
        )
        self._channels: list[str] = [
            c.strip() for c in channels_raw.split(",") if c.strip()
        ]
        self._tls: bool = (
            str(self.config.get("tls") or os.environ.get("IRC_TLS") or "1") == "1"
        )

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._connected = False

    async def connect(self) -> bool:
        """Open TCP/TLS connection, register, join channels, start read loop."""
        try:
            ssl_ctx = ssl.create_default_context() if self._tls else None
            self._reader, self._writer = await asyncio.open_connection(
                self._host, self._port, ssl=ssl_ctx,
            )
            if self._password:
                await self._send_raw(f"PASS {self._password}")
            await self._send_raw(f"NICK {self._nick}")
            await self._send_raw(f"USER {self._nick} 0 * :{self._realname}")
            for channel in self._channels:
                await self._send_raw(f"JOIN {channel}")
            self._connected = True
            self._read_task = asyncio.create_task(self._read_loop())
            return True
        except (TimeoutError, OSError) as e:
            self._set_fatal_error(
                code="connect_failed",
                message=f"IRC connect to {self._host}:{self._port} failed: {e}",
                retryable=True,
            )
            return False

    async def disconnect(self) -> None:
        """Send QUIT, cancel read loop, close streams."""
        if self._writer is not None and not self._writer.is_closing():
            try:
                await self._send_raw("QUIT :OpenComputer disconnecting")
            except Exception:  # noqa: BLE001
                pass
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
        self._connected = False

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send PRIVMSG. ``chat_id`` is the channel name (``#foo``) or nick.

        Long messages are split on newlines + chunked to <= max_message_length
        per IRC line (RFC 512-byte limit minus prefix overhead).
        """
        if not self._connected or self._writer is None:
            return SendResult(success=False, error="not connected")

        sent = 0
        for line in text.splitlines() or [text]:
            for chunk in _chunk_text(line, self.max_message_length):
                await self._send_raw(f"PRIVMSG {chat_id} :{chunk}")
                sent += 1
        return SendResult(success=True, message_id=str(sent))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _send_raw(self, line: str) -> None:
        """Send one IRC protocol line, appending CRLF."""
        if self._writer is None:
            raise RuntimeError("not connected")
        self._writer.write(f"{line}\r\n".encode())
        await self._writer.drain()

    async def _read_loop(self) -> None:
        """Read loop — parse incoming lines, dispatch PRIVMSG to handle_message,
        respond to server PING with PONG."""
        if self._reader is None:
            return
        try:
            while not self._reader.at_eof():
                raw = await self._reader.readline()
                if not raw:
                    break
                try:
                    line = raw.decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                prefix, command, params = _parse_irc_line(line)
                if command == "PING" and params:
                    await self._send_raw(f"PONG :{params[0]}")
                    continue
                if command == "PRIVMSG" and len(params) >= 2:
                    target, body = params[0], params[1]
                    sender = _nick_from_prefix(prefix)
                    # If target is our nick, treat sender as chat_id (DM);
                    # otherwise the channel is the chat
                    chat_id = target if target.startswith("#") else sender
                    event = MessageEvent(
                        platform=Platform.IRC,
                        chat_id=chat_id,
                        user_id=sender,
                        text=body,
                        message_id=None,
                    )
                    await self.handle_message(event)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            self._set_fatal_error(
                code="read_loop_error",
                message=f"IRC read loop crashed: {e}",
                retryable=True,
            )


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split text into <=limit-char chunks at word boundaries when possible."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= limit:
            out.append(text)
            break
        # Try to break at last space within limit
        cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return out
