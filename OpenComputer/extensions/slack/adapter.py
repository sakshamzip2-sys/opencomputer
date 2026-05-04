"""SlackAdapter — Slack channel via the Web API (G.17 / Tier 2.12).

Outbound + reactions / edit / delete via raw httpx calls to the Slack
Web API. Inbound is intentionally NOT in this adapter — Slack inbound
requires Socket Mode (a heavyweight WebSocket client) or a public URL
for the Events API. Users wanting inbound should:

1. Set up Slack Outgoing Webhooks pointing at an OC webhook token (G.3).
2. The webhook adapter receives the POST, dispatches to the agent.
3. Agent's response goes back via this adapter's ``send``.

This keeps Slack support lightweight (no extra deps, no Socket Mode
runtime) while still enabling the most common use case: "agent posts
to a Slack channel".

Capabilities: REACTIONS, EDIT_MESSAGE, DELETE_MESSAGE.

Setup:

1. Create a Slack app at https://api.slack.com/apps.
2. Add Bot Token Scopes: ``chat:write``, ``reactions:write``, ``chat:write.public``.
3. Install to workspace, copy the Bot User OAuth Token (starts ``xoxb-``).
4. Set ``SLACK_BOT_TOKEN`` in OC's environment.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, ProcessingOutcome, SendResult
from plugin_sdk.format_converters import slack_mrkdwn

logger = logging.getLogger("opencomputer.ext.slack")


_SLACK_API_BASE = "https://slack.com/api"

# PR #221 follow-up Item 3 — ConsentGate inline-button approval format.
# Mirrors the Telegram wire format so :class:`Dispatch._handle_approval_click`
# can route Slack clicks through the same ``(verb, token)`` shape.
_APPROVAL_VALUE_PREFIX = "oc:approve:"

# Bound the seen-action set so a long-running adapter doesn't accumulate
# unbounded state. Insertion-ordered eviction is fine — we only need to
# remember "very recent action_ids".
_CALLBACK_DEDUPE_CAPACITY = 1024

# Slack's documented signature replay window is 5 minutes. Clients
# outside this window are rejected outright.
_SIGNATURE_MAX_AGE_S = 60 * 5

# PR 4.6 — default "agent is thinking" status string surfaced via
# ``assistant.threads.setStatus``. Cleared (empty string) on
# ``on_processing_complete`` so a stale "thinking…" indicator never
# survives an agent run. When ConsentGate prompts the user via Slack,
# the dispatch code calls :meth:`pause_typing_status` to clear the
# indicator while we wait for a button click — otherwise users see a
# typing indicator that lies (the agent is blocked, not thinking).
_DEFAULT_THINKING_STATUS = "Thinking…"


class SlackAdapter(BaseChannelAdapter):
    """Slack channel — Web API only (no Socket Mode runtime)."""

    platform = Platform.SLACK
    max_message_length = 40_000  # Slack's per-block_text limit; chat.postMessage allows up to ~40k
    capabilities = (
        ChannelCapabilities.REACTIONS
        | ChannelCapabilities.EDIT_MESSAGE
        | ChannelCapabilities.DELETE_MESSAGE
        | ChannelCapabilities.THREADS
    )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        # OpenClaw 1.A wiring (ship-now Sub-project C) — streaming chunker.
        # 1100 ms floor on human_delay_min_ms is Slack's safe rate
        # (~1 message/sec on chat.postMessage tier).
        streaming = config.get("streaming") or {}
        self.streaming_block_chunker: bool = bool(streaming.get("block_chunker", False))
        self.streaming_min_chars: int = int(streaming.get("min_chars", 80))
        self.streaming_max_chars: int = int(streaming.get("max_chars", 1500))
        self.streaming_human_delay_min_ms: int = int(
            streaming.get("human_delay_min_ms", 1100)
        )
        self.streaming_human_delay_max_ms: int = int(
            streaming.get("human_delay_max_ms", 2500)
        )
        self._token = config["bot_token"]
        self._client: httpx.AsyncClient | None = None
        # PR 4.6 — track per-thread typing status so we can restore it
        # after a ConsentGate prompt resolves. Maps channel:thread_ts
        # → last set status string (or "" for cleared). Bounded by
        # the number of concurrent active threads.
        self._typing_status: dict[str, str] = {}

        # PR #221 follow-up Item 3 — ConsentGate inline-button surface.
        # ``_approval_callback`` is the function the gateway / agent loop
        # registers via :meth:`set_approval_callback` to receive button
        # clicks. The adapter intentionally doesn't import ConsentGate —
        # it routes raw ``(verb, token)`` tuples and lets the gateway
        # translate to (session_id, capability_id, decision, persist).
        self._approval_callback: (
            Callable[[str, str], Awaitable[None]] | None
        ) = None
        # ``_approval_tokens`` maps the opaque request_token we sent in
        # button.value back to the chat_id + ts of the buttons message
        # so the inbound handler can edit the original to remove the
        # buttons + show the resolution.
        self._approval_tokens: dict[str, dict[str, Any]] = {}
        # Bounded dedupe set keyed on action_id — absorbs Slack retry
        # deliveries (Slack retries unacknowledged interactivity for
        # ~30s). Insertion-order eviction keeps the working set small.
        self._seen_action_ids: OrderedDict[str, None] = OrderedDict()

        # Optional aiohttp interactivity server. Enabled when
        # ``interactivity_port > 0`` AND ``signing_secret`` is set; the
        # signing secret is used to verify Slack's
        # ``X-Slack-Signature`` header on inbound clicks.
        self._signing_secret: str = str(config.get("signing_secret") or "")
        self._interactivity_port: int = int(
            config.get("interactivity_port") or 0
        )
        self._interactivity_path: str = str(
            config.get("interactivity_path") or "/slack/interactive"
        )
        self._interactivity_host: str = str(
            config.get("interactivity_host") or "0.0.0.0"
        )
        self._interactivity_runner: Any = None  # aiohttp.web.AppRunner

    async def connect(self) -> bool:
        """Connect = verify the bot token is valid via auth.test.

        PR #221 follow-up Item 3 — when ``interactivity_port > 0`` AND a
        ``signing_secret`` is configured, also spawns an aiohttp HTTP
        server on the configured port + path so ConsentGate inline
        button clicks (Slack interactivity payloads) can reach the
        adapter. Disabled by default — requires explicit opt-in via
        config to avoid surprising users with an open port.
        """
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            resp = await self._client.post(f"{_SLACK_API_BASE}/auth.test")
            data = resp.json()
            if not data.get("ok"):
                logger.error("slack auth.test failed: %s", data.get("error"))
                return False
            logger.info(
                "slack: connected as %s in workspace %s",
                data.get("user"),
                data.get("team"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("slack connect failed: %s", exc)
            return False

        # Optionally start the interactivity HTTP server. Failure here
        # is logged + non-fatal — outbound continues to work without it.
        if self._interactivity_port > 0:
            if not self._signing_secret:
                logger.warning(
                    "slack: interactivity_port=%d but signing_secret unset — "
                    "skipping inbound server (signature verification "
                    "would always fail)",
                    self._interactivity_port,
                )
            else:
                try:
                    await self._start_interactivity_server()
                except Exception as exc:  # noqa: BLE001
                    logger.error(
                        "slack: interactivity server failed to start: %s",
                        exc,
                    )
        return True

    async def disconnect(self) -> None:
        if self._interactivity_runner is not None:
            try:
                await self._interactivity_runner.cleanup()
            except Exception:  # noqa: BLE001
                logger.debug("slack: interactivity runner cleanup failed",
                             exc_info=True)
            self._interactivity_runner = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send_multiple_images(
        self,
        chat_id: str,
        image_paths: list[str],
        caption: str = "",
        **kwargs: Any,
    ) -> None:
        """Upload N images to a Slack channel with one comment.

        Wave 5 T11 closure (Hermes-port 3de8e2168). Slack's native multi-file
        UX is a sequence of ``files.upload`` calls where only the first
        carries the ``initial_comment``; the others appear as a thread/burst
        in the channel UI. Per-file uploads also keep a single failure from
        losing the whole batch.

        Each image is sent via the legacy ``files.upload`` endpoint (still
        functional; the modern ``files.uploadV2`` two-step flow can replace
        this when needed). On per-file errors, the failure is logged and
        the loop continues to the next file — partial delivery beats none.
        """
        if not image_paths:
            return
        if self._client is None:
            return
        from pathlib import Path as _Path

        for i, raw in enumerate(image_paths):
            p = _Path(raw)
            if not p.exists() or not p.is_file():
                logger.warning("slack send_multiple_images: missing file %s", p)
                continue
            data: dict[str, Any] = {"channels": chat_id}
            if i == 0 and caption:
                data["initial_comment"] = caption
            data["filename"] = p.name
            try:
                with p.open("rb") as fh:
                    files = {"file": (p.name, fh, "application/octet-stream")}
                    # files.upload is multipart; httpx-side that means
                    # passing data + files separately.
                    resp = await self._client.post(
                        f"{_SLACK_API_BASE}/files.upload",
                        data=data,
                        files=files,
                    )
                if resp.status_code != 200:
                    logger.warning(
                        "slack files.upload(%s) HTTP %s", p.name, resp.status_code,
                    )
                    continue
                body = resp.json()
                if not body.get("ok"):
                    logger.warning(
                        "slack files.upload(%s) error: %s",
                        p.name, body.get("error"),
                    )
            except Exception as exc:  # noqa: BLE001 — partial delivery beats none
                logger.warning("slack files.upload(%s) raised: %s", p.name, exc)

    # ------------------------------------------------------------------
    # PR 4.6 — typing-status / pause-during-approval
    # ------------------------------------------------------------------

    @staticmethod
    def _typing_key(chat_id: str, thread_ts: str | None) -> str:
        return f"{chat_id}:{thread_ts or ''}"

    async def _set_typing_status(
        self,
        chat_id: str,
        status: str,
        thread_ts: str | None = None,
    ) -> None:
        """Best-effort ``assistant.threads.setStatus`` call.

        Slack only honours setStatus on assistant-thread channels (the
        AI-assistant tab); on regular channels the call returns
        ``not_in_channel`` / similar and we simply absorb the error.
        That's fine — the API is documented as a no-op outside
        assistant threads.

        ``status=""`` clears the indicator.
        """
        if self._client is None:
            return
        key = self._typing_key(chat_id, thread_ts)
        self._typing_status[key] = status
        payload: dict[str, Any] = {"channel_id": chat_id, "status": status}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            resp = await self._client.post(
                f"{_SLACK_API_BASE}/assistant.threads.setStatus",
                json=payload,
            )
            data = resp.json()
            if not data.get("ok"):
                # Common harmless errors when called outside an assistant
                # thread; log at DEBUG so production logs aren't flooded.
                logger.debug(
                    "slack assistant.threads.setStatus(%r) -> %s",
                    status, data.get("error"),
                )
        except Exception as exc:  # noqa: BLE001 — typing is decoration
            logger.debug("slack setStatus failed: %s", exc)

    async def pause_typing_status(
        self, chat_id: str, thread_ts: str | None = None
    ) -> None:
        """Clear the typing indicator.

        Called by ConsentGate / approval flows when we're about to
        prompt the user for input — a stale typing indicator while
        waiting for a button click misleads users into thinking the
        agent is still running. The previous status (if any) is
        preserved so :meth:`resume_typing_status` can restore it.
        """
        # Note: we deliberately don't read+restore the previous status
        # in the API — Slack has no getStatus. Caller decides what to
        # restore after resume.
        await self._set_typing_status(chat_id, "", thread_ts=thread_ts)

    async def resume_typing_status(
        self,
        chat_id: str,
        thread_ts: str | None = None,
        status: str = _DEFAULT_THINKING_STATUS,
    ) -> None:
        """Restore the typing indicator.

        Called once the approval flow resolves so the user knows the
        agent is back on the job. Defaults to "Thinking…" — caller can
        pass a custom status to convey progress (e.g. "Reading
        Confluence…").
        """
        await self._set_typing_status(chat_id, status, thread_ts=thread_ts)

    # ------------------------------------------------------------------
    # Lifecycle hooks — show "Thinking…" while the agent is running, clear on
    # complete. Override of BaseChannelAdapter so reactions don't double-set
    # status indicators.
    # ------------------------------------------------------------------

    async def on_processing_start(
        self, chat_id: str, message_id: str | None
    ) -> None:
        """Set ``Thinking…`` status. Overrides the base eye-reaction."""
        # ``message_id`` is the Slack ts of the inbound message; in
        # assistant-threads channels we treat it as thread_ts so the
        # status surfaces in the right thread.
        await self._set_typing_status(
            chat_id, _DEFAULT_THINKING_STATUS, thread_ts=message_id
        )

    async def on_processing_complete(
        self,
        chat_id: str,
        message_id: str | None,
        outcome: ProcessingOutcome,
    ) -> None:
        """Clear the typing status when the agent finishes (any outcome)."""
        del outcome  # status is binary — final state irrelevant here
        await self._set_typing_status(chat_id, "", thread_ts=message_id)

    # ------------------------------------------------------------------
    # PR #221 follow-up Item 3 — ConsentGate inline-button approval
    # ------------------------------------------------------------------

    def set_approval_callback(
        self, callback: Callable[[str, str], Awaitable[None]]
    ) -> None:
        """Register the coroutine that receives ``(verb, request_token)``
        clicks. ``verb`` is one of ``"once"``, ``"always"``, ``"deny"``;
        ``request_token`` is the opaque token the caller minted when it
        invoked :meth:`send_approval_request`. The gateway is responsible
        for translating those back into a ``ConsentGate.resolve_pending``
        call (it owns the session_id ↔ token map).

        Replaces any previously-registered callback.
        """
        self._approval_callback = callback

    async def send_approval_request(
        self,
        chat_id: str,
        prompt_text: str,
        request_token: str,
        **kwargs: Any,
    ) -> SendResult:
        """Post a Block Kit approval prompt with three action buttons.

        Mirrors :meth:`TelegramAdapter.send_approval_request`. The
        button ``value`` carries the ``"oc:approve:<verb>:<token>"``
        triple end-to-end so the inbound interactivity handler routes
        Slack clicks through the same shape Telegram uses (so
        :class:`Dispatch._handle_approval_click` doesn't need a
        platform-specific branch).

        ``prompt_text`` SHOULD be the result of
        ``ConsentGate.render_prompt(claim, scope)`` so the adapter
        doesn't introduce a parallel risk classifier.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")

        # PR 4.6 — pause the typing indicator while we wait on a click.
        # Best-effort; ``thread_ts`` may not be known here so we clear
        # the channel-level status. ``resume_typing_status`` is invoked
        # on resolution by ``_handle_interactivity``.
        try:
            await self.pause_typing_status(chat_id)
        except Exception:  # noqa: BLE001
            logger.debug("slack: pause_typing_status before approval failed",
                         exc_info=True)

        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": prompt_text},
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✓ Allow once"},
                        "value": f"{_APPROVAL_VALUE_PREFIX}once:{request_token}",
                        "action_id": f"oc_approve_once_{request_token}",
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✓ Allow always"},
                        "value": f"{_APPROVAL_VALUE_PREFIX}always:{request_token}",
                        "action_id": f"oc_approve_always_{request_token}",
                        "style": "primary",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✗ Deny"},
                        "value": f"{_APPROVAL_VALUE_PREFIX}deny:{request_token}",
                        "action_id": f"oc_approve_deny_{request_token}",
                        "style": "danger",
                    },
                ],
            },
        ]

        payload: dict[str, Any] = {
            "channel": chat_id,
            "text": prompt_text,  # fallback for clients that can't render blocks
            "blocks": blocks,
        }
        try:
            resp = await self._client.post(
                f"{_SLACK_API_BASE}/chat.postMessage", json=payload,
            )
            data = resp.json()
            if not data.get("ok"):
                return SendResult(
                    success=False, error=str(data.get("error") or data),
                )
            ts = str(data.get("ts") or "")
            self._approval_tokens[request_token] = {
                "chat_id": chat_id, "ts": ts,
            }
            return SendResult(success=True, message_id=ts)
        except Exception as exc:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # --- Signature verification + interactivity routing -----------------

    def _verify_signature(
        self, *, timestamp: str, body: bytes, signature: str
    ) -> bool:
        """Verify ``X-Slack-Signature`` per Slack's signing-secret algorithm.

        Spec: https://api.slack.com/authentication/verifying-requests-from-slack
        - basestring = ``v0:<timestamp>:<raw-request-body>``
        - HMAC-SHA256 with the signing secret
        - prefix the hex digest with ``v0=``
        - compare with ``X-Slack-Signature`` (constant-time).

        Replay window: reject requests older than
        :data:`_SIGNATURE_MAX_AGE_S` (5 minutes).
        """
        if not self._signing_secret or not timestamp or not signature:
            return False
        try:
            ts_int = int(timestamp)
        except (ValueError, TypeError):
            return False
        if abs(time.time() - ts_int) > _SIGNATURE_MAX_AGE_S:
            return False
        basestring = f"v0:{timestamp}:".encode() + body
        digest = hmac.new(
            self._signing_secret.encode(), basestring, hashlib.sha256,
        ).hexdigest()
        expected = f"v0={digest}"
        return hmac.compare_digest(expected, signature)

    async def _handle_interactivity(self, request: Any) -> Any:
        """aiohttp handler for Slack interactivity POSTs.

        Slack delivers button clicks as ``application/x-www-form-urlencoded``
        bodies with a single ``payload`` field carrying the JSON. We
        verify the signature, extract ``actions[0].value`` (the
        ``oc:approve:<verb>:<token>`` triple we minted), and dispatch
        through the registered approval callback. Returns a 200 with a
        Slack-formatted ``{"text": ...}`` body to replace the buttons
        with a confirmation line.
        """
        from aiohttp import web  # local import — aiohttp is heavy

        raw_body = await request.read()
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not self._verify_signature(
            timestamp=timestamp, body=raw_body, signature=signature,
        ):
            return web.Response(status=401, text="invalid signature")

        # Slack sends form-encoded data. Parse the ``payload`` field as JSON.
        try:
            form = await request.post()
            payload_raw = form.get("payload", "") if hasattr(form, "get") else ""
            if not payload_raw:
                # Fall back to manual parse for the non-multipart case.
                from urllib.parse import parse_qs as _parse_qs
                parsed = _parse_qs(raw_body.decode("utf-8", errors="replace"))
                payload_raw = parsed.get("payload", [""])[0]
            payload = json.loads(payload_raw) if payload_raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.Response(status=400, text="malformed payload")

        actions = payload.get("actions") or []
        if not actions:
            return web.Response(status=200, text="no action")
        action = actions[0]
        value = str(action.get("value") or "")
        action_id = str(action.get("action_id") or "")

        # action_id-level dedupe — Slack retries unacked deliveries.
        if action_id in self._seen_action_ids:
            return web.Response(status=200, text="duplicate")
        self._seen_action_ids[action_id] = None
        while len(self._seen_action_ids) > _CALLBACK_DEDUPE_CAPACITY:
            self._seen_action_ids.popitem(last=False)

        if not value.startswith(_APPROVAL_VALUE_PREFIX):
            return web.Response(status=200, text="not approval")
        rest = value[len(_APPROVAL_VALUE_PREFIX):]
        try:
            verb, token = rest.split(":", 1)
        except ValueError:
            logger.warning("slack approval value malformed: %r", value)
            return web.Response(status=200, text="malformed value")

        # Token-level dedupe — once a verb has been processed for a
        # token, subsequent clicks (even with new action_ids) are
        # dropped. We pop on first dispatch.
        token_meta = self._approval_tokens.pop(token, None)
        if token_meta is None:
            logger.info(
                "slack approval click for unknown token=%s — stale, ignored",
                token,
            )
            return web.Response(
                status=200,
                content_type="application/json",
                text=json.dumps(
                    {"text": "Decision already recorded.", "replace_original": True},
                ),
            )

        if self._approval_callback is None:
            logger.warning(
                "slack approval click for token=%s but no callback registered",
                token,
            )
            # Re-register the token so a future callback registration could
            # resolve it.
            self._approval_tokens[token] = token_meta
            return web.Response(status=200, text="no callback")

        try:
            await self._approval_callback(verb, token)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "slack approval callback raised for verb=%s token=%s: %s",
                verb, token, exc,
            )
            self._approval_tokens[token] = token_meta
            return web.Response(status=200, text="callback failed")

        # Best-effort: resume the typing indicator now that the user
        # has decided (the agent is back to working).
        try:
            await self.resume_typing_status(token_meta["chat_id"])
        except Exception:  # noqa: BLE001
            logger.debug("slack: resume_typing_status after approval failed",
                         exc_info=True)

        label = {
            "once": "✓ Allowed once",
            "always": "✓ Allowed always",
            "deny": "✗ Denied",
        }.get(verb, verb)
        return web.Response(
            status=200,
            content_type="application/json",
            text=json.dumps(
                {"text": f"Decision recorded: {label}", "replace_original": True},
            ),
        )

    async def _start_interactivity_server(self) -> None:
        """Spawn the aiohttp interactivity server. Called from ``connect``."""
        from aiohttp import web  # local import — aiohttp is heavy

        app = web.Application()
        app.router.add_post(self._interactivity_path, self._handle_interactivity)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner, self._interactivity_host, self._interactivity_port,
        )
        await site.start()
        self._interactivity_runner = runner
        logger.info(
            "slack: interactivity server listening on %s:%d%s",
            self._interactivity_host,
            self._interactivity_port,
            self._interactivity_path,
        )

    # ------------------------------------------------------------------
    # Format-message — markdown → Slack mrkdwn (PR 3b.2)
    # ------------------------------------------------------------------

    def format_message(self, text: str) -> str:
        """Convert generic markdown into Slack mrkdwn.

        ``**bold**`` → ``*bold*``, ``[label](url)`` → ``<url|label>``,
        code fences preserved, etc. The converter falls back to plain
        text on parse error so a malformed input never crashes send.
        """
        return slack_mrkdwn.convert(text or "")

    # ------------------------------------------------------------------
    # Outbound — chat.postMessage
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send a message to a channel id (``C…``) or DM id (``D…``).

        ``kwargs`` may include:
        - ``thread_ts``: post as a threaded reply.
        - ``broadcast``: when threading, also broadcast to channel.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        formatted = self.format_message(text or "")
        payload: dict[str, Any] = {
            "channel": chat_id,
            "text": formatted[: self.max_message_length],
        }
        if kwargs.get("thread_ts"):
            payload["thread_ts"] = kwargs["thread_ts"]
            if kwargs.get("broadcast"):
                payload["reply_broadcast"] = True

        async def _do_send() -> SendResult:
            try:
                resp = await self._client.post(
                    f"{_SLACK_API_BASE}/chat.postMessage",
                    json=payload,
                )
                data = resp.json()
                if not data.get("ok"):
                    return SendResult(
                        success=False, error=str(data.get("error") or data)
                    )
                return SendResult(success=True, message_id=str(data.get("ts") or ""))
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_send)

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    async def send_reaction(
        self, chat_id: str, message_id: str, emoji: str, **kwargs: Any
    ) -> SendResult:
        """Add an emoji reaction. Slack expects emoji NAMES (e.g. ``thumbsup``)
        not unicode codepoints — caller can pass either; we map common ones.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        name = _emoji_to_slack_name(emoji)

        async def _do_react() -> SendResult:
            try:
                resp = await self._client.post(
                    f"{_SLACK_API_BASE}/reactions.add",
                    json={"channel": chat_id, "timestamp": message_id, "name": name},
                )
                data = resp.json()
                if not data.get("ok"):
                    # already_reacted is harmless idempotent; surface as success
                    if data.get("error") == "already_reacted":
                        return SendResult(success=True)
                    return SendResult(
                        success=False, error=str(data.get("error") or data)
                    )
                return SendResult(success=True)
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_react)

    # ------------------------------------------------------------------
    # Edit / Delete
    # ------------------------------------------------------------------

    async def edit_message(
        self, chat_id: str, message_id: str, text: str, **kwargs: Any
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        formatted = self.format_message(text or "")

        async def _do_edit() -> SendResult:
            try:
                resp = await self._client.post(
                    f"{_SLACK_API_BASE}/chat.update",
                    json={
                        "channel": chat_id,
                        "ts": message_id,
                        "text": formatted[: self.max_message_length],
                    },
                )
                data = resp.json()
                if not data.get("ok"):
                    return SendResult(
                        success=False, error=str(data.get("error") or data)
                    )
                return SendResult(success=True, message_id=str(data.get("ts") or ""))
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_edit)

    async def delete_message(
        self, chat_id: str, message_id: str, **kwargs: Any
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")

        async def _do_delete() -> SendResult:
            try:
                resp = await self._client.post(
                    f"{_SLACK_API_BASE}/chat.delete",
                    json={"channel": chat_id, "ts": message_id},
                )
                data = resp.json()
                if not data.get("ok"):
                    return SendResult(
                        success=False, error=str(data.get("error") or data)
                    )
                return SendResult(success=True)
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_delete)


# ---------------------------------------------------------------------------
# Emoji → Slack reaction name map
#
# Slack reactions use short-codes (``:thumbsup:``) rather than unicode emoji.
# Most callers will pass unicode (``"👍"``) so we map common ones; users can
# also pass the bare name (``"thumbsup"``) and we'll pass it through.
# ---------------------------------------------------------------------------

_EMOJI_TO_SLACK_NAME = {
    "👍": "thumbsup",
    "👎": "thumbsdown",
    "❤️": "heart",
    "❤": "heart",
    "🎉": "tada",
    "🔥": "fire",
    "👀": "eyes",
    "✅": "white_check_mark",
    "❌": "x",
    "⚠️": "warning",
    "⚠": "warning",
    "🚀": "rocket",
    "💯": "100",
    "😂": "joy",
    "🤔": "thinking_face",
    "👏": "clap",
}


def _emoji_to_slack_name(emoji_or_name: str) -> str:
    """Map a unicode emoji to its Slack reaction name. Bare-name input is passed through."""
    if not emoji_or_name:
        return ""
    # Already a slack name (no special chars)?
    if all(ch.isalnum() or ch in {"_", "-", "+"} for ch in emoji_or_name):
        return emoji_or_name.lower()
    return _EMOJI_TO_SLACK_NAME.get(emoji_or_name, emoji_or_name)


__all__ = ["SlackAdapter", "_emoji_to_slack_name"]
