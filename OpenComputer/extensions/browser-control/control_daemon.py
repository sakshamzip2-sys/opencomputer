"""WebSocket server for the browser-control extension (Wave 6).

Hosted at ``ws://127.0.0.1:<control_port>/ext`` alongside the existing
HTTP dispatcher routes. The extension (loaded into Chrome) connects on
startup, sends a ``hello`` with its contextId + version, then awaits
commands.

Daemon → extension flow:
  1. Caller (adapter / Browser tool) calls ``ControlDaemon.send(cmd)``
  2. Daemon serializes the Command to wire JSON, writes to the WS
  3. Daemon stores a Future keyed by ``cmd.id``
  4. Extension processes, returns Result via WS
  5. Daemon's WS reader correlates the Result to the pending Future,
     resolves it
  6. Caller receives the Result (or TimeoutError after 30s)

Concurrency: multiple in-flight commands from concurrent adapters are
demuxed by ``cmd.id``. Per-connection state lives in
``ConnectedExtension.pending`` so two extensions (two profiles) don't
collide.

Lifecycle:
  - Daemon starts when the browser-control plugin first needs the
    control-extension driver (lazily — cheap when nobody asks)
  - Survives across requests; one daemon per agent process
  - Stops on plugin teardown or process exit (production fix tracked
    separately — see scripts/cleanup_agent_chrome.sh)

Tests inject a stub server / stub extension via the ``server_factory``
kwarg; production uses ``websockets.serve``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .control_protocol import (
    DEFAULT_COMMAND_TIMEOUT_S,
    DEFAULT_CONTROL_DAEMON_PORT,
    DEFAULT_CONTROL_WS_PATH,
    SUPPORTED_ACTIONS_V0_6,
    Command,
    ConnectedExtension,
    HelloMessage,
    LogMessage,
    Result,
)

_log = logging.getLogger("opencomputer.browser_control.control_daemon")


class ControlDaemonError(RuntimeError):
    """Raised when the daemon can't satisfy a request (no extension, timeout, etc.)."""


class CommandTimeoutError(ControlDaemonError):
    """Raised when a command exceeds its timeout waiting for a Result."""


class ActionNotSupportedError(ControlDaemonError):
    """Raised when the requested action isn't in v0.6's supported set.

    Daemon-side gate; the extension itself can handle all 14 actions, but
    we refuse the unimplemented-on-our-side ones to avoid silently
    wedging on Result types we don't translate yet.
    """


@dataclass(slots=True)
class _PendingFuture:
    """One in-flight command awaiting its Result.

    Stored keyed by ``cmd.id`` in ``ConnectedExtension.pending``.
    """

    future: asyncio.Future[Result]
    action: str


@dataclass(slots=True)
class ControlDaemon:
    """Per-process daemon hosting the WS endpoint.

    One instance per agent process. Multiple connected extensions
    (e.g. one per profile) tracked by their ``context_id``.
    """

    port: int = DEFAULT_CONTROL_DAEMON_PORT
    ws_path: str = DEFAULT_CONTROL_WS_PATH
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S
    extensions: dict[str, ConnectedExtension] = field(default_factory=dict)
    _server_task: asyncio.Task[None] | None = None
    _stop_event: asyncio.Event | None = None
    # Map (context_id, command_id) → pending future, for in-flight cmds
    _pending: dict[tuple[str, str], _PendingFuture] = field(default_factory=dict)
    # Per-extension WS sender callbacks (set when WS connects)
    _senders: dict[str, Callable[[str], Awaitable[None]]] = field(default_factory=dict)

    async def start(self) -> None:
        """Spawn the WS server. Idempotent.

        Production callers don't need to invoke this directly — the
        ``BrowserControlExtensionDriver.spawn(...)`` entrypoint starts
        the daemon if needed.
        """
        if self._server_task is not None and not self._server_task.done():
            return
        self._stop_event = asyncio.Event()
        # Lazy import — websockets is a heavy import; only pay when actually used
        import websockets  # type: ignore[import-not-found]

        async def _on_connection(ws: Any) -> None:  # noqa: ANN401
            await self._handle_connection(ws)

        # websockets.serve handles the path matching internally — we
        # gate on `path` inside _handle_connection.
        async def _runner() -> None:
            assert self._stop_event is not None
            async with websockets.serve(_on_connection, "127.0.0.1", self.port):
                await self._stop_event.wait()

        self._server_task = asyncio.create_task(
            _runner(), name="opencomputer-control-daemon"
        )

    async def stop(self) -> None:
        """Signal graceful shutdown of the WS server. Idempotent."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=2.0)
            except TimeoutError:
                self._server_task.cancel()
        self._server_task = None
        self._stop_event = None
        # Cancel all pending futures
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(
                    ControlDaemonError("daemon stopping; command cancelled")
                )
        self._pending.clear()
        self.extensions.clear()
        self._senders.clear()

    async def send(
        self,
        cmd: Command,
        *,
        context_id: str | None = None,
    ) -> Result:
        """Dispatch a command to the connected extension and await the Result.

        ``context_id`` selects which connected extension when multiple
        are present. None → first extension found (production usually
        has exactly one connected per agent process).
        """
        if cmd.action not in SUPPORTED_ACTIONS_V0_6:
            raise ActionNotSupportedError(
                f"action {cmd.action!r} is not yet supported in v0.6 "
                f"(extension can handle it but daemon translation is in v0.6.x)"
            )

        target_ctx = context_id or self._first_context_id()
        if target_ctx is None:
            raise ControlDaemonError(
                "no extension connected — open Chrome with the OpenComputer "
                "Browser Control extension loaded, or check extension popup "
                "for connection status"
            )

        sender = self._senders.get(target_ctx)
        if sender is None:
            raise ControlDaemonError(
                f"extension for contextId={target_ctx!r} disconnected during dispatch"
            )

        # Pre-allocate the future before sending so we can't race with
        # an unusually fast Result.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Result] = loop.create_future()
        self._pending[(target_ctx, cmd.id)] = _PendingFuture(
            future=future, action=cmd.action
        )

        try:
            await sender(json.dumps(cmd.to_wire()))
        except Exception:
            # Sender failed before we could even register the cmd — clean
            # up the pending entry.
            self._pending.pop((target_ctx, cmd.id), None)
            raise

        try:
            return await asyncio.wait_for(future, timeout=self.command_timeout_s)
        except TimeoutError as exc:
            self._pending.pop((target_ctx, cmd.id), None)
            raise CommandTimeoutError(
                f"command {cmd.id} ({cmd.action}) timed out after "
                f"{self.command_timeout_s}s"
            ) from exc

    def _first_context_id(self) -> str | None:
        if not self.extensions:
            return None
        # Stable iteration: dict preserves insertion order in 3.7+
        return next(iter(self.extensions))

    def make_command(
        self,
        action: str,
        **kwargs: Any,
    ) -> Command:
        """Construct a Command with a fresh UUID.

        Convenience helper so callers don't have to mint ids themselves.
        """
        # Action validation happens in send(); here we just wrap.
        return Command(id=uuid.uuid4().hex, action=action, **kwargs)  # type: ignore[arg-type]

    async def _handle_connection(self, ws: Any) -> None:
        """Handle one WS connection from the extension.

        Reads the ``hello`` message first to register the extension,
        then loops reading Results / log messages until disconnect.
        """
        # Path-gate: only accept connections on /ext (matches OpenCLI's
        # daemon and the protocol.ts DAEMON_WS_URL).
        path = getattr(ws, "path", None)
        if path is not None and path != self.ws_path:
            await ws.close(code=1008, reason=f"unexpected path {path!r}")
            return

        ext: ConnectedExtension | None = None
        ctx_id: str | None = None

        async def _send_str(payload: str) -> None:
            await ws.send(payload)

        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    # Binary frames not part of our protocol.
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as exc:
                    _log.warning("control-daemon: invalid JSON from extension: %s", exc)
                    continue
                if not isinstance(msg, dict):
                    continue

                msg_type = msg.get("type")

                if msg_type == "hello":
                    hello = HelloMessage.from_wire(msg)
                    ctx_id = hello.context_id or "default"
                    ext = ConnectedExtension(
                        context_id=ctx_id,
                        extension_version=hello.version,
                        compat_range=hello.compat_range,
                    )
                    self.extensions[ctx_id] = ext
                    self._senders[ctx_id] = _send_str
                    _log.info(
                        "control-daemon: extension connected — contextId=%s, "
                        "version=%s, compat=%s",
                        ctx_id,
                        hello.version,
                        hello.compat_range,
                    )
                    continue

                if msg_type == "log":
                    log_msg = LogMessage.from_wire(msg)
                    _log.log(
                        {"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}[
                            log_msg.level
                        ],
                        "[ext %s] %s",
                        ctx_id or "?",
                        log_msg.msg,
                    )
                    continue

                # Otherwise: it's a Result for a pending command.
                result_id = msg.get("id")
                if not isinstance(result_id, str) or ctx_id is None:
                    continue
                pending = self._pending.pop((ctx_id, result_id), None)
                if pending is None:
                    _log.debug(
                        "control-daemon: orphan Result for %s (action unknown)",
                        result_id,
                    )
                    continue
                if not pending.future.done():
                    try:
                        pending.future.set_result(Result.from_wire(msg))
                    except Exception as exc:  # noqa: BLE001
                        pending.future.set_exception(exc)
        finally:
            if ctx_id is not None:
                self.extensions.pop(ctx_id, None)
                self._senders.pop(ctx_id, None)
                # Fail any still-pending commands for this extension.
                for key in list(self._pending.keys()):
                    if key[0] != ctx_id:
                        continue
                    pending = self._pending.pop(key)
                    if not pending.future.done():
                        pending.future.set_exception(
                            ControlDaemonError(
                                f"extension {ctx_id!r} disconnected; "
                                f"command {key[1]} ({pending.action}) cancelled"
                            )
                        )
            _log.info("control-daemon: connection closed (contextId=%s)", ctx_id)
