"""OC tool wrappers around the lifted Hermes dispatcher.

Each ``BaseTool`` subclass here wraps one ``browser_*`` dispatcher function
(``browser_navigate``, ``browser_snapshot``, ``browser_click``, etc.) and
exposes it to OC's agent loop with a typed ``ToolSchema``.

Why separate from the lifted ``browser_tool.py``:

  * The dispatcher is byte-identical to Hermes upstream — keeping the
    OC-specific tool-shape adapter in its own file makes upstream re-syncs
    trivial. ``browser_tool.py`` never imports from OC; ``tools.py`` is
    where the OC contract meets the lifted code.

  * The dispatcher returns JSON strings; OC's ``ToolResult`` wants a
    string in ``.content``. Pass the JSON string through directly so
    downstream agents (and the Recall tool's full-text search) can read
    it. ``is_error=True`` is set when the dispatcher's JSON has
    ``success: false``.

  * The dispatcher is synchronous (it shells out to ``agent-browser`` via
    ``subprocess.Popen``). Wrap each call in ``asyncio.to_thread`` so the
    agent loop's event loop isn't blocked.

Task ID semantics:
    Hermes uses ``task_id`` as a session-isolation key — same id = same
    browser tab/session, lazily created. OC's natural unit is the chat
    session within an active profile. The shim derives a stable id from
    the OC ``RuntimeContext`` (profile + session) when available, falling
    back to ``"default"`` for one-shot non-chat invocations.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# Sibling-module imports — loader puts the plugin dir on sys.path.
# Module is named ``dispatcher`` (not ``browser_tool``) because the OC
# plugin loader doesn't clear ``browser_tool`` from sys.modules between
# plugin loads, and ``dev-tools`` already exposes ``browser_tool`` with a
# different ``BrowserTool`` symbol — leaving them under the same module
# name caused a namespace collision when both plugins were active.
import dispatcher as _bt  # type: ignore[import-not-found]


# ─── shared helpers ──────────────────────────────────────────────────────


def _resolve_task_id(call: ToolCall) -> str:
    """Derive a Hermes-style task_id for browser-session isolation.

    Preference order:
      1. Caller-provided ``task_id`` arg (rare — explicit override).
      2. ``OPENCOMPUTER_BROWSER_TASK_ID`` env var (set by tools.py wrapper
         when a chat session boundary is known).
      3. ``"default"`` — one global session for non-chat / non-scoped uses.

    Hermes uses the task_id only as an isolation key; it doesn't have to
    be human-readable. Keeping it stable across calls inside one OC chat
    turn is what matters.
    """
    explicit = call.arguments.get("task_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    env_scoped = os.environ.get("OPENCOMPUTER_BROWSER_TASK_ID")
    if env_scoped:
        return env_scoped
    return "default"


def _classify_error(json_text: str) -> bool:
    """Return True if the dispatcher's JSON response indicates an error.

    Hermes browser tools return ``{"success": false, "error": "..."}`` on
    error and ``{"success": true, ...}`` on success. We mirror this into
    OC's ``ToolResult.is_error``.
    """
    try:
        parsed = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return False  # treat unparseable as success-ish; agent can read it
    if isinstance(parsed, dict) and parsed.get("success") is False:
        return True
    return False


async def _run_dispatcher(fn, *args, **kwargs) -> str:
    """Invoke a sync ``browser_*`` dispatcher function off the event loop.

    Hermes's dispatcher uses ``subprocess.Popen`` + ``proc.wait()`` for
    each command — fully synchronous. Running it on the OC event loop
    would block other tools. ``asyncio.to_thread`` parks it on the default
    thread executor.

    Cooperative interrupt support: registers a probe with
    ``compat.register_interrupt_probe`` that returns True when the
    current asyncio task has been cancelled. The dispatcher polls
    ``compat.is_interrupted`` from inside its subprocess loop and
    early-exits when the probe fires. Probe is unregistered in a
    finally block so per-call lifecycle is clean.
    """
    import compat as _compat  # type: ignore[import-not-found]

    # The current asyncio task carries its own cancellation flag.
    # Reading it requires being on the event loop; we capture it here
    # (still on the loop) and pass a closure to the worker thread.
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None

    if current is None:
        # No event loop / no task — fall through without interrupt support.
        return await asyncio.to_thread(fn, *args, **kwargs)

    def _probe() -> bool:
        return current.cancelled() or current.cancelling() > 0

    _compat.register_interrupt_probe(_probe)
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    finally:
        _compat.unregister_interrupt_probe(_probe)


# ─── tool wrappers ───────────────────────────────────────────────────────


class BrowserNavigateTool(BaseTool):
    """Drive the browser to a URL.

    Wraps ``browser_tool.browser_navigate`` from the lifted Hermes
    dispatcher. Returns a JSON string with the navigation outcome plus a
    compact accessibility-tree snapshot (so the model can act on the page
    without a separate ``BrowserSnapshot`` call in most workflows).
    """

    parallel_safe = False  # browser sessions are per-task; concurrent navs are an anti-pattern

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="BrowserNavigate",
            description=(
                "Navigate the browser to a URL. Initialises the session and "
                "loads the page. Must be called before other browser tools "
                "for that task. Returns a JSON object with the navigation "
                "result and a compact page snapshot (interactive elements "
                "with ref IDs like @e1, @e2). For simple data retrieval, "
                "prefer plain HTTP fetch (httpx) — use this when you need "
                "to interact with a JS-rendered page or use logged-in "
                "session cookies."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Absolute http(s) URL to navigate to.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": (
                            "Optional session-isolation key. Reuse the same "
                            "value across calls to share one browser tab. "
                            "Leave unset to use the active OC chat scope."
                        ),
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        url = str(call.arguments.get("url", "")).strip()
        if not url:
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({"success": False, "error": "url is required"}),
                is_error=True,
            )
        task_id = _resolve_task_id(call)
        result_json = await _run_dispatcher(_bt.browser_navigate, url, task_id=task_id)
        return ToolResult(
            tool_call_id=call.id,
            content=result_json,
            is_error=_classify_error(result_json),
        )


class BrowserSnapshotTool(BaseTool):
    parallel_safe = True  # read-only

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="BrowserSnapshot",
            description=(
                "Read a text-based accessibility-tree snapshot of the "
                "current page. Returns interactive elements with ref IDs "
                "(@e1, @e2, ...) usable by BrowserClick / BrowserType. "
                "Use ``full=true`` for full content (LLM-summarised when "
                "very large). Requires BrowserNavigate first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "full": {
                        "type": "boolean",
                        "description": (
                            "false (default): compact view with interactive "
                            "elements. true: full page content."
                        ),
                    },
                    "user_task": {
                        "type": "string",
                        "description": (
                            "Optional task hint for content-extraction "
                            "summarisation when full=true and the page is "
                            "very large."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Session-isolation key (see BrowserNavigate).",
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        full = bool(call.arguments.get("full", False))
        user_task = call.arguments.get("user_task")
        if user_task is not None and not isinstance(user_task, str):
            user_task = str(user_task)
        task_id = _resolve_task_id(call)
        result_json = await _run_dispatcher(
            _bt.browser_snapshot, full=full, user_task=user_task, task_id=task_id,
        )
        return ToolResult(
            tool_call_id=call.id,
            content=result_json,
            is_error=_classify_error(result_json),
        )


class BrowserClickTool(BaseTool):
    parallel_safe = False

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="BrowserClick",
            description=(
                "Click an element by its ref ID from a recent BrowserSnapshot "
                "(e.g. ``@e5``). The click happens in the active browser "
                "session for this task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Element ref ID, e.g. ``@e5``.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Session-isolation key.",
                    },
                },
                "required": ["ref"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        ref = str(call.arguments.get("ref", "")).strip()
        if not ref:
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({"success": False, "error": "ref is required"}),
                is_error=True,
            )
        task_id = _resolve_task_id(call)
        result_json = await _run_dispatcher(_bt.browser_click, ref, task_id=task_id)
        return ToolResult(
            tool_call_id=call.id,
            content=result_json,
            is_error=_classify_error(result_json),
        )


class BrowserTypeTool(BaseTool):
    parallel_safe = False

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="BrowserType",
            description=(
                "Focus an input element (by ref ID from BrowserSnapshot) "
                "and type text into it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "description": "Input element ref ID, e.g. ``@e7``.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Session-isolation key.",
                    },
                },
                "required": ["ref", "text"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        ref = str(call.arguments.get("ref", "")).strip()
        text = call.arguments.get("text", "")
        if not ref:
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({"success": False, "error": "ref is required"}),
                is_error=True,
            )
        if not isinstance(text, str):
            text = str(text)
        task_id = _resolve_task_id(call)
        result_json = await _run_dispatcher(_bt.browser_type, ref, text, task_id=task_id)
        return ToolResult(
            tool_call_id=call.id,
            content=result_json,
            is_error=_classify_error(result_json),
        )


class BrowserVisionTool(BaseTool):
    """Visual page understanding via screenshot + vision LLM.

    Hermes ships ``browser_vision`` for tasks that require *seeing* the page
    — canvas-rendered UIs, complex layouts, charts. Currently the underlying
    auxiliary LLM call is stubbed (raises ``CallLLMNotConfigured``) and
    Hermes's call site degrades gracefully: the screenshot is captured and
    saved, the vision analysis returns a "vision unavailable" payload.
    Wiring to OC's ``auxiliary_client`` is a future enhancement.
    """

    parallel_safe = True  # screenshot-only is read-only

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="BrowserVision",
            description=(
                "Take a screenshot of the current page and ask a vision LLM "
                "a question about it. Use for visually-driven tasks where "
                "the accessibility tree is insufficient (canvas, complex "
                "layouts). Note: vision LLM analysis is currently degraded "
                "in OC's port — the screenshot is saved but the LLM call is "
                "not yet wired; the response will indicate this."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Question to ask about the page screenshot.",
                    },
                    "annotate": {
                        "type": "boolean",
                        "description": (
                            "If true, also include the accessibility-tree "
                            "snippet alongside the screenshot for context."
                        ),
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Session-isolation key.",
                    },
                },
                "required": ["question"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        question = str(call.arguments.get("question", "")).strip()
        if not question:
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({"success": False, "error": "question is required"}),
                is_error=True,
            )
        annotate = bool(call.arguments.get("annotate", False))
        task_id = _resolve_task_id(call)
        result_json = await _run_dispatcher(
            _bt.browser_vision, question, annotate=annotate, task_id=task_id,
        )
        return ToolResult(
            tool_call_id=call.id,
            content=result_json,
            is_error=_classify_error(result_json),
        )


# ─── module exports ──────────────────────────────────────────────────────


ALL_TOOL_CLASSES = (
    BrowserNavigateTool,
    BrowserSnapshotTool,
    BrowserClickTool,
    BrowserTypeTool,
    BrowserVisionTool,
)


__all__ = [
    "BrowserNavigateTool",
    "BrowserSnapshotTool",
    "BrowserClickTool",
    "BrowserTypeTool",
    "BrowserVisionTool",
    "ALL_TOOL_CLASSES",
]
