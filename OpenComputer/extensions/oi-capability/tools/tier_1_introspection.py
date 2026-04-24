"""Tier 1 — Introspection tools (8 tools, read-only, lowest risk).

Tools:
  1. read_file_region     — Read a file slice (offset + length)
  2. list_app_usage       — Recently-active apps (last N hours)
  3. read_clipboard_once  — Single clipboard read, never streamed
  4. screenshot           — Screen capture, base64 PNG
  5. extract_screen_text  — OCR screen text via Tesseract
  6. list_recent_files    — Files modified in last N hours
  7. search_files         — aifs-backed file search
  8. read_git_log         — git log parser (INLINE — no OI subprocess needed)

OI method mappings per oi-source-map.md:
  - read_file_region    → computer.files.read (custom slice wrapper)
  - list_app_usage      → computer.terminal.run("shell", "ps aux | grep ...")
  - read_clipboard_once → computer.clipboard.view()
  - screenshot          → computer.display.view()
  - extract_screen_text → computer.display.ocr()
  - list_recent_files   → computer.terminal.run("shell", "find ... -newer ...")
  - search_files        → computer.files.search()
  - read_git_log        → inline git log (no OI dependency — design §11.4 carve-out)
"""

from __future__ import annotations

import subprocess
from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

from ..subprocess.wrapper import OISubprocessWrapper


class ReadFileRegionTool(BaseTool):
    """Read a region of a file (offset + length). Never reads the whole file by default."""

    consent_tier: int = 1
    parallel_safe: bool = True

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_file_region",
            description=(
                "Read a region of a file specified by byte offset and length. "
                "Returns the file slice as a string. Never reads the whole file by default. "
                "Platform: all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"},
                    "offset": {"type": "integer", "description": "Byte offset to start reading (default: 0)", "default": 0},
                    "length": {"type": "integer", "description": "Number of bytes to read (default: 4096)", "default": 4096},
                },
                "required": ["path"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5
        # if self._consent_gate: await self._consent_gate.require(scope="oi.tier1.read_file_region", ...)

        try:
            result = await self._wrapper.call("computer.files.read", call.arguments)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        # if self._audit: self._audit.append(actor="oi-capability", action="read_file_region", ...)

        return ToolResult(tool_call_id=call.id, content=str(result))


class ListAppUsageTool(BaseTool):
    """List recently-active apps in the last N hours."""

    consent_tier: int = 1
    parallel_safe: bool = True

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_app_usage",
            description=(
                "List recently-active applications in the last N hours. "
                "Returns a list of app names and last-seen timestamps. "
                "Platform: macOS, Linux."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "Look-back window in hours (default: 8)", "default": 8},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        try:
            result = await self._wrapper.call(
                "computer.terminal.run",
                {"language": "shell", "code": "ps aux | sort -k10 -rn | head -30"},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


class ReadClipboardOnceTool(BaseTool):
    """Read clipboard contents once (never streamed)."""

    consent_tier: int = 1
    parallel_safe: bool = False  # clipboard is a singleton

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_clipboard_once",
            description=(
                "Read the current clipboard contents once. "
                "Never streams or polls — single read only. "
                "May contain sensitive data. Platform: all."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        try:
            result = await self._wrapper.call("computer.clipboard.view", {})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


class ScreenshotTool(BaseTool):
    """Capture a screenshot, returned as base64-encoded PNG."""

    consent_tier: int = 1
    parallel_safe: bool = True

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="screenshot",
            description=(
                "Capture a screenshot of the current screen. "
                "Returns base64-encoded PNG. May contain sensitive on-screen data. "
                "Platform: all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "quadrant": {
                        "type": "string",
                        "description": "Optional screen quadrant to capture: 'top-left', 'top-right', 'bottom-left', 'bottom-right'",
                        "enum": ["top-left", "top-right", "bottom-left", "bottom-right"],
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        params: dict[str, Any] = {}
        if "quadrant" in call.arguments:
            params["quadrant"] = call.arguments["quadrant"]

        try:
            result = await self._wrapper.call("computer.display.view", params)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


class ExtractScreenTextTool(BaseTool):
    """Extract text from the screen via OCR (Tesseract)."""

    consent_tier: int = 1
    parallel_safe: bool = True

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="extract_screen_text",
            description=(
                "Extract all visible text from the screen using OCR (Tesseract). "
                "Returns plain text. Requires Tesseract installed on the system. "
                "Platform: all."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        try:
            result = await self._wrapper.call("computer.display.ocr", {})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


class ListRecentFilesTool(BaseTool):
    """List files modified in the last N hours."""

    consent_tier: int = 1
    parallel_safe: bool = True

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_recent_files",
            description=(
                "List files modified in the last N hours in the specified directory. "
                "Returns a list of file paths sorted by modification time (newest first). "
                "Platform: macOS, Linux."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "Look-back window in hours (default: 8)", "default": 8},
                    "directory": {"type": "string", "description": "Directory to search (default: home dir)", "default": "~"},
                    "limit": {"type": "integer", "description": "Max results to return (default: 50)", "default": 50},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        hours = call.arguments.get("hours", 8)
        directory = call.arguments.get("directory", "~")
        limit = call.arguments.get("limit", 50)
        cmd = f"find {directory} -newer /tmp/.oc_ref -type f 2>/dev/null | head -{limit}"
        minutes = int(hours) * 60

        # Use mmin for cross-platform compatibility
        cmd = (
            f"find {directory} -mmin -{minutes} -type f 2>/dev/null "
            f"| xargs ls -lt 2>/dev/null | head -{limit}"
        )

        try:
            result = await self._wrapper.call(
                "computer.terminal.run",
                {"language": "shell", "code": cmd},
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


class SearchFilesTool(BaseTool):
    """Search files by query string using aifs-backed OI file search."""

    consent_tier: int = 1
    parallel_safe: bool = True

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="search_files",
            description=(
                "Search for files by name or content using a query string. "
                "Backed by aifs (AI File Search). Returns matching file paths. "
                "Platform: all."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"},
                    "directory": {"type": "string", "description": "Directory to search (default: home)", "default": "~"},
                },
                "required": ["query"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        try:
            result = await self._wrapper.call("computer.files.search", call.arguments)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


class ReadGitLogTool(BaseTool):
    """Read git log for a repository. INLINE — does NOT use OI subprocess.

    Design §11.4 carve-out: git log is a simple shell call that needs no
    OI dependencies. Implemented inline to avoid OI subprocess overhead and
    to guarantee zero AGPL exposure for this tool specifically.
    """

    consent_tier: int = 1
    parallel_safe: bool = True

    def __init__(
        self,
        *,
        wrapper: OISubprocessWrapper,  # accepted but unused — carve-out tool
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._wrapper = wrapper  # kept for constructor uniformity (Phase 5 interweaving)
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_git_log",
            description=(
                "Read the git commit log for a repository. "
                "Returns formatted log entries. Implemented inline (no OI subprocess). "
                "Platform: all (requires git installed)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "Path to the git repository (default: current dir)", "default": "."},
                    "limit": {"type": "integer", "description": "Number of commits to return (default: 20)", "default": 20},
                    "format": {"type": "string", "description": "Log format string (default: oneline)", "default": "oneline"},
                    "branch": {"type": "string", "description": "Branch or ref to read (default: HEAD)", "default": "HEAD"},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        repo_path = call.arguments.get("repo_path", ".")
        limit = call.arguments.get("limit", 20)
        fmt = call.arguments.get("format", "oneline")
        branch = call.arguments.get("branch", "HEAD")

        # Inline implementation — no OI subprocess
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "log", f"--{fmt}", f"-{limit}", branch],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"git log error: {result.stderr.strip()}",
                    is_error=True,
                )
            output = result.stdout
        except FileNotFoundError:
            return ToolResult(
                tool_call_id=call.id,
                content="git not found — install git and ensure it is on PATH",
                is_error=True,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_call_id=call.id,
                content="git log timed out after 30 s",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=output)


ALL_TOOLS = [
    ReadFileRegionTool,
    ListAppUsageTool,
    ReadClipboardOnceTool,
    ScreenshotTool,
    ExtractScreenTextTool,
    ListRecentFilesTool,
    SearchFilesTool,
    ReadGitLogTool,
]

__all__ = [
    "ReadFileRegionTool",
    "ListAppUsageTool",
    "ReadClipboardOnceTool",
    "ScreenshotTool",
    "ExtractScreenTextTool",
    "ListRecentFilesTool",
    "SearchFilesTool",
    "ReadGitLogTool",
    "ALL_TOOLS",
]
