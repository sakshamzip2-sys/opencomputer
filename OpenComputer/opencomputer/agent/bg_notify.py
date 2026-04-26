"""Background-process auto-notification — Round 2B P-8.

When a background process started via ``StartProcess`` exits, the harness
fires a :class:`~plugin_sdk.hooks.HookEvent.NOTIFICATION` hook carrying a
:class:`BgProcessExit` payload. The default subscriber registered in this
module appends a formatted system message to a per-session pending store;
the agent loop drains that store between turns and surfaces the messages
to the model so it can react to long-running work completing.

Why a side-channel instead of the normal Notification path?
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``PushNotificationTool`` already uses the ``Notification`` hook for
user-facing alerts (Telegram silent / loud). Background-process exits are
a different audience — they're *for the agent itself*, not the user.
Conflating them on the same delivery path means a Telegram subscriber
would buzz the user every time `npm run dev` recompiles. Keeping the
side-channel separate (a session-keyed pending store) lets channel
adapters opt in / out cleanly without filtering payload shapes.

API surface
~~~~~~~~~~~

* :class:`BgProcessExit` — frozen payload carried inside the hook ctx.
* :func:`set_session_id_provider` — wired by ``AgentLoop.__init__`` so
  ``StartProcess`` can stamp the active session onto the watcher task.
* :func:`add_pending` / :func:`consume_pending` — the per-session store.
* :func:`format_system_message` — render a payload into the system text.
* :func:`build_default_subscriber_spec` — returns a ``HookSpec`` the
  coding-harness plugin registers at activation.

Concurrency
~~~~~~~~~~~

The pending store is touched by the watcher task (which runs on the
agent's event loop) and by the agent loop itself between turns. Hold a
single ``threading.Lock`` around every read/write — contention is
trivial (small per-session lists) and a finer-grained lock would be
premature.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

_log = logging.getLogger("opencomputer.agent.bg_notify")


# ─── Payload ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BgProcessExit:
    """Carried by the Notification hook when a bg process exits.

    Stashed on :class:`~plugin_sdk.hooks.HookContext` via the otherwise-
    unused ``message`` slot — see :func:`make_hook_context` for the
    encoding rule. Subscribers decode by checking ``message.name ==
    BG_PROCESS_EXIT_MARKER`` then parsing ``message.content``.
    """

    session_id: str
    tool_call_id: str
    """The id of the original ``StartProcess`` call that spawned this proc."""

    exit_code: int
    tail_stdout: str
    """Last 200 chars of stdout (post-decode, post-rstrip joined by ``\\n``)."""

    tail_stderr: str
    """Last 200 chars of stderr."""

    duration_seconds: float


#: Marker on ``Message.name`` so subscribers can disambiguate bg-exit
#: notifications from PushNotificationTool's user-facing payloads.
BG_PROCESS_EXIT_MARKER = "_bg_process_exit"


def make_hook_context(payload: BgProcessExit) -> HookContext:
    """Render a ``BgProcessExit`` into a ``HookContext`` suitable for firing.

    Uses a JSON-encoded ``Message`` payload because ``HookContext`` is a
    frozen dataclass with no extension point. Subscribers detect this
    encoding by checking ``ctx.message.name == BG_PROCESS_EXIT_MARKER``;
    other Notification subscribers (Telegram mirroring etc.) skip on the
    marker mismatch so they don't see structured noise.
    """
    import json

    from plugin_sdk.core import Message

    body = json.dumps(
        {
            "tool_call_id": payload.tool_call_id,
            "exit_code": payload.exit_code,
            "tail_stdout": payload.tail_stdout,
            "tail_stderr": payload.tail_stderr,
            "duration_seconds": payload.duration_seconds,
        },
        separators=(",", ":"),
    )
    return HookContext(
        event=HookEvent.NOTIFICATION,
        session_id=payload.session_id,
        message=Message(
            role="system",
            content=body,
            name=BG_PROCESS_EXIT_MARKER,
        ),
    )


def decode_payload(ctx: HookContext) -> BgProcessExit | None:
    """Inverse of :func:`make_hook_context`. Returns ``None`` on mismatch.

    Defensive: malformed JSON, missing fields, or a non-``Notification``
    event all return ``None`` rather than raising — Notification hooks
    are observation-only and the wider subscriber chain must not crash
    on a bogus payload.
    """
    import json

    if ctx.event is not HookEvent.NOTIFICATION:
        return None
    msg = ctx.message
    if msg is None or msg.name != BG_PROCESS_EXIT_MARKER:
        return None
    try:
        data = json.loads(msg.content)
    except (json.JSONDecodeError, TypeError):
        return None
    try:
        return BgProcessExit(
            session_id=ctx.session_id,
            tool_call_id=str(data["tool_call_id"]),
            exit_code=int(data["exit_code"]),
            tail_stdout=str(data.get("tail_stdout", "")),
            tail_stderr=str(data.get("tail_stderr", "")),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


# ─── Session-id plumbing ───────────────────────────────────────────────
#
# ``_session_id_provider`` is the active agent's session-id getter, or a
# stub returning ``""`` when no agent is running. Wired by
# :class:`opencomputer.agent.loop.AgentLoop` on construction (mirrors the
# ``MemoryContext.session_id_provider`` pattern). ``StartProcessTool``
# calls :func:`current_session_id` at execute time to stamp the watcher
# task — we capture at *start* time, not exit time, so a session that
# ends mid-run still routes the eventual exit notification to the
# original session (by id) rather than dropping it.


def _default_session_id_provider() -> str:
    return ""


_session_id_provider: Callable[[], str] = _default_session_id_provider


def set_session_id_provider(fn: Callable[[], str]) -> None:
    """Install the agent loop's session-id getter. Idempotent across loops."""
    global _session_id_provider
    _session_id_provider = fn


def current_session_id() -> str:
    """Return the active session id, or ``""`` when no provider is wired."""
    try:
        return _session_id_provider() or ""
    except Exception:  # noqa: BLE001 — never let a broken provider crash callers
        _log.debug("bg_notify session_id_provider raised", exc_info=True)
        return ""


# ─── Pending-message store ─────────────────────────────────────────────


_pending: dict[str, list[str]] = {}
_lock = threading.Lock()


def add_pending(session_id: str, text: str) -> None:
    """Stash a system-message body for ``session_id``.

    Empty session ids and empty text are dropped silently — neither carries
    actionable info for the agent loop.
    """
    if not session_id or not text:
        return
    with _lock:
        _pending.setdefault(session_id, []).append(text)


def consume_pending(session_id: str) -> list[str]:
    """Pop and return all pending messages for ``session_id`` (FIFO)."""
    if not session_id:
        return []
    with _lock:
        return _pending.pop(session_id, [])


def has_pending(session_id: str) -> bool:
    """True if any messages are pending for ``session_id``. Does not consume."""
    if not session_id:
        return False
    with _lock:
        return bool(_pending.get(session_id))


def reset_pending() -> None:
    """Clear every pending entry. Tests use this between runs."""
    with _lock:
        _pending.clear()


# ─── System-message formatting ─────────────────────────────────────────


def format_system_message(payload: BgProcessExit) -> str:
    """Render the system-reminder body the agent will see on its next turn.

    Format pinned by tests so the contract stays observable. The trailing
    tail is truncated to 100 chars per the P-8 plan to keep the
    reminder body short — the model can call ``CheckOutput`` for the
    full buffer if it wants more.
    """
    tail = payload.tail_stdout[-100:] if payload.tail_stdout else ""
    return (
        f"[bg-process #{payload.tool_call_id} exited "
        f"code={payload.exit_code}] tail: {tail}"
    )


# ─── Default subscriber ────────────────────────────────────────────────


async def _default_handler(ctx: HookContext) -> HookDecision | None:
    """Notification-hook handler that stashes bg-process exits.

    Returns ``None`` (= "pass") in every case: this is a fire-and-forget
    observation handler. Decoding failures and unknown payloads are
    silent so the wider Notification subscriber chain isn't spammed.
    """
    payload = decode_payload(ctx)
    if payload is None:
        return None
    body = format_system_message(payload)
    add_pending(payload.session_id, body)
    return None


def build_default_subscriber_spec() -> HookSpec:
    """Return the ``HookSpec`` the coding-harness plugin registers.

    Single Notification subscriber, ``fire_and_forget=True`` because the
    drain happens via the agent-loop between-turns checkpoint — the
    handler itself is just a stash.
    """
    return HookSpec(
        event=HookEvent.NOTIFICATION,
        handler=_default_handler,
        matcher=None,
        fire_and_forget=True,
    )


# ─── Helpers used by StartProcessTool ──────────────────────────────────


def tail_chars(buf_lines: list[str], limit: int = 200) -> str:
    """Join ``buf_lines`` with ``\\n`` and return the last ``limit`` chars.

    Helper so :class:`opencomputer.extensions.coding_harness.tools.background.
    StartProcessTool` doesn't need to import json or duplicate the truncation
    rule. Empty input → empty string.
    """
    if not buf_lines:
        return ""
    joined = "\n".join(buf_lines)
    if len(joined) <= limit:
        return joined
    return joined[-limit:]


# Reserved for the agent loop's between-turns drain. Kept here so the
# loop import path is a single module instead of pulling in coding-harness
# internals.
def drain_for_session(session_id: str) -> list[Any]:
    """Return pending bg-exit system messages for ``session_id`` and clear them.

    Wraps :func:`consume_pending` so the agent loop can call exactly one
    function per turn boundary; future refactors that change the storage
    shape only need to update this seam.
    """
    return list(consume_pending(session_id))


__all__ = [
    "BG_PROCESS_EXIT_MARKER",
    "BgProcessExit",
    "add_pending",
    "build_default_subscriber_spec",
    "consume_pending",
    "current_session_id",
    "decode_payload",
    "drain_for_session",
    "format_system_message",
    "has_pending",
    "make_hook_context",
    "reset_pending",
    "set_session_id_provider",
    "tail_chars",
]
