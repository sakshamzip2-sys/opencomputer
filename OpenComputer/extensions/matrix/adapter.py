"""MatrixAdapter — Matrix channel via the Client-Server API (G.19 + Wave 6.E.3).

Outbound + reactions + edit (m.replace) + redaction (delete) via raw httpx
calls to the Matrix Client-Server API ``/_matrix/client/v3/...``.
**No end-to-end encryption** — works only in unencrypted rooms. Adding E2E
support would require ``matrix-nio`` + olm/megolm libs, deferred until
demand.

Wave 6.E.3 (2026-05-04): adds **inbound /sync long-poll** so the adapter
sees ``m.reaction`` events on its own messages. Combined with
:mod:`extensions.matrix.approval`, this gives OC a reaction-based
approval primitive — post a "want to run X?" message, await a ✅/❌.

Setup:

1. Get an access token from your homeserver (e.g. via Element → Help &
   About → Advanced → Access Token, or via ``POST /_matrix/client/v3/login``).
2. Set ``MATRIX_HOMESERVER`` (e.g. ``https://matrix.org``) and
   ``MATRIX_ACCESS_TOKEN``. Disabled by default.

Capabilities: REACTIONS + EDIT_MESSAGE + DELETE_MESSAGE + THREADS.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any
from urllib.parse import quote

import httpx

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import Platform, SendResult
from plugin_sdk.format_converters import matrix_html

# Wave 6.E.3 — approval primitive lives next door. Imported lazily
# inside ``__init__`` to avoid a synthetic-module-name resolution
# failure when OC's plugin loader imports ``adapter`` directly without
# the ``extensions.matrix`` package context.

logger = logging.getLogger("opencomputer.ext.matrix")

# Default /sync long-poll timeout (server holds open up to this long
# waiting for new events). Matrix spec recommends 30s.
_SYNC_TIMEOUT_MS = 30_000

# Initial filter: timeline events only, not presence / typing / receipts.
# Keeps the payload small + reduces server load.
_INITIAL_FILTER = (
    '{"room":{"timeline":{"types":["m.room.message","m.reaction"]}}}'
)


def _is_plain_markdown(text: str) -> bool:
    """Heuristic: does ``text`` contain markdown that would benefit from
    HTML formatting? When False the adapter omits ``formatted_body`` to
    keep payloads small (and avoid noisy "*" → "<em>*</em>" for symbols
    that aren't actually meant to be markdown)."""
    if not text:
        return False
    return any(
        marker in text
        for marker in ("**", "__", "~~", "[", "`", "# ", "## ", "### ")
    )


class MatrixAdapter(BaseChannelAdapter):
    """Matrix channel — Client-Server API. Unencrypted rooms only."""

    platform = Platform.MATRIX
    max_message_length = 60_000
    capabilities = (
        ChannelCapabilities.REACTIONS
        | ChannelCapabilities.EDIT_MESSAGE
        | ChannelCapabilities.DELETE_MESSAGE
        | ChannelCapabilities.THREADS
    )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self._homeserver = str(config["homeserver"]).rstrip("/")
        self._access_token = str(config["access_token"])
        self._client: httpx.AsyncClient | None = None
        self._user_id: str | None = None

        # Wave 6.E.3 — inbound /sync state.
        self._sync_task: asyncio.Task[None] | None = None
        self._sync_stop: asyncio.Event = asyncio.Event()
        self._next_batch: str | None = None
        # Approval primitive — lazy import (see comment above the class).
        try:
            from extensions.matrix.approval import ApprovalQueue
        except ImportError:
            # OC plugin loader path — fall back to importlib + the
            # adapter's own __file__ to find the sibling.
            import importlib.util as _ilu
            from pathlib import Path as _Path
            spec = _ilu.spec_from_file_location(
                "_oc_matrix_approval",
                _Path(__file__).parent / "approval.py",
            )
            mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            ApprovalQueue = mod.ApprovalQueue
        self.approval_queue = ApprovalQueue()
        # Disable inbound polling unless explicitly enabled — back-compat
        # for existing matrix users who only want outbound.
        self._inbound_enabled: bool = bool(
            config.get("inbound_sync", config.get("enable_sync", False)),
        )

    async def connect(self) -> bool:
        """Verify the access token via ``GET /_matrix/client/v3/account/whoami``."""
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            resp = await self._client.get(
                f"{self._homeserver}/_matrix/client/v3/account/whoami"
            )
            if resp.status_code != 200:
                logger.error(
                    "matrix whoami HTTP %s: %s",
                    resp.status_code, resp.text[:200],
                )
                return False
            data = resp.json()
            self._user_id = data.get("user_id")
            logger.info("matrix: connected as %s", self._user_id)
            # Wave 6.E.3 — start /sync polling iff opted in.
            if self._inbound_enabled:
                self._sync_stop.clear()
                self._sync_task = asyncio.create_task(
                    self._poll_forever(),
                    name="matrix-sync",
                )
                logger.info("matrix: inbound /sync polling started")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("matrix connect failed: %s", exc)
            return False

    async def disconnect(self) -> None:
        # Wave 6.E.3 — stop /sync first so it can't fire after the
        # client closes.
        if self._sync_task is not None:
            self._sync_stop.set()
            try:
                await asyncio.wait_for(self._sync_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._sync_task.cancel()
            self._sync_task = None
        # Resolve any still-pending approvals as cancelled so callers
        # don't block forever during teardown.
        self.approval_queue.cancel_all()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Outbound — m.room.message
    # ------------------------------------------------------------------

    async def send(self, chat_id: str, text: str, **kwargs: Any) -> SendResult:
        """Send an ``m.text`` message to the given room id.

        ``chat_id`` is a room ID like ``!roomid:server.example`` or a room
        alias ``#alias:server.example``. Aliases are NOT auto-resolved;
        call your homeserver's ``/directory/room/{alias}`` first if you only
        have an alias.

        ``kwargs`` may include ``thread_root`` (an event id) to thread under.

        PR 3b.3: when ``text`` contains markdown, also includes a
        ``formatted_body`` rendered via
        :mod:`plugin_sdk.format_converters.matrix_html` (org.matrix.custom.html
        format). Plain text is preserved in ``body`` for clients that
        don't support HTML rendering.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        txn_id = uuid.uuid4().hex[:16]
        body = text[: self.max_message_length]
        content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        if _is_plain_markdown(body):
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = matrix_html.convert(body)
        if kwargs.get("thread_root"):
            content["m.relates_to"] = {
                "rel_type": "m.thread",
                "event_id": kwargs["thread_root"],
            }

        async def _do_send() -> SendResult:
            try:
                resp = await self._client.put(
                    f"{self._homeserver}/_matrix/client/v3/rooms/{quote(chat_id)}/send/m.room.message/{txn_id}",
                    json=content,
                )
                if resp.status_code != 200:
                    return SendResult(
                        success=False,
                        error=f"matrix HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                data = resp.json()
                return SendResult(
                    success=True, message_id=str(data.get("event_id") or "")
                )
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_send)

    # ------------------------------------------------------------------
    # Reactions — send an m.reaction event referencing the target event
    # ------------------------------------------------------------------

    async def send_reaction(
        self, chat_id: str, message_id: str, emoji: str, **kwargs: Any
    ) -> SendResult:
        """Add a reaction. Matrix uses unicode emoji directly (no name mapping)."""
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        if not emoji:
            return SendResult(success=False, error="emoji must be non-empty")
        txn_id = uuid.uuid4().hex[:16]
        content = {
            "m.relates_to": {
                "rel_type": "m.annotation",
                "event_id": message_id,
                "key": emoji,
            }
        }

        async def _do_react() -> SendResult:
            try:
                resp = await self._client.put(
                    f"{self._homeserver}/_matrix/client/v3/rooms/{quote(chat_id)}/send/m.reaction/{txn_id}",
                    json=content,
                )
                if resp.status_code != 200:
                    return SendResult(
                        success=False,
                        error=f"matrix HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                return SendResult(success=True)
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_react)

    # ------------------------------------------------------------------
    # Edit — Matrix represents edits via m.replace events
    # ------------------------------------------------------------------

    async def edit_message(
        self, chat_id: str, message_id: str, text: str, **kwargs: Any
    ) -> SendResult:
        """Edit via an ``m.replace`` event referencing ``message_id``.

        Matrix clients render edits by combining the original event with
        the latest m.replace; servers don't change the original event.

        PR 3b.3: emits ``formatted_body`` + ``format`` on both the
        fallback body and ``m.new_content`` when the text carries
        markdown.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        txn_id = uuid.uuid4().hex[:16]
        body = text[: self.max_message_length]
        new_content: dict[str, Any] = {"msgtype": "m.text", "body": body}
        content: dict[str, Any] = {
            "msgtype": "m.text",
            "body": f"* {body}",  # convention: "* " prefix in fallback body
            "m.new_content": new_content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": message_id},
        }
        if _is_plain_markdown(body):
            html = matrix_html.convert(body)
            content["format"] = "org.matrix.custom.html"
            content["formatted_body"] = f"* {html}"
            new_content["format"] = "org.matrix.custom.html"
            new_content["formatted_body"] = html

        async def _do_edit() -> SendResult:
            try:
                resp = await self._client.put(
                    f"{self._homeserver}/_matrix/client/v3/rooms/{quote(chat_id)}/send/m.room.message/{txn_id}",
                    json=content,
                )
                if resp.status_code != 200:
                    return SendResult(
                        success=False,
                        error=f"matrix HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                data = resp.json()
                return SendResult(
                    success=True, message_id=str(data.get("event_id") or "")
                )
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_edit)

    # ------------------------------------------------------------------
    # Delete — Matrix uses redactions
    # ------------------------------------------------------------------

    async def delete_message(
        self, chat_id: str, message_id: str, **kwargs: Any
    ) -> SendResult:
        """Redact (delete) via ``PUT /_matrix/client/v3/rooms/.../redact/...``.

        ``kwargs`` may include ``reason`` (string) which is recorded with
        the redaction.
        """
        if self._client is None:
            return SendResult(success=False, error="adapter not connected")
        txn_id = uuid.uuid4().hex[:16]
        content: dict[str, Any] = {}
        if kwargs.get("reason"):
            content["reason"] = str(kwargs["reason"])

        async def _do_delete() -> SendResult:
            try:
                resp = await self._client.put(
                    f"{self._homeserver}/_matrix/client/v3/rooms/{quote(chat_id)}/redact/{quote(message_id)}/{txn_id}",
                    json=content,
                )
                if resp.status_code != 200:
                    return SendResult(
                        success=False,
                        error=f"matrix HTTP {resp.status_code}: {resp.text[:200]}",
                    )
                return SendResult(success=True)
            except Exception as exc:  # noqa: BLE001
                if self._is_retryable_error(exc):
                    raise
                return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")

        return await self._send_with_retry(_do_delete)

    # ------------------------------------------------------------------
    # Wave 6.E.3 — inbound /sync long-poll
    # ------------------------------------------------------------------

    async def _poll_forever(self) -> None:
        """Long-poll ``/sync`` until ``self._sync_stop`` fires.

        Behaviour:
        - First tick uses an initial filter (timeline only) and no
          ``since`` token — Matrix returns "current state" without a
          full backfill, which is what we want.
        - Subsequent ticks pass the previous ``next_batch`` to receive
          only deltas.
        - 401 from the server flips the adapter to fatal-non-retryable
          (token revoked / mistyped) and exits.
        - Other HTTP errors back off with exponential delay capped at
          60s; this matches the pattern in the telegram adapter.

        See https://spec.matrix.org/latest/client-server-api/#syncing
        """
        if self._client is None:
            return
        consecutive_errors = 0
        backoff = 1.0
        while not self._sync_stop.is_set():
            params: dict[str, str] = {"timeout": str(_SYNC_TIMEOUT_MS)}
            if self._next_batch:
                params["since"] = self._next_batch
            else:
                params["filter"] = _INITIAL_FILTER

            try:
                resp = await self._client.get(
                    f"{self._homeserver}/_matrix/client/v3/sync",
                    params=params,
                )
            except (httpx.RequestError, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                consecutive_errors += 1
                wait = min(60.0, backoff * (2 ** min(consecutive_errors, 5)))
                logger.warning(
                    "matrix /sync request error (#%d, sleeping %.1fs): %s",
                    consecutive_errors, wait, exc,
                )
                try:
                    await asyncio.wait_for(self._sync_stop.wait(), timeout=wait)
                except TimeoutError:
                    pass
                continue

            if resp.status_code == 401:
                logger.error(
                    "matrix /sync returned 401 — access token rejected. "
                    "Stopping inbound polling. Set MATRIX_ACCESS_TOKEN to "
                    "a valid value to recover."
                )
                # Reuse the OpenClaw fatal-error path if available.
                if hasattr(self, "_set_fatal_error"):
                    self._set_fatal_error(
                        "matrix-auth",
                        "MATRIX_ACCESS_TOKEN rejected by /sync",
                        retryable=False,
                    )
                return
            if resp.status_code != 200:
                consecutive_errors += 1
                wait = min(60.0, backoff * (2 ** min(consecutive_errors, 5)))
                logger.warning(
                    "matrix /sync HTTP %d (#%d, sleeping %.1fs): %s",
                    resp.status_code, consecutive_errors, wait, resp.text[:200],
                )
                try:
                    await asyncio.wait_for(self._sync_stop.wait(), timeout=wait)
                except TimeoutError:
                    pass
                continue

            consecutive_errors = 0
            data = resp.json()
            self._next_batch = data.get("next_batch") or self._next_batch
            try:
                self._handle_sync_response(data)
            except Exception:  # noqa: BLE001 — never crash the loop
                logger.exception("matrix /sync: handler error (ignored)")

            # Reap expired approvals every tick.
            self.approval_queue.reap_expired()

    def _handle_sync_response(self, data: dict[str, Any]) -> None:
        """Walk a sync response and dispatch reaction events.

        We intentionally only look at ``rooms.join.<room>.timeline.events``
        and only at type ``m.reaction``; everything else (presence,
        m.room.message, account_data) is ignored. The whole point of
        this polling loop is to drive the approval queue — message
        receipt is the webhook adapter's job.
        """
        rooms = data.get("rooms", {}).get("join", {})
        if not isinstance(rooms, dict):
            return
        for _room_id, room in rooms.items():
            timeline = room.get("timeline", {})
            for evt in timeline.get("events", []) or []:
                if evt.get("type") != "m.reaction":
                    continue
                # Skip our own reactions so we don't accidentally
                # resolve our own approvals.
                if evt.get("sender") and evt["sender"] == self._user_id:
                    continue
                relates = evt.get("content", {}).get("m.relates_to", {})
                if relates.get("rel_type") != "m.annotation":
                    continue
                target = relates.get("event_id")
                emoji = relates.get("key")
                if not target or not emoji:
                    continue
                resolved = self.approval_queue.on_reaction(target, emoji)
                if resolved:
                    logger.info(
                        "matrix approval: %s reacted with %r → resolved %s",
                        evt.get("sender", "?"), emoji, target,
                    )


__all__ = ["MatrixAdapter"]
