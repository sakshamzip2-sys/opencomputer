"""EmailAdapter — IMAP poller + SMTP sender (G.14 / Tier 2.7).

Inbound: polls IMAP at ``poll_interval_seconds`` for unseen messages,
marks them seen, parses body to plain text, emits ``MessageEvent``.

Outbound: sends a reply via SMTP to the original ``From:`` (or to any
chat_id which is treated as a target email address).

Stdlib only — ``imaplib`` + ``smtplib`` + ``email`` modules — wrapped in
``asyncio.to_thread`` so they don't block the gateway loop. Avoids
adding ``aiosmtplib`` / ``aioimaplib`` deps.

Config keys (passed through ``EmailAdapter({...})``):

- ``imap_host`` / ``imap_port`` (default 993)
- ``smtp_host`` / ``smtp_port`` (default 465)
- ``username`` / ``password`` — same for IMAP and SMTP
- ``from_address`` — the address replies appear from (defaults to ``username``)
- ``poll_interval_seconds`` (default 60)
- ``mailbox`` (default ``"INBOX"``)
- ``allowed_senders`` — optional list of email addresses; messages from
  unrecognised senders are ignored (security guard against random spam
  triggering the agent).

For Gmail: enable IMAP, create an App Password at
``https://myaccount.google.com/apppasswords`` and use that as ``password``.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import smtplib
import time
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult

logger = logging.getLogger("opencomputer.ext.email")


_DEFAULT_POLL_INTERVAL = 60.0
_DEFAULT_IMAP_PORT = 993
_DEFAULT_SMTP_PORT = 465


class EmailAdapter(BaseChannelAdapter):
    """Email channel — IMAP poll + SMTP send."""

    platform = Platform.WEB  # No EMAIL enum yet; reuse WEB
    max_message_length = 64_000  # Email bodies are generous
    capabilities = ChannelCapabilities.NONE  # No typing / reactions / edit on email

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._imap_host = config["imap_host"]
        self._imap_port = int(config.get("imap_port", _DEFAULT_IMAP_PORT))
        self._smtp_host = config.get("smtp_host", config["imap_host"])
        self._smtp_port = int(config.get("smtp_port", _DEFAULT_SMTP_PORT))
        self._username = config["username"]
        self._password = config["password"]
        self._from_address = config.get("from_address", self._username)
        self._poll_interval = float(config.get("poll_interval_seconds", _DEFAULT_POLL_INTERVAL))
        self._mailbox = config.get("mailbox", "INBOX")
        self._allowed_senders: set[str] = {
            s.strip().lower() for s in (config.get("allowed_senders") or [])
        }
        self._stop_event = asyncio.Event()
        self._poll_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        # Verify IMAP creds with a one-shot login. SMTP creds are checked
        # lazily on first send so a temporary outbound outage doesn't
        # block the gateway from accepting inbound.
        try:
            await asyncio.to_thread(self._test_imap_login)
        except Exception as exc:  # noqa: BLE001
            logger.error("email IMAP login failed: %s", exc)
            return False
        self._poll_task = asyncio.create_task(self._poll_forever())
        logger.info(
            "email: connected to %s:%d (mailbox=%s, poll=%ds)",
            self._imap_host, self._imap_port, self._mailbox, self._poll_interval,
        )
        return True

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # Outbound — SMTP
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send an email. ``chat_id`` is the recipient address.

        ``kwargs`` may include ``subject`` (default ``"Re: OpenComputer"``)
        and ``in_reply_to`` (Message-ID header for threading).
        """
        if not chat_id or "@" not in chat_id:
            return SendResult(success=False, error=f"invalid email address: {chat_id!r}")

        subject = str(kwargs.get("subject") or "Re: OpenComputer")
        in_reply_to = kwargs.get("in_reply_to")

        msg = EmailMessage()
        msg["From"] = self._from_address
        msg["To"] = chat_id
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = str(in_reply_to)
            msg["References"] = str(in_reply_to)
        msg.set_content(text)

        try:
            await asyncio.to_thread(self._smtp_send, msg)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")
        return SendResult(success=True)

    # ------------------------------------------------------------------
    # Inbound — IMAP polling loop
    # ------------------------------------------------------------------

    async def _poll_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                events = await asyncio.to_thread(self._poll_once)
            except Exception as exc:  # noqa: BLE001
                logger.warning("email IMAP poll failed: %s", exc)
                events = []
            for event in events:
                try:
                    await self.handle_message(event)
                except Exception:  # noqa: BLE001
                    logger.exception("email handle_message failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
                return  # stop_event fired
            except TimeoutError:
                continue  # tick again

    # ------------------------------------------------------------------
    # Stdlib IMAP / SMTP helpers (run in asyncio.to_thread)
    # ------------------------------------------------------------------

    def _test_imap_login(self) -> None:
        with imaplib.IMAP4_SSL(self._imap_host, self._imap_port) as conn:
            conn.login(self._username, self._password)

    def _poll_once(self) -> list[MessageEvent]:
        """Connect, fetch UNSEEN messages, mark them \\Seen, return events."""
        events: list[MessageEvent] = []
        with imaplib.IMAP4_SSL(self._imap_host, self._imap_port) as conn:
            conn.login(self._username, self._password)
            conn.select(self._mailbox)
            typ, raw = conn.search(None, "UNSEEN")
            if typ != "OK" or not raw or not raw[0]:
                return events
            for uid in raw[0].split():
                event = self._fetch_one(conn, uid)
                if event is not None:
                    events.append(event)
                # Mark seen even if we couldn't parse — avoid replay loops.
                conn.store(uid, "+FLAGS", "\\Seen")
        return events

    def _fetch_one(self, conn: imaplib.IMAP4_SSL, uid: bytes) -> MessageEvent | None:
        typ, msg_data = conn.fetch(uid, "(RFC822)")
        if typ != "OK" or not msg_data:
            return None
        # msg_data is [(b"<id> (RFC822 {n}", b"<raw>"), b")"]; first tuple has bytes
        for part in msg_data:
            if isinstance(part, tuple) and len(part) >= 2:
                raw_bytes = part[1]
                break
        else:
            return None
        msg = email.message_from_bytes(raw_bytes)
        return self._email_to_event(msg)

    def _email_to_event(self, msg: email.message.Message) -> MessageEvent | None:
        from_header = msg.get("From", "")
        from_name, from_addr = parseaddr(from_header)
        if not from_addr:
            return None
        from_addr = from_addr.lower()
        if self._allowed_senders and from_addr not in self._allowed_senders:
            logger.info("email: ignoring message from %s (not in allowed_senders)", from_addr)
            return None

        subject = self._decode_header_safe(msg.get("Subject", ""))
        message_id = msg.get("Message-ID", "")
        body = self._extract_body(msg)
        if not body and not subject:
            return None
        text = subject + ("\n\n" + body if body else "")

        return MessageEvent(
            platform=Platform.WEB,
            chat_id=from_addr,
            user_id=from_addr,
            text=text,
            timestamp=time.time(),
            metadata={
                "email_subject": subject,
                "email_from_name": from_name,
                "email_message_id": message_id,
            },
        )

    @staticmethod
    def _decode_header_safe(raw: str) -> str:
        if not raw:
            return ""
        try:
            return str(make_header(decode_header(raw)))
        except Exception:  # noqa: BLE001
            return raw

    @staticmethod
    def _extract_body(msg: email.message.Message) -> str:
        """Pull the plain-text body out of a (potentially multipart) message.

        Prefers ``text/plain``; falls back to ``text/html`` with HTML tags
        stripped via the stdlib ``html.parser`` if no plaintext part exists.
        """
        plain_parts: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk() if msg.is_multipart() else [msg]:
            ctype = part.get_content_type()
            charset = part.get_content_charset() or "utf-8"
            try:
                payload = part.get_payload(decode=True)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(payload, bytes):
                continue
            try:
                decoded = payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                decoded = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain_parts.append(decoded)
            elif ctype == "text/html":
                html_parts.append(decoded)

        if plain_parts:
            return "\n".join(plain_parts).strip()
        if html_parts:
            return _strip_html(html_parts[0]).strip()
        return ""

    def _smtp_send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP_SSL(self._smtp_host, self._smtp_port) as conn:
            conn.login(self._username, self._password)
            conn.send_message(msg)


# ---------------------------------------------------------------------------
# HTML stripping (stdlib only — keeps deps tiny)
# ---------------------------------------------------------------------------


def _strip_html(html_text: str) -> str:
    """Quick-and-dirty HTML → text (good enough for forwarded articles)."""
    from html.parser import HTMLParser

    class _Stripper(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

        def handle_starttag(self, tag: str, attrs: list) -> None:
            if tag in ("br", "p", "div", "li"):
                self.parts.append("\n")

    s = _Stripper()
    s.feed(html_text)
    out = "".join(s.parts)
    # Collapse runs of whitespace
    return "\n".join(line.strip() for line in out.splitlines() if line.strip())


__all__ = ["EmailAdapter", "_strip_html"]
