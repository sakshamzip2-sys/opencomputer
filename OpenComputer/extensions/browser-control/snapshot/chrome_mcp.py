"""Chrome MCP client — wraps the upstream `chrome-devtools-mcp` subprocess.

Default invocation:

    npx -y chrome-devtools-mcp@latest --autoConnect \
        --experimentalStructuredContent --experimental-page-id-routing

Flags are load-bearing — without ``--experimentalStructuredContent`` the
``take_snapshot`` response degrades to text-only and ``extract_snapshot``
falls back to regex parsing (we don't ship that fallback for v0.1).

Uses the official `mcp` Python SDK (StdioServerParameters + stdio_client +
ClientSession). We do NOT hand-roll JSON-RPC framing.

Per-(profile_name, user_data_dir) session cache. Tool errors keep the
session alive (raise ``ChromeMcpToolError``); transport errors tear it
down (raise ``ChromeMcpTransportError``).

Owner-task pattern (v0.5 Bug C)
-------------------------------
The MCP session's full lifetime — ``__aenter__`` through ``__aexit__`` —
must live inside ONE asyncio task. ``mcp.client.stdio.stdio_client`` and
``mcp.ClientSession`` use anyio task groups internally; anyio enforces
that the task that opens a cancel scope must also be the task that
closes it. The previous design opened the contexts in task A (spawn) and
closed them in task B (close), tripping anyio's cross-task guard:

    RuntimeError: Attempted to exit cancel scope in a different task than
    it was entered in.

Fix: a dedicated "lifetime" task owns the session for its entire life.
``spawn_chrome_mcp`` starts that task and waits on a ``ready`` event;
``ChromeMcpClient.close()`` only signals the lifetime task to exit (via
a ``done`` event) and awaits the task. The lifetime task does the
``__aexit__`` itself, in the same task that did the ``__aenter__``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Final

_log = logging.getLogger("opencomputer.browser_control.snapshot.chrome_mcp")

DEFAULT_CHROME_MCP_COMMAND: Final[str] = "npx"
DEFAULT_CHROME_MCP_ARGS: Final[tuple[str, ...]] = (
    "-y",
    "chrome-devtools-mcp@latest",
    "--autoConnect",
    "--experimentalStructuredContent",
    "--experimental-page-id-routing",
)


# ─── exceptions ───────────────────────────────────────────────────────


class ChromeMcpUnavailableError(RuntimeError):
    """Chrome MCP couldn't be launched — typically Node missing or npx absent."""


class ChromeMcpTransportError(RuntimeError):
    """Transport-layer error — the session must be torn down and reattached."""


class ChromeMcpToolError(RuntimeError):
    """Tool-level error (`isError: true` in the MCP response). Session OK."""


# ─── data ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ChromeMcpToolResult:
    """Mirror of the MCP SDK's CallToolResult — with the bits we care about."""

    structured_content: dict[str, Any] | None = None
    content_text: list[str] = field(default_factory=list)
    is_error: bool = False
    error_message: str | None = None


# ─── client ───────────────────────────────────────────────────────────


class ChromeMcpClient:
    """A connected client over a chrome-devtools-mcp subprocess.

    Acquired via :func:`spawn_chrome_mcp`. The instance is reusable for
    the lifetime of the underlying transport — transport errors cause
    ``call_tool`` to raise ``ChromeMcpTransportError`` *and* mark the
    client closed; the caller (server_context lifecycle) is responsible
    for re-spawning.

    Two construction paths:

      1. Production / real subprocess — :func:`spawn_chrome_mcp` starts
         a dedicated ``lifetime`` asyncio task that owns the
         ``stdio_client`` + ``ClientSession`` context managers. Closing
         signals the task via the ``done`` event; the task itself runs
         ``__aexit__`` for both contexts. This avoids anyio's cross-task
         cancel-scope violation.
      2. Test factory — ``session_factory`` returns ``(session,
         cleanup)``; the cleanup is invoked directly from
         ``close()``. This path predates the owner-task pattern; tests
         that use it never trip anyio because the fakes don't open
         cancel scopes.
    """

    def __init__(
        self,
        *,
        session: Any,  # mcp.ClientSession
        cleanup: Any | None = None,  # async cleanup callable (test path)
        lifetime_task: asyncio.Task[Any] | None = None,  # production path
        done_event: asyncio.Event | None = None,  # production path
        pid: int | None = None,
    ) -> None:
        if cleanup is None and lifetime_task is None:
            raise ValueError(
                "ChromeMcpClient requires either `cleanup` (test path) or "
                "`lifetime_task` + `done_event` (production owner-task path)"
            )
        self._session = session
        self._cleanup = cleanup
        self._lifetime_task = lifetime_task
        self._done_event = done_event
        self._pid = pid
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def session(self) -> Any:
        """Expose the underlying mcp.ClientSession for advanced callers.

        Backward-compat: existing call sites that reach into
        ``client._session`` continue to work, but new code should prefer
        :meth:`call_tool` / :meth:`list_tools`.
        """
        return self._session

    async def list_tools(self) -> list[str]:
        """Return the names of tools advertised by the server."""
        if self._closed:
            raise ChromeMcpTransportError("ChromeMcpClient is closed")
        try:
            tools = await self._session.list_tools()
        except Exception as exc:  # noqa: BLE001
            await self._mark_closed()
            raise ChromeMcpTransportError(f"list_tools failed: {exc}") from exc
        return [t.name for t in getattr(tools, "tools", [])]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> ChromeMcpToolResult:
        """Call a tool. Tool errors raise ChromeMcpToolError; transport errors tear down."""
        if self._closed:
            raise ChromeMcpTransportError("ChromeMcpClient is closed")
        async with self._lock:
            try:
                result = await self._session.call_tool(name, arguments=arguments or {})
            except Exception as exc:  # noqa: BLE001 — transport-layer
                await self._mark_closed()
                raise ChromeMcpTransportError(
                    f"call_tool({name!r}) transport error: {exc}"
                ) from exc

        sc = getattr(result, "structuredContent", None)
        if sc is None:
            sc = getattr(result, "structured_content", None)
        content = getattr(result, "content", []) or []
        text_blocks: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                text_blocks.append(text)
        is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))

        wrapped = ChromeMcpToolResult(
            structured_content=sc if isinstance(sc, dict) else None,
            content_text=text_blocks,
            is_error=is_error,
        )
        if is_error:
            wrapped.error_message = "; ".join(text_blocks) or f"tool {name!r} returned isError"
            raise ChromeMcpToolError(wrapped.error_message)
        return wrapped

    async def close(self) -> None:
        await self._mark_closed()

    async def _mark_closed(self) -> None:
        if self._closed:
            return
        self._closed = True

        # Production path — signal the lifetime task to exit. The task
        # itself unwinds the ClientSession + stdio_client context
        # managers in the same task that entered them, so anyio's
        # cancel-scope guard is satisfied.
        if self._lifetime_task is not None and self._done_event is not None:
            self._done_event.set()
            try:
                await asyncio.wait_for(self._lifetime_task, timeout=5.0)
            except TimeoutError:
                _log.debug(
                    "ChromeMcpClient lifetime task did not exit within 5s — cancelling"
                )
                self._lifetime_task.cancel()
                with suppress(BaseException):
                    await self._lifetime_task
            except Exception as exc:  # noqa: BLE001
                _log.debug("ChromeMcpClient lifetime task raised on close: %s", exc)
            return

        # Test path — fire the legacy cleanup callable.
        if self._cleanup is not None:
            try:
                await self._cleanup()
            except Exception as exc:  # noqa: BLE001
                _log.debug("ChromeMcpClient cleanup raised: %s", exc)


# ─── helpers shared with snapshot pipeline ────────────────────────────


def extract_structured_content(result: ChromeMcpToolResult) -> dict[str, Any]:
    return result.structured_content or {}


def extract_snapshot(result: ChromeMcpToolResult) -> dict[str, Any]:
    """Pull ``structuredContent.snapshot`` from a `take_snapshot` result.

    Raises ``ChromeMcpToolError`` if missing — the structured-content flag
    is non-optional for the snapshot path.
    """
    sc = result.structured_content or {}
    snapshot = sc.get("snapshot") if isinstance(sc, dict) else None
    if not isinstance(snapshot, dict):
        raise ChromeMcpToolError(
            "take_snapshot did not return structuredContent.snapshot — "
            "is the server running with --experimentalStructuredContent?"
        )
    return snapshot


def extract_message_text(result: ChromeMcpToolResult) -> str:
    sc = result.structured_content or {}
    msg = sc.get("message") if isinstance(sc, dict) else None
    if isinstance(msg, str) and msg:
        return msg
    for text in result.content_text:
        if text:
            return text
    return ""


def extract_json_message(result: ChromeMcpToolResult) -> Any:
    """Try every text block as JSON; raise the last error if none parse."""
    last_err: Exception | None = None
    for candidate in result.content_text:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError) as exc:
            last_err = exc
    sc = result.structured_content
    if isinstance(sc, dict):
        return sc
    if last_err is not None:
        raise ChromeMcpToolError(f"no JSON-parseable content: {last_err}") from last_err
    raise ChromeMcpToolError("no content blocks to parse")


# ─── spawn ────────────────────────────────────────────────────────────


def _build_args(*, user_data_dir: str | None) -> list[str]:
    args = list(DEFAULT_CHROME_MCP_ARGS)
    udir = (user_data_dir or "").strip()
    if udir:
        args.extend(["--userDataDir", udir])
    return args


async def spawn_chrome_mcp(
    *,
    profile_name: str | None = None,
    user_data_dir: str | None = None,
    command: str = DEFAULT_CHROME_MCP_COMMAND,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    session_factory: Any | None = None,
) -> ChromeMcpClient:
    """Boot the chrome-devtools-mcp subprocess and return a connected client.

    ``session_factory`` is for tests — must be an async callable that
    returns a ``(session, cleanup)`` pair.

    Production path uses the owner-task pattern (Bug C, v0.5): a
    dedicated asyncio task enters both ``stdio_client`` and
    ``ClientSession`` and blocks on a ``done`` event until
    :meth:`ChromeMcpClient.close` signals shutdown. The same task then
    unwinds both contexts, satisfying anyio's "exit cancel scope from
    the same task that entered it" rule.
    """
    if session_factory is not None:
        session, cleanup = await session_factory(profile_name=profile_name, user_data_dir=user_data_dir)
        client = ChromeMcpClient(session=session, cleanup=cleanup)
        try:
            await client.list_tools()  # smoke-test the transport
        except ChromeMcpTransportError:
            await client.close()
            raise
        return client

    full_args = list(args) if args is not None else _build_args(user_data_dir=user_data_dir)

    # Import the SDK eagerly so a missing-dep failure surfaces here
    # (with the documented message) before we spawn an asyncio task.
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise ChromeMcpUnavailableError(
            "mcp Python SDK not installed — install with `pip install opencomputer[browser]`"
        ) from exc

    ready = asyncio.Event()
    done = asyncio.Event()
    session_holder: list[Any] = []
    error_holder: list[BaseException] = []

    async def _lifetime() -> None:
        """Owner task — opens, holds, then closes the MCP session.

        Both ``stdio_client`` and ``ClientSession`` are entered AND
        exited in this task; anyio's cancel-scope guard is happy.

        We deliberately catch ``Exception`` (not ``BaseException``):
        ``asyncio.CancelledError`` is a ``BaseException`` and must
        propagate so callers (and the asyncio runtime) see the
        cancellation rather than us silently swallowing it.
        """
        try:
            params = StdioServerParameters(
                command=command,
                args=full_args,
                env={**os.environ, **(env or {})},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    session_holder.append(session)
                    ready.set()
                    # Park until close() asks us to exit. Both context
                    # managers exit on the way out — in this task.
                    await done.wait()
        except Exception as exc:  # noqa: BLE001
            error_holder.append(exc)
            # Unblock spawn() so it can re-raise rather than hanging.
            ready.set()

    task = asyncio.create_task(_lifetime(), name=f"chrome-mcp-lifetime[{profile_name or '?'}]")
    await ready.wait()

    if error_holder:
        # Lifetime task already crashed before becoming ready. Wait for
        # it to fully wind down (its except branch already finished, but
        # the task object may still be marked pending for one tick).
        with suppress(BaseException):
            await task
        exc = error_holder[0]
        if isinstance(exc, ChromeMcpUnavailableError):
            raise exc
        raise ChromeMcpTransportError(
            f"failed to spawn chrome-devtools-mcp ({command} {' '.join(full_args)}): {exc}"
        ) from exc

    session = session_holder[0]
    client = ChromeMcpClient(
        session=session,
        lifetime_task=task,
        done_event=done,
        pid=None,
    )
    try:
        await client.list_tools()
    except ChromeMcpTransportError:
        await client.close()
        raise
    return client
