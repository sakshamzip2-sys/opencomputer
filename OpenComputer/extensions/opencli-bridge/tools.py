"""Five ``BaseTool`` wrappers that surface OpenCLI to the LLM.

Tool selection priority is encoded directly in the descriptions so the
model picks correctly without needing prompt-engineering scaffolding:

  * ``OpenCliList``      → discover what 100+ adapters exist
  * ``OpenCliRun``       → run a deterministic adapter (zero LLM tokens)
  * ``OpenCliBrowse``    → live browser ops via the chrome.debugger
                           extension when no adapter exists
  * ``OpenCliAuthor``    → crystallize a browse session into a reusable
                           adapter so next time is free
  * ``OpenCliInspect``   → introspect an adapter's source/recon notes

All five run the underlying ``opencli`` Node CLI through ``dispatcher.py``,
which manages the HOME-shim for per-OC-profile state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import dispatcher  # type: ignore[import-not-found]

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.opencli_bridge.tools")


def _format_result(payload: Any) -> str:
    """JSON-pretty-print or pass strings through unchanged."""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)


def _is_error_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and "error" in payload


async def _run_in_thread(fn, *args, **kwargs) -> Any:
    """Park sync subprocess work off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


class OpenCliList(BaseTool):
    """List all registered OpenCLI adapters (100+ built-in + locally authored)."""

    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="OpenCliList",
            description=(
                "List every available OpenCLI site/adapter. PREFER calling this "
                "FIRST whenever a user asks for web data, before reaching for "
                "any browser tool. Returns a structured catalog of sites and "
                "their commands. If the target site is in the catalog, use "
                "OpenCliRun (zero LLM tokens). If not, use OpenCliBrowse + "
                "OpenCliAuthor to crystallize a new adapter."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional substring to filter sites by name.",
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        flt = (call.arguments or {}).get("filter")
        try:
            payload = await _run_in_thread(dispatcher.list_adapters)
        except Exception as exc:  # noqa: BLE001
            _log.exception("OpenCliList failed")
            return ToolResult(
                tool_call_id=call.id,
                content=f"OpenCliList failed: {exc}",
                is_error=True,
            )
        if _is_error_payload(payload):
            return ToolResult(
                tool_call_id=call.id,
                content=_format_result(payload),
                is_error=True,
            )
        if flt:
            payload = _filter_catalog(payload, flt)
        return ToolResult(tool_call_id=call.id, content=_format_result(payload))


class OpenCliRun(BaseTool):
    """Run a deterministic OpenCLI adapter — zero LLM tokens at runtime."""

    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="OpenCliRun",
            description=(
                "Run a pre-built site adapter. PREFERRED for any web data task "
                "where the site is in OpenCliList output. Returns clean JSON, "
                "consumes ZERO LLM tokens at runtime, deterministic across "
                "runs. If this returns 'adapter_not_found', do NOT just fall "
                "back to live browsing — use OpenCliBrowse + OpenCliAuthor to "
                "crystallize a new adapter. Browsing without persisting wastes "
                "tokens forever after."
            ),
            parameters={
                "type": "object",
                "required": ["site", "command"],
                "properties": {
                    "site": {
                        "type": "string",
                        "description": "Site identifier from OpenCliList (e.g. 'hackernews').",
                    },
                    "command": {
                        "type": "string",
                        "description": "Command on that site (e.g. 'top').",
                    },
                    "args": {
                        "type": "object",
                        "description": (
                            "Adapter arguments as key/value. Each key becomes "
                            "--<key> <value>. Booleans become bare --<key> "
                            "flags. Use the OpenCliInspect tool first if "
                            "you're unsure what args this adapter accepts."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Override default 90s timeout.",
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        a = call.arguments or {}
        site = a.get("site")
        command = a.get("command")
        if not site or not command:
            return ToolResult(
                tool_call_id=call.id,
                content="OpenCliRun requires both 'site' and 'command'.",
                is_error=True,
            )
        argv = [site, command]
        for k, v in (a.get("args") or {}).items():
            argv.extend(_kv_to_flags(k, v))
        try:
            payload = await _run_in_thread(
                dispatcher.run_opencli, argv, timeout=a.get("timeout")
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("OpenCliRun failed")
            return ToolResult(
                tool_call_id=call.id,
                content=f"OpenCliRun failed: {exc}",
                is_error=True,
            )
        is_err = _is_error_payload(payload)
        if is_err and payload.get("error") in ("nonzero_exit", "binary_not_found"):
            payload.setdefault(
                "hint",
                "If 'adapter_not_found' or unknown site/command, use OpenCliBrowse "
                "+ OpenCliAuthor to author one before answering.",
            )
        return ToolResult(
            tool_call_id=call.id,
            content=_format_result(payload),
            is_error=is_err,
        )


class OpenCliBrowse(BaseTool):
    """Live browser via the chrome.debugger extension — for sites without an adapter."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="OpenCliBrowse",
            description=(
                "Live browser control through the OpenCLI chrome.debugger "
                "extension running inside the agent's own Chrome. Use ONLY "
                "when no OpenCliRun adapter exists, OR when authoring a new "
                "adapter. After 1-2 successful browse sessions on a domain, "
                "you SHOULD call OpenCliAuthor to crystallize the pattern — "
                "the user's procedural memory benefits forever. Common "
                "actions: 'open' (navigate), 'state' (DOM+URL), 'click', "
                "'type', 'fill', 'extract', 'wait', 'eval', 'screenshot', "
                "'tab list', 'tab new', 'close'."
            ),
            parameters={
                "type": "object",
                "required": ["action"],
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "opencli browser sub-command.",
                    },
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Positional + flag args for the action.",
                    },
                    "profile": {
                        "type": "string",
                        "description": "Chrome profile alias (default: agent's profile).",
                    },
                    "tab": {
                        "type": "string",
                        "description": "Specific tab targetId from prior 'tab list'.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Override default 90s timeout.",
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        a = call.arguments or {}
        action = a.get("action")
        if not action:
            return ToolResult(
                tool_call_id=call.id,
                content="OpenCliBrowse requires 'action'.",
                is_error=True,
            )
        try:
            payload = await _run_in_thread(
                dispatcher.run_browser,
                action,
                args=a.get("args"),
                profile=a.get("profile"),
                target=a.get("tab"),
                timeout=a.get("timeout"),
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("OpenCliBrowse failed")
            return ToolResult(
                tool_call_id=call.id,
                content=f"OpenCliBrowse failed: {exc}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=_format_result(payload),
            is_error=_is_error_payload(payload),
        )


class OpenCliAuthor(BaseTool):
    """Initialize + verify a new adapter from a live browse session."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="OpenCliAuthor",
            description=(
                "Crystallize a browse session into a reusable deterministic "
                "adapter. CALL ME after you've done OpenCliBrowse work on a "
                "site, recognized a pattern (list/feed/search/status), and "
                "the user is likely to ask similar questions later. The "
                "adapter is persisted to ~/.opencli/clis/<site>/<command>.js "
                "(per OC profile via HOME-shim) and becomes visible to "
                "OpenCliList immediately after. Two-step: 'init' creates the "
                "scaffold, 'verify' validates it. Use 'init' first."
            ),
            parameters={
                "type": "object",
                "required": ["mode", "site", "command"],
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["init", "verify"],
                        "description": "'init' scaffolds; 'verify' tests.",
                    },
                    "site": {"type": "string"},
                    "command": {"type": "string"},
                    "url": {
                        "type": "string",
                        "description": "Source URL (init only).",
                    },
                    "extra_args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional opencli flags.",
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        a = call.arguments or {}
        mode = a.get("mode")
        site = a.get("site")
        command = a.get("command")
        if mode not in ("init", "verify") or not site or not command:
            return ToolResult(
                tool_call_id=call.id,
                content="OpenCliAuthor requires mode in {init,verify}, site, command.",
                is_error=True,
            )
        target = f"{site}/{command}"
        if mode == "init":
            argv = ["browser", "init", target]
            url = a.get("url")
            if url:
                argv.append(url)
        else:
            argv = ["browser", "verify", target]
        if a.get("extra_args"):
            argv.extend(a["extra_args"])
        try:
            payload = await _run_in_thread(dispatcher.run_opencli, argv, timeout=120.0)
        except Exception as exc:  # noqa: BLE001
            _log.exception("OpenCliAuthor failed")
            return ToolResult(
                tool_call_id=call.id,
                content=f"OpenCliAuthor failed: {exc}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=_format_result(payload),
            is_error=_is_error_payload(payload),
        )


class OpenCliInspect(BaseTool):
    """Show adapter source / recon notes / status — for debugging or learning."""

    parallel_safe = True

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="OpenCliInspect",
            description=(
                "Inspect an adapter's source code, args, recon notes, and "
                "last verify status. Useful when OpenCliRun fails and you "
                "need to understand why, or when authoring a new adapter "
                "and you want to see how a similar one was structured."
            ),
            parameters={
                "type": "object",
                "required": ["site"],
                "properties": {
                    "site": {"type": "string"},
                    "command": {
                        "type": "string",
                        "description": "Optional — if omitted, lists all commands on the site.",
                    },
                },
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        a = call.arguments or {}
        site = a.get("site")
        command = a.get("command")
        if not site:
            return ToolResult(
                tool_call_id=call.id,
                content="OpenCliInspect requires 'site'.",
                is_error=True,
            )
        argv = ["adapter", "status", site if not command else f"{site}/{command}"]
        try:
            payload = await _run_in_thread(dispatcher.run_opencli, argv, timeout=20.0)
        except Exception as exc:  # noqa: BLE001
            _log.exception("OpenCliInspect failed")
            return ToolResult(
                tool_call_id=call.id,
                content=f"OpenCliInspect failed: {exc}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=_format_result(payload),
            is_error=_is_error_payload(payload),
        )


# ─── helpers ──────────────────────────────────────────────────────────


def _kv_to_flags(key: str, value: Any) -> list[str]:
    """Map a Python key/value into opencli CLI flags.

    Booleans → bare ``--<key>`` (only when True).
    Lists → repeated ``--<key> <v>`` (matches opencli convention).
    Anything else → ``--<key> <stringified>``.
    """
    flag = f"--{key}"
    if isinstance(value, bool):
        return [flag] if value else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for v in value:
            out.extend([flag, str(v)])
        return out
    return [flag, str(value)]


def _filter_catalog(payload: Any, needle: str) -> Any:
    """Best-effort substring filter on opencli list output."""
    if isinstance(payload, dict) and "sites" in payload:
        return {
            **payload,
            "sites": [
                s for s in payload["sites"]
                if needle.lower() in str(s).lower()
            ],
        }
    if isinstance(payload, list):
        return [s for s in payload if needle.lower() in str(s).lower()]
    return payload


ALL_TOOL_CLASSES: tuple[type[BaseTool], ...] = (
    OpenCliList,
    OpenCliRun,
    OpenCliBrowse,
    OpenCliAuthor,
    OpenCliInspect,
)


__all__ = [
    "ALL_TOOL_CLASSES",
    "OpenCliList",
    "OpenCliRun",
    "OpenCliBrowse",
    "OpenCliAuthor",
    "OpenCliInspect",
]
