"""WhatsAppBridgeAdapter — Baileys-bridge channel (PR 6.2).

Adapter for a *personal-account* WhatsApp connection, in contrast to
the Cloud-API plugin in ``extensions/whatsapp/`` which only speaks the
business-API. The bridge architecture is:

  +------------------+   HTTP   +-----------------------+
  |  Python adapter  | <------> |  Node.js Baileys      |
  |   (this file)    |          |  index.js (subproc)   |
  +------------------+          +-----------------------+
                                        |
                                        v
                            WhatsApp multi-device protocol

The Node bridge owns the encrypted socket; we own everything else.

Key behaviours:

* **Lazy spawn.** ``connect()`` starts the Node subprocess; before that
  the supervisor is ``None``. This keeps test imports cheap.
* **HTTP plane.** Outbound POST ``/send {to, text}``; inbound long-poll
  GET ``/messages?since=<id>`` returning a JSON list of message
  envelopes.
* **QR-code login.** First-launch Node prints the QR text to stdout;
  the adapter's stdout reader scrapes it and re-emits it as a system
  ``MessageEvent`` so the user sees it in whichever channel they're
  currently chatting from.
* **Echo suppression.** Tracked on the Node side via
  ``recentlySentIds``. The adapter sends the message id back as part
  of every ``/send`` so the bridge can compare against incoming
  envelopes and drop self-echoes before they reach us.
* **Cross-platform shutdown.** Delegates to
  :class:`BridgeSupervisor.terminate` which knows how to kill the
  whole process tree on both Windows and POSIX.

Env vars consumed via ``config``:
* ``host`` — bind host of the bridge (default ``127.0.0.1``)
* ``port`` — bind port (default ``3001``)
* ``auth_dir`` — where the bridge persists session credentials
* ``bridge_dir`` — directory containing the Node ``index.js``
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

try:
    from bridge_supervisor import BridgeSupervisor, _kill_port_process
except ImportError:  # pragma: no cover
    from extensions.whatsapp_bridge.bridge_supervisor import (
        BridgeSupervisor,
        _kill_port_process,
    )

from plugin_sdk.channel_contract import BaseChannelAdapter, ChannelCapabilities
from plugin_sdk.core import MessageEvent, Platform, SendResult

logger = logging.getLogger("opencomputer.ext.whatsapp_bridge")


#: Phrase the Node bridge prints to stdout when it has a QR ready.
#: Format: ``QR: <base64-or-text>``. The adapter parses lines starting
#: with this prefix and surfaces them as system events.
_QR_STDOUT_PREFIX = "QR:"

#: Phrase the Node bridge prints when the websocket is up.
_READY_STDOUT_MARKER = "READY"


class WhatsAppBridgeAdapter(BaseChannelAdapter):
    """WhatsApp Baileys-bridge channel adapter."""

    # Re-uses the WHATSAPP platform so existing routing keys still work;
    # the registration key in plugin.py distinguishes the two adapters.
    platform = Platform.WHATSAPP
    max_message_length = 4096

    capabilities = (
        ChannelCapabilities.REACTIONS
    )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.host: str = str(config.get("host", "127.0.0.1"))
        self.port: int = int(config.get("port", 3001))
        self.auth_dir: str = str(config.get("auth_dir", ""))
        self.bridge_dir: str = str(config.get("bridge_dir", ""))
        self._base_url = f"http://{self.host}:{self.port}"
        self._client: httpx.AsyncClient | None = None
        self._supervisor: BridgeSupervisor | None = None
        self._poll_task: asyncio.Task | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._last_message_id: str | None = None
        # Echo-suppression mirror: the bridge tracks ids on its side
        # but we also keep a small local set so the test harness can
        # exercise the path without spawning Node.
        self._recently_sent_ids: set[str] = set()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=30.0,
            )
        return self._client

    async def connect(self) -> bool:
        """Spawn the bridge (if not running), verify health, start poll loop."""
        # Reset stop signal — connect() may be invoked after a clean disconnect.
        self._stop_event = asyncio.Event()

        if self._supervisor is None or not self._supervisor.is_alive():
            self._supervisor = BridgeSupervisor(
                bridge_dir=self.bridge_dir,
                host=self.host,
                port=self.port,
                auth_dir=self.auth_dir,
            )
            try:
                self._supervisor.spawn()
            except FileNotFoundError as e:
                logger.error(
                    "whatsapp-bridge: node binary not found (%s) — "
                    "install Node.js >= 18 and re-run",
                    e,
                )
                return False
            except Exception as e:  # noqa: BLE001
                logger.error("whatsapp-bridge: spawn failed: %s", e)
                return False
            # Give the bridge a moment to bind its port before the first
            # health probe. This is short on purpose — the poll loop will
            # tolerate a few early 503s while Baileys finishes booting.
            self._stdout_task = asyncio.create_task(self._read_stdout())

        # Health probe with retry (Baileys boot can take 1-2s).
        ok = await self._wait_for_health(timeout_s=10.0)
        if not ok:
            logger.warning(
                "whatsapp-bridge: bridge unreachable on %s — "
                "continuing anyway, poll loop will retry",
                self._base_url,
            )

        self._poll_task = asyncio.create_task(self._poll_forever())
        return ok

    async def _wait_for_health(self, timeout_s: float = 10.0) -> bool:
        """Poll ``/health`` until 200 or *timeout_s* elapses."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                resp = await self.http.get("/health")
                if resp.status_code == 200:
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return False

    async def disconnect(self) -> None:
        """Stop poll loop, close http session, kill bridge subprocess."""
        self._stop_event.set()
        for task in (self._poll_task, self._stdout_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._poll_task = None
        self._stdout_task = None

        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

        if self._supervisor is not None:
            self._terminate_bridge_process()

    def _terminate_bridge_process(self) -> None:
        """Clean termination of the bridge subprocess (cross-platform)."""
        if self._supervisor is None:
            return
        try:
            self._supervisor.terminate(timeout=5.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("whatsapp-bridge: termination raised: %s", e)
        # Belt-and-braces: also reap any process still on the port (a
        # crashed Node child may have re-bound).
        try:
            _kill_port_process(self.port)
        except Exception:  # noqa: BLE001
            pass
        self._supervisor = None

    def _kill_port_process(self, port: int) -> list[int]:
        """Public-ish wrapper so tests can drive the cross-platform reaper."""
        return _kill_port_process(port)

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        text: str,
        **kwargs: Any,
    ) -> SendResult:
        """POST ``/send`` to the bridge with the message envelope."""

        async def _do_send() -> SendResult:
            try:
                resp = await self.http.post(
                    "/send", json={"to": chat_id, "text": text}
                )
            except httpx.RequestError as exc:
                if self._is_retryable_error(exc):
                    raise
                return SendResult(
                    success=False, error=f"{type(exc).__name__}: {exc}"
                )
            if resp.status_code >= 400:
                return SendResult(
                    success=False,
                    error=f"bridge returned {resp.status_code}: {resp.text[:300]}",
                )
            try:
                body = resp.json()
            except Exception:  # noqa: BLE001
                body = {}
            mid = str(body.get("id") or body.get("message_id") or "")
            if mid:
                self._recently_sent_ids.add(mid)
            return SendResult(success=True, message_id=mid or None)

        return await self._send_with_retry(_do_send)

    # ------------------------------------------------------------------
    # Inbound — long-poll
    # ------------------------------------------------------------------

    async def _poll_forever(self) -> None:
        """Long-poll the bridge for inbound envelopes until told to stop."""
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                params = {"timeout": "25"}
                if self._last_message_id:
                    params["since"] = self._last_message_id
                resp = await self.http.get("/messages", params=params)
                if resp.status_code != 200:
                    raise httpx.HTTPStatusError(
                        f"poll {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                events = resp.json() or []
                for env in events:
                    self._last_message_id = str(env.get("id") or "") or self._last_message_id
                    await self._handle_inbound_envelope(env)
                # Reset backoff on success
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("whatsapp-bridge: poll error: %s", e)
                # Don't tight-loop on a flapping bridge.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=backoff
                    )
                    return  # stopped
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 30.0)

    async def _handle_inbound_envelope(self, env: dict[str, Any]) -> None:
        """Convert a bridge envelope into a MessageEvent (with echo guard)."""
        mid = str(env.get("id") or "")
        if mid and mid in self._recently_sent_ids:
            # Bridge should have suppressed this on its side, but a
            # double-belt local check costs nothing. Trim the set
            # opportunistically to keep memory bounded.
            self._recently_sent_ids.discard(mid)
            return
        from_me = bool(env.get("fromMe") or env.get("from_me"))
        if from_me:
            return
        chat_id = str(env.get("chat") or env.get("chat_id") or "")
        if not chat_id:
            return
        user_id = str(env.get("sender") or env.get("user") or chat_id)
        text = str(env.get("text") or env.get("body") or "")
        if not text:
            return
        ts_raw = env.get("timestamp") or env.get("ts") or time.time()
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            ts = time.time()
        event = MessageEvent(
            platform=Platform.WHATSAPP,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            timestamp=ts,
            metadata={"message_id": mid, "via": "bridge"},
        )
        await self.handle_message(event)

    # ------------------------------------------------------------------
    # Stdout reader — scrape QR text and surface it as a system event
    # ------------------------------------------------------------------

    async def _read_stdout(self) -> None:
        """Drain bridge stdout, surfacing QR-text lines as system events."""
        sup = self._supervisor
        if sup is None or sup._proc is None or sup._proc.stdout is None:
            return
        loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            line = await loop.run_in_executor(None, sup._proc.stdout.readline)
            if not line:
                # EOF — process exited.
                return
            line = line.rstrip()
            sup.append_stdout(line)
            if line.startswith(_QR_STDOUT_PREFIX):
                qr_payload = line[len(_QR_STDOUT_PREFIX):].strip()
                await self._dispatch_qr_event(qr_payload)
            elif _READY_STDOUT_MARKER in line:
                logger.info("whatsapp-bridge: bridge READY")

    async def _dispatch_qr_event(self, qr_payload: str) -> None:
        """Surface a QR-code payload as a system MessageEvent."""
        if not qr_payload:
            return
        event = MessageEvent(
            platform=Platform.WHATSAPP,
            chat_id="__system__",
            user_id="__system__",
            text=(
                "WhatsApp bridge needs login. Scan this QR with the "
                "WhatsApp app on your phone:\n" + qr_payload
            ),
            timestamp=time.time(),
            metadata={"system": True, "kind": "whatsapp_bridge_qr"},
        )
        try:
            await self.handle_message(event)
        except Exception as e:  # noqa: BLE001
            logger.warning("whatsapp-bridge: QR dispatch failed: %s", e)


__all__ = ["WhatsAppBridgeAdapter", "_QR_STDOUT_PREFIX", "_READY_STDOUT_MARKER"]
