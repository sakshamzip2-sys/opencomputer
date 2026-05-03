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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
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
    """

    def __init__(
        self,
        *,
        session: Any,  # mcp.ClientSession
        cleanup: Any,  # async cleanup callable
        pid: int | None = None,
    ) -> None:
        self._session = session
        self._cleanup = cleanup
        self._pid = pid
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def closed(self) -> bool:
        return self._closed

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


@contextlib.asynccontextmanager
async def _spawn_session(
    *,
    command: str,
    args: list[str],
    env: dict[str, str] | None,
) -> AsyncIterator[tuple[Any, int | None]]:
    """Open the MCP session against the child process; yield (session, pid)."""
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        raise ChromeMcpUnavailableError(
            "mcp Python SDK not installed — install with `pip install opencomputer[browser]`"
        ) from exc

    params = StdioServerParameters(
        command=command,
        args=args,
        env={**os.environ, **(env or {})},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            pid: int | None = None
            yield session, pid


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

    cm = _spawn_session(command=command, args=full_args, env=env)
    aiter = cm.__aenter__()
    try:
        session, pid = await aiter
    except ChromeMcpUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ChromeMcpTransportError(
            f"failed to spawn chrome-devtools-mcp ({command} {' '.join(full_args)}): {exc}"
        ) from exc

    async def _cleanup() -> None:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)

    client = ChromeMcpClient(session=session, cleanup=_cleanup, pid=pid)
    try:
        await client.list_tools()
    except ChromeMcpTransportError:
        await client.close()
        raise
    return client
