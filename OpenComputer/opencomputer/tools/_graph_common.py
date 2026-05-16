"""Shared plumbing for the Microsoft Graph agent tools.

Build-chunk 3 of Milestone 3. The three Graph tools — :class:`GraphSendMailTool`
(``graph_mail.py``), :class:`GraphListCalendarTool` (``graph_calendar.py``) and
:class:`GraphListDriveFilesTool` (``graph_drive.py``) — all need the same three
things:

1. **Acquire a token.** Synchronously call
   :func:`opencomputer.auth.graph_oauth.get_valid_access_token` — that module is
   *synchronous* and its refresh path makes a blocking HTTP call, so it is run
   on a worker thread via :func:`asyncio.to_thread` to keep the agent's event
   loop free.
2. **Map a failure to a clean :class:`ToolResult`.** ``GraphOAuthError`` (not
   logged in / refresh failed) and the three ``GraphError`` subclasses
   (transport / API / base) are turned into ``ToolResult(is_error=True)`` with a
   human-readable message — a token value is *never* interpolated into it.
3. **Refuse when not logged in.** Every Graph tool's ``execute`` first checks
   :func:`opencomputer.auth.graph_oauth.has_stored_token`; when no token is
   stored it returns :data:`NOT_AUTHENTICATED_MESSAGE` without any network call.

This module deliberately holds *no* tool-specific logic — each tool keeps its
own schema, validation and result formatting. What lives here is the wiring
that would otherwise be copy-pasted three times.

The 401 → force-refresh → retry-once policy is *not* implemented here as a
blanket helper, because the send tool must **never** retry (re-POSTing
``sendMail`` risks a duplicate email) while the read tools must. The read tools
use :func:`run_read_with_401_retry`; the send tool calls :func:`acquire_token`
directly and runs its single, un-retried request itself.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from opencomputer.auth.graph_oauth import (
    GraphOAuthError,
    get_valid_access_token,
    has_stored_token,
)
from opencomputer.integrations.graph.client import (
    GraphAPIError,
    GraphClient,
    GraphError,
    GraphTransportError,
)
from plugin_sdk.core import ToolCall, ToolResult

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

#: HTTP status that means "the access token was rejected" — the read tools
#: respond by force-refreshing the token once and retrying.
_HTTP_UNAUTHORIZED = 401

#: Returned (verbatim) by every Graph tool when no token is stored. Phrased as
#: a direct instruction so the agent surfaces the exact fix to the user.
NOT_AUTHENTICATED_MESSAGE = (
    "Microsoft Graph is not connected. Run `oc auth login graph` to sign in "
    "with your Microsoft account, then try again."
)


class GraphToolError(Exception):
    """Internal control-flow signal carrying a ready-made error string.

    Raised by :func:`acquire_token` (and caught by the read-tool runner /
    :func:`error_result`) so a not-logged-in or OAuth failure becomes a clean
    :class:`ToolResult` without leaking a token or a stack trace to the agent.
    Never escapes a tool's ``execute``.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def tool_available() -> bool:
    """Return whether the Microsoft Graph tools should be usable.

    ``True`` once the user has run ``oc auth login graph`` (a token is stored).
    Used both to gate registration in ``cli._register_builtin_tools`` and as the
    first check inside each tool's ``execute`` (defence in depth — a tool
    registered while logged in then logged out still refuses cleanly).
    """
    try:
        return has_stored_token()
    except Exception:  # noqa: BLE001 - a corrupt token store must not crash a tool
        logger.warning(
            "Microsoft Graph token-store check failed; treating Graph tools "
            "as unavailable",
            exc_info=True,
        )
        return False


async def acquire_token(*, force_refresh: bool = False) -> str:
    """Return a valid Microsoft Graph access token, off the event loop.

    :mod:`opencomputer.auth.graph_oauth` is synchronous and its refresh path
    makes a blocking HTTP request, so the call is dispatched to a worker thread.

    Args:
        force_refresh: Forwarded to ``get_valid_access_token`` — set it to mint
            a fresh token after a Graph ``401`` rejected the cached one.

    Raises:
        GraphToolError: If the user is not logged in or a needed refresh failed.
            The exception's message is a clean, user-facing string with no
            token value in it.
    """
    try:
        return await asyncio.to_thread(
            get_valid_access_token, force_refresh=force_refresh
        )
    except GraphOAuthError as exc:
        # graph_oauth raises this both for "not logged in" and for a failed
        # refresh. Its message is already user-facing and token-free; for the
        # not-logged-in case prefer our canonical instruction.
        message = str(exc)
        if "not logged in" in message.lower():
            raise GraphToolError(NOT_AUTHENTICATED_MESSAGE) from exc
        raise GraphToolError(f"Microsoft Graph authentication failed: {message}") from exc


def describe_graph_error(exc: GraphError) -> str:
    """Render a :class:`GraphError` as a concise, user-facing message.

    Tokens never appear in :class:`GraphError` messages (the client builds them
    from the HTTP status + the Graph error envelope only), so the string is
    safe to surface. A few common statuses get a friendlier hint.
    """
    if isinstance(exc, GraphTransportError):
        return f"Could not reach Microsoft Graph: {exc}"
    if isinstance(exc, GraphAPIError):
        status = exc.status_code
        detail = exc.error_message or exc.raw_body or "(no detail)"
        if status == _HTTP_UNAUTHORIZED:
            return (
                "Microsoft Graph rejected the access token (HTTP 401). The "
                "session may have been revoked — run `oc auth login graph` "
                "to sign in again."
            )
        if status == 403:
            return (
                "Microsoft Graph denied the request (HTTP 403). The signed-in "
                f"account may lack the required permission. Detail: {detail}"
            )
        if status == 429:
            return (
                "Microsoft Graph is rate-limiting requests (HTTP 429). Wait a "
                "short while and try again."
            )
        return f"Microsoft Graph request failed (HTTP {status}): {detail}"
    # GraphError base / any future subclass — surface it without a stack trace.
    return f"Microsoft Graph error: {exc}"


def error_result(call: ToolCall, exc: Exception) -> ToolResult:
    """Map any exception a Graph tool may hit to an error :class:`ToolResult`.

    Handles the internal :class:`GraphToolError` (already a finished message),
    the :class:`GraphError` family (rendered via :func:`describe_graph_error`),
    and — as a last-resort backstop so ``execute`` can satisfy its "never raise"
    contract — any other ``Exception``.
    """
    if isinstance(exc, GraphToolError):
        return ToolResult(tool_call_id=call.id, content=exc.message, is_error=True)
    if isinstance(exc, GraphError):
        return ToolResult(
            tool_call_id=call.id,
            content=describe_graph_error(exc),
            is_error=True,
        )
    # Unexpected — log with the traceback for the developer, return a terse,
    # token-free message to the agent.
    logger.exception("Unexpected error in a Microsoft Graph tool")
    return ToolResult(
        tool_call_id=call.id,
        content=f"Microsoft Graph tool failed unexpectedly: {type(exc).__name__}: {exc}",
        is_error=True,
    )


async def run_read_with_401_retry[T](
    operation: Callable[[GraphClient], Awaitable[_T]],
) -> _T:
    """Run a Graph **read** operation, refreshing once on a 401.

    Acquires a token, opens a :class:`GraphClient`, and runs ``operation``. If
    Graph answers ``401`` (the cached access token was rejected), the token is
    force-refreshed and the operation retried **exactly once** with a brand-new
    client. Any second failure — or any non-401 error — propagates.

    This is for the calendar / drive list tools only. The send tool must not be
    routed through here: it acquires its token via :func:`acquire_token` and
    runs its single request itself so a send is never replayed.

    Args:
        operation: A coroutine factory taking a live :class:`GraphClient` and
            performing one read (e.g. ``lambda c: c.calendar.list(...)``).

    Returns:
        Whatever ``operation`` returns.

    Raises:
        GraphToolError: Propagated from :func:`acquire_token`.
        GraphError: Propagated from the Graph call (after the one retry, for a
            persistent 401; immediately for any other error).
    """
    token = await acquire_token()
    try:
        async with GraphClient(token) as client:
            return await operation(client)
    except GraphAPIError as exc:
        if exc.status_code != _HTTP_UNAUTHORIZED:
            raise
        logger.info(
            "Microsoft Graph returned 401; force-refreshing the token and "
            "retrying the read once"
        )

    # Single retry with a freshly-minted token. A failure here is final.
    refreshed = await acquire_token(force_refresh=True)
    async with GraphClient(refreshed) as client:
        return await operation(client)


__all__ = [
    "NOT_AUTHENTICATED_MESSAGE",
    "GraphToolError",
    "acquire_token",
    "describe_graph_error",
    "error_result",
    "run_read_with_401_retry",
    "tool_available",
]
