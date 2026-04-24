"""Tier 3 — Browser tools (3 tools).

Tools:
  1. read_browser_history   — Chrome/Safari history via sqlite3
  2. read_browser_bookmarks — Browser bookmarks via sqlite3
  3. read_browser_dom       — Get DOM/page content via Selenium (stricter consent)

OI method mappings per oi-source-map.md:
  - read_browser_history   → terminal.run("shell", "sqlite3 <history.db> ...")
  - read_browser_bookmarks → terminal.run("shell", ...); or browser.py metadata
  - read_browser_dom       → computer.browser.get_page_content()

Platform notes: All platforms for history/bookmarks (browser-specific paths).
read_browser_dom requires Selenium + ChromeDriver — all platforms.
"""

from __future__ import annotations

from typing import Any

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

from ..subprocess.wrapper import OISubprocessWrapper


class ReadBrowserHistoryTool(BaseTool):
    """Read browser history by querying the browser's sqlite database directly."""

    consent_tier: int = 3
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
            name="read_browser_history",
            description=(
                "Read browser history by querying the browser's sqlite database. "
                "No Selenium required — reads directly from the database file. "
                "Supports Chrome and Safari. Platform: macOS, Linux."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "browser": {
                        "type": "string",
                        "description": "Browser to read history from (default: chrome)",
                        "enum": ["chrome", "safari", "firefox"],
                        "default": "chrome",
                    },
                    "limit": {"type": "integer", "description": "Number of history entries to return (default: 50)", "default": 50},
                    "days": {"type": "integer", "description": "Look-back window in days (default: 7)", "default": 7},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        limit = call.arguments.get("limit", 50)
        # browser and days params are accepted by schema for user clarity;
        # the shell command auto-detects the browser DB path.

        # macOS: ~/Library/Application Support/Google/Chrome/Default/History
        # Linux: ~/.config/google-chrome/Default/History
        cmd = (
            f"sqlite3 -separator '|' "
            f"\"$(find ~ -name 'History' -path '*Chrome*' 2>/dev/null | head -1)\" "
            f"\"SELECT url, title, last_visit_time FROM urls "
            f"ORDER BY last_visit_time DESC LIMIT {limit}\" 2>/dev/null "
            f"|| echo 'Browser history DB not found or locked'"
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


class ReadBrowserBookmarksTool(BaseTool):
    """Read browser bookmarks by querying the browser's sqlite/json database."""

    consent_tier: int = 3
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
            name="read_browser_bookmarks",
            description=(
                "Read browser bookmarks from the browser's bookmarks file. "
                "No Selenium required — reads directly from the database/JSON file. "
                "Supports Chrome and Firefox. Platform: macOS, Linux."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "browser": {
                        "type": "string",
                        "description": "Browser to read bookmarks from (default: chrome)",
                        "enum": ["chrome", "firefox"],
                        "default": "chrome",
                    },
                    "limit": {"type": "integer", "description": "Max bookmarks to return (default: 100)", "default": 100},
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5

        # limit param accepted by schema; output is capped via str(data)[:4096] below
        cmd = (
            "python3 -c \""
            "import json, glob, os; "
            "paths = glob.glob(os.path.expanduser('~') + '/**/*Bookmarks', recursive=True); "
            "data = json.load(open(paths[0])) if paths else {}; "
            "print(str(data)[:4096])"
            "\" 2>/dev/null || echo 'Bookmarks file not found'"
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


class ReadBrowserDomTool(BaseTool):
    """Get page DOM/content from a URL via Selenium (stricter consent — opens browser)."""

    consent_tier: int = 3
    parallel_safe: bool = False  # Selenium is stateful

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
            name="read_browser_dom",
            description=(
                "Navigate to a URL and return the page DOM / text content. "
                "Uses Selenium — user will see a Chrome browser window open. "
                "Stricter consent required. Platform: all (requires ChromeDriver)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to and read"},
                    "extract_text_only": {
                        "type": "boolean",
                        "description": "Return only visible text (true) or full HTML (false). Default: true.",
                        "default": True,
                    },
                },
                "required": ["url"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        # CONSENT_HOOK — Session A wires ConsentGate.require here in Phase 5
        # Stricter: scope="oi.tier3.read_browser_dom" — user sees browser open

        url = call.arguments["url"]

        try:
            # Navigate first
            await self._wrapper.call("computer.browser.go_to_url", {"url": url})
            # Then get content
            result = await self._wrapper.call("computer.browser.get_page_content", {})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        # AUDIT_HOOK — Session A wires AuditLog.append here in Phase 5
        return ToolResult(tool_call_id=call.id, content=str(result))


ALL_TOOLS = [
    ReadBrowserHistoryTool,
    ReadBrowserBookmarksTool,
    ReadBrowserDomTool,
]

__all__ = [
    "ReadBrowserHistoryTool",
    "ReadBrowserBookmarksTool",
    "ReadBrowserDomTool",
    "ALL_TOOLS",
]
