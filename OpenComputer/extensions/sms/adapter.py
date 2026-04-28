"""SmsAdapter — Twilio SMS channel adapter.

Ported from hermes-agent (MIT) gateway/platforms/sms.py 2026-04-28
following docs/superpowers/specs/2026-04-27-platform-reach-port-guide.md.

Inbound: aiohttp webhook server receives Twilio POSTs, validates the
``X-Twilio-Signature`` header (HMAC-SHA1 over URL + sorted form params),
dispatches as ``MessageEvent``.

Outbound: Twilio REST API (``Messages.json``) via HTTP Basic auth.

What we ship vs hermes:
- Trim: hermes phone-number redaction utility, markdown stripping helper,
  background-task tracker — replaced with inline equivalents.
- Keep: signature validation (security-critical), port-variant URL
  fallback (Twilio sometimes signs with default port included).
- Skip: ``SMS_HOME_CHANNEL`` (cron delivery hook — not part of channel
  contract; handle in cron config).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Any

import aiohttp
from aiohttp import web

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.channel_helpers import redact_phone, strip_markdown
from plugin_sdk.core import MessageEvent, Platform, SendResult

logger = logging.getLogger("opencomputer.ext.sms")

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"
MAX_SMS_LENGTH = 1600  # ~10 SMS segments
DEFAULT_WEBHOOK_PORT = 8080
DEFAULT_WEBHOOK_HOST = "0.0.0.0"


# PR 3c.3 — markdown stripping + phone redaction now come from
# ``plugin_sdk.channel_helpers`` so every channel adapter shares the
# same implementation. The local copies that lived here previously
# (`_strip_markdown` / `_redact_phone`) had subtle behavioural
# differences (the local redactor kept only 2 trailing digits and
# didn't preserve country-code separation). Re-exported below as
# private aliases for any downstream caller (tests, plugins) that
# imported them by name from this module.
_strip_markdown = strip_markdown
_redact_phone = redact_phone


class SmsAdapter(BaseChannelAdapter):
    """Twilio SMS adapter.

    Each inbound phone number maps to a chat_id (the E.164 number itself).
    Replies go from the configured ``TWILIO_PHONE_NUMBER``.
    """

    platform = Platform.SMS
    max_message_length = MAX_SMS_LENGTH
    capabilities = ChannelCapabilities.NONE  # SMS = text only

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._account_sid: str = config["account_sid"]
        self._auth_token: str = config["auth_token"]
        self._from_number: str = config.get("from_number", "")
        self._webhook_port: int = int(config.get("webhook_port", DEFAULT_WEBHOOK_PORT))
        self._webhook_host: str = config.get("webhook_host", DEFAULT_WEBHOOK_HOST)
        self._webhook_url: str = config.get("webhook_url", "").strip()
        self._insecure_no_signature: bool = bool(
            config.get("insecure_no_signature", False)
        )
        self._runner: web.AppRunner | None = None
        self._http_session: aiohttp.ClientSession | None = None
        self._running: bool = False

    def _basic_auth_header(self) -> str:
        creds = f"{self._account_sid}:{self._auth_token}"
        return "Basic " + base64.b64encode(creds.encode("ascii")).decode("ascii")

    # ── BaseChannelAdapter API ────────────────────────────────────────

    async def connect(self) -> bool:
        if not self._from_number:
            logger.error("TWILIO_PHONE_NUMBER not set — cannot send replies")
            return False

        if not self._webhook_url and not self._insecure_no_signature:
            logger.error(
                "Refusing to start: SMS_WEBHOOK_URL is required for Twilio "
                "signature validation. Set it to the public URL configured "
                "in your Twilio console (e.g. https://example.com/webhooks/twilio). "
                "For local development without validation, set "
                "SMS_INSECURE_NO_SIGNATURE=true (not recommended)."
            )
            return False

        if self._insecure_no_signature and not self._webhook_url:
            logger.warning(
                "SMS_INSECURE_NO_SIGNATURE=true — Twilio signature validation "
                "is DISABLED. Any client that can reach port %d can inject "
                "messages. DO NOT use this in production.",
                self._webhook_port,
            )

        app = web.Application()
        app.router.add_post("/webhooks/twilio", self._handle_webhook)
        app.router.add_get("/health", lambda _: web.Response(text="ok"))

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._webhook_host, self._webhook_port)
        await site.start()
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._running = True

        logger.info(
            "Twilio webhook server listening on %s:%d, from=%s",
            self._webhook_host,
            self._webhook_port,
            redact_phone(self._from_number),
        )
        return True

    async def disconnect(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        self._running = False
        logger.info("Disconnected")

    async def send(
        self, chat_id: str, text: str, **kwargs: Any
    ) -> SendResult:
        formatted = strip_markdown(text)
        chunks = self._chunk_for_sms(formatted)
        last_result = SendResult(success=True)

        url = f"{TWILIO_API_BASE}/{self._account_sid}/Messages.json"
        headers = {"Authorization": self._basic_auth_header()}

        owns_session = self._http_session is None
        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
        )
        try:
            for chunk in chunks:
                # PR #221 O2 — wrap the Twilio Messages.json POST with the
                # base adapter's transient-error retry helper. We need a
                # coroutine wrapper because aiohttp's ``session.post(...)``
                # returns a context-manager rather than a plain awaitable;
                # the helper just calls ``await fn(...)`` and trips the
                # retry path on retryable exceptions raised inside.
                async def _do_post(form_payload: aiohttp.FormData) -> SendResult:
                    async with session.post(
                        url, data=form_payload, headers=headers
                    ) as resp:
                        body = await resp.json()
                        if resp.status >= 400:
                            error_msg = body.get("message", str(body))
                            logger.error(
                                "send failed to %s: %d %s",
                                redact_phone(chat_id),
                                resp.status,
                                error_msg,
                            )
                            return SendResult(
                                success=False,
                                error=f"Twilio {resp.status}: {error_msg}",
                            )
                        return SendResult(
                            success=True, message_id=body.get("sid", "")
                        )

                form_data = aiohttp.FormData()
                form_data.add_field("From", self._from_number)
                form_data.add_field("To", chat_id)
                form_data.add_field("Body", chunk)
                try:
                    chunk_result = await self._send_with_retry(
                        _do_post, form_data
                    )
                except Exception as exc:  # noqa: BLE001 — non-retryable propagate here
                    logger.error("send error to %s: %s", redact_phone(chat_id), exc)
                    return SendResult(success=False, error=str(exc))
                if not chunk_result.success:
                    return chunk_result
                last_result = chunk_result
        finally:
            if owns_session and session is not None:
                await session.close()

        return last_result

    # ── SMS-specific helpers ──────────────────────────────────────────

    def _chunk_for_sms(self, text: str, limit: int = MAX_SMS_LENGTH) -> list[str]:
        """Split ``text`` into SMS-friendly chunks. Single SMS = 160 chars
        but Twilio auto-concatenates up to ``limit`` chars. Beyond that we
        chunk. Prefer line breaks so multi-message replies don't split
        sentences mid-word."""
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for line in text.splitlines(keepends=True):
            ll = len(line)
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

    # ── Twilio signature validation ───────────────────────────────────

    def _validate_twilio_signature(
        self, url: str, post_params: dict[str, str], signature: str
    ) -> bool:
        """Validate the X-Twilio-Signature header.

        Algorithm: https://www.twilio.com/docs/usage/security#validating-requests
        Try the URL both with and without the default port — Twilio
        sometimes signs with the explicit default port.
        """
        if self._check_signature(url, post_params, signature):
            return True
        variant = self._port_variant_url(url)
        return bool(variant and self._check_signature(variant, post_params, signature))

    def _check_signature(
        self, url: str, post_params: dict[str, str], signature: str
    ) -> bool:
        data_to_sign = url
        for key in sorted(post_params.keys()):
            data_to_sign += key + post_params[key]
        mac = hmac.new(
            self._auth_token.encode("utf-8"),
            data_to_sign.encode("utf-8"),
            hashlib.sha1,
        )
        computed = base64.b64encode(mac.digest()).decode("utf-8")
        return hmac.compare_digest(computed, signature)

    @staticmethod
    def _port_variant_url(url: str) -> str | None:
        parsed = urllib.parse.urlparse(url)
        default_ports = {"https": 443, "http": 80}
        default_port = default_ports.get(parsed.scheme)
        if default_port is None:
            return None
        if parsed.port == default_port:
            # Strip explicit default port
            return urllib.parse.urlunparse(
                (parsed.scheme, parsed.hostname or "", parsed.path,
                 parsed.params, parsed.query, parsed.fragment)
            )
        if parsed.port is None:
            netloc = f"{parsed.hostname}:{default_port}"
            return urllib.parse.urlunparse(
                (parsed.scheme, netloc, parsed.path,
                 parsed.params, parsed.query, parsed.fragment)
            )
        return None  # non-standard port — no variant

    # ── Webhook handler ───────────────────────────────────────────────

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        empty_twiml = (
            '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
        )

        try:
            raw = await request.read()
            form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("webhook parse error: %s", exc)
            return web.Response(text=empty_twiml, content_type="application/xml", status=400)

        # Validate Twilio signature when SMS_WEBHOOK_URL is set
        if self._webhook_url:
            sig = request.headers.get("X-Twilio-Signature", "")
            if not sig:
                logger.warning("Rejected: missing X-Twilio-Signature header")
                return web.Response(
                    text=empty_twiml, content_type="application/xml", status=403
                )
            flat_params = {k: v[0] for k, v in form.items() if v}
            if not self._validate_twilio_signature(
                self._webhook_url, flat_params, sig
            ):
                logger.warning("Rejected: invalid Twilio signature")
                return web.Response(
                    text=empty_twiml, content_type="application/xml", status=403
                )

        from_number = form.get("From", [""])[0].strip()
        text = form.get("Body", [""])[0].strip()
        message_sid = form.get("MessageSid", [""])[0].strip()

        if not from_number or not text:
            return web.Response(text=empty_twiml, content_type="application/xml")

        # Echo prevention: ignore messages from our own number
        if from_number == self._from_number:
            return web.Response(text=empty_twiml, content_type="application/xml")

        logger.info(
            "inbound from=%s text=%r",
            redact_phone(from_number),
            text[:80],
        )

        event = MessageEvent(
            platform=Platform.SMS,
            chat_id=from_number,  # the sender's E.164
            user_id=from_number,
            text=text,
            timestamp=time.time(),
            metadata={"message_sid": message_sid},
        )
        # Dispatch in the background — Twilio expects a fast response
        import asyncio
        asyncio.create_task(self.handle_message(event))

        return web.Response(text=empty_twiml, content_type="application/xml")
