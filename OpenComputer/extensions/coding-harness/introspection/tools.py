"""Native cross-platform introspection tools.

Replaces ``extensions/coding-harness/oi_bridge/tools/tier_1_introspection.py``
which shelled out to an Open Interpreter subprocess. The new tools call native
Python libraries directly:

  * list_app_usage       → psutil
  * read_clipboard_once  → pyperclip
  * screenshot           → mss
  * extract_screen_text  → mss + rapidocr-onnxruntime (see ``ocr.py``)
  * list_recent_files    → os.walk + pathlib + stat

Removing the OI subprocess eliminates the AGPL dependency chain and gives us
true cross-platform support (Windows included where the underlying libs allow).
Capability claims live under the ``introspection.*`` namespace from the start.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, ClassVar

import mss
import mss.tools
import psutil
import pyperclip
from extensions.coding_harness.introspection.ocr import ocr_text_from_screen

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger(__name__)

# Prime psutil's system-wide CPU-percent sampler. Per-PID samplers still need
# their own priming pass — see ``ListAppUsageTool.execute`` which does a
# two-pass walk to make ``cpu_percent`` values meaningful on the first call.
psutil.cpu_percent(interval=None)


def _quadrant_bounds(monitor: dict, quadrant: str) -> dict:
    """Compute the rect for one quadrant of `monitor`, preserving its origin."""
    half_w = monitor["width"] // 2
    half_h = monitor["height"] // 2
    left = monitor["left"]
    top = monitor["top"]
    if quadrant == "top-left":
        return {"left": left, "top": top, "width": half_w, "height": half_h}
    if quadrant == "top-right":
        return {"left": left + half_w, "top": top, "width": half_w, "height": half_h}
    if quadrant == "bottom-left":
        return {"left": left, "top": top + half_h, "width": half_w, "height": half_h}
    if quadrant == "bottom-right":
        return {"left": left + half_w, "top": top + half_h, "width": half_w, "height": half_h}
    return monitor


# Files modified inside any of these directories are noise — system caches,
# package bundles, sandbox data — not what the user means by "files I edited".
_SKIP_DIR_NAMES: frozenset[str] = frozenset({
    # Generic dot/dunder
    ".git", ".hg", ".svn", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # Build / packaging
    "node_modules", "dist", "build", ".next", ".nuxt",
    # macOS bloat
    "Library",  # broad: skips ~/Library entirely; user-edited content lives elsewhere
    "Mail", "Caches", "Containers",  # in case someone passes a non-home root
    # Windows bloat
    "AppData",
})

# Hard cap on files inspected per call. Walks halt early once exceeded;
# the partial sorted result is returned. Tuned for "responsive" (~<1s)
# on developer machines.
_WALK_FILE_BUDGET: int = 50_000


def _walk_recent_files(base: Path, cutoff: float, limit: int) -> list[tuple[float, Path]]:
    """Return [(mtime, path), ...] for files under ``base`` modified after ``cutoff``.

    Skips directories named in ``_SKIP_DIR_NAMES`` and any starting with '.'.
    Returns at most ``limit * 2`` entries (caller sorts + truncates to ``limit``).
    Halts early at ``_WALK_FILE_BUDGET`` files inspected.

    Permission errors during directory traversal are logged at DEBUG level
    rather than swallowed silently — this matters on Windows where locked
    subtrees (System Volume Information, etc.) would otherwise vanish from
    the walk without any signal that results may be incomplete.
    """
    def _onerror(exc: OSError) -> None:
        _log.debug("list_recent_files: skipped subtree due to %s", exc)

    out: list[tuple[float, Path]] = []
    cap = max(limit * 2, limit + 10)
    inspected = 0
    for root, dirs, files in os.walk(base, onerror=_onerror):  # followlinks=False (default) — safe
        # Prune in-place so os.walk doesn't recurse into them
        dirs[:] = [d for d in dirs if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        for fname in files:
            if fname.startswith("."):
                continue
            inspected += 1
            if inspected > _WALK_FILE_BUDGET:
                return out
            p = Path(root) / fname
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > cutoff:
                out.append((m, p))
                if len(out) >= cap:
                    return out
    return out


class ListAppUsageTool(BaseTool):
    """List recently-active apps in the last N hours (psutil-backed)."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.list_app_usage",
            tier_required=ConsentTier.IMPLICIT,
            human_description="List recently-active applications (last N hours).",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_app_usage",
            description=(
                "List recently-active applications on the user's machine over the last "
                "N hours (default 8). Returns a JSON array of {name, cpu_percent, started} "
                "sorted by CPU usage (highest first), capped at 30 entries. Use this when "
                "answering 'what was I doing?' or when tailoring suggestions to current "
                "workflows. CAUTION: process list is personal data; do not echo it to "
                "third parties without consent. Cross-platform via psutil (macOS, Linux, "
                "Windows). Read-only — under F1 ConsentGate (IMPLICIT tier)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours (default: 8)",
                        "default": 8,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        hours = int(call.arguments.get("hours", 8))
        cutoff = time.time() - hours * 3600

        rows: list[dict[str, Any]] = []
        try:
            # psutil's cpu_percent is a delta-since-last-call sample tracked
            # per-PID. The first call for any process returns 0.0 because
            # there's no prior sample to compare against. Walking
            # process_iter once primes those per-PID samplers; a brief
            # asyncio sleep lets the kernel accumulate CPU-time deltas; the
            # second walk then reads meaningful values. ~100 ms latency cost.
            for _ in psutil.process_iter(["cpu_percent"]):
                pass
            await asyncio.sleep(0.1)

            for p in psutil.process_iter(["name", "cpu_percent", "create_time"]):
                info = p.info
                create_time = info.get("create_time") or 0.0
                if create_time < cutoff:
                    continue
                rows.append(
                    {
                        "name": info.get("name") or "<unknown>",
                        "cpu_percent": float(info.get("cpu_percent") or 0.0),
                        "started": create_time,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        rows.sort(key=lambda r: r["cpu_percent"], reverse=True)
        return ToolResult(tool_call_id=call.id, content=json.dumps(rows[:30]))


class ReadClipboardOnceTool(BaseTool):
    """Read clipboard contents once, never streamed (pyperclip-backed)."""

    consent_tier: int = 1
    parallel_safe: bool = False  # clipboard is a singleton
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.read_clipboard_once",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Read clipboard contents once (never streamed).",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_clipboard_once",
            description=(
                "Read the current system clipboard contents — single read only, never "
                "streamed or polled. Use when the user references 'this' / 'what I just "
                "copied' and you need the actual text. CAUTION: clipboards frequently "
                "contain sensitive data (passwords, API keys, addresses); treat the "
                "result as private and do not log, echo to third parties, or include in "
                "unrelated tool calls. Cross-platform via pyperclip — Linux requires "
                "xclip or xsel on PATH (handled at install / verified by `opencomputer "
                "doctor`). Under F1 ConsentGate (IMPLICIT tier). Single-shot semantics "
                "by design — repeated reads require explicit re-invocation."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            text = pyperclip.paste()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return ToolResult(tool_call_id=call.id, content=text)


class ScreenshotTool(BaseTool):
    """Capture a screenshot, returned as base64-encoded PNG (mss-backed)."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.screenshot",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Capture a screenshot of the current screen.",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="screenshot",
            description=(
                "Capture a screenshot of the primary monitor, returned as base64-encoded "
                "PNG. Use when the user asks 'what's on my screen?' or when you need to "
                "verify GUI state. Pass `quadrant` (top-left/top-right/bottom-left/bottom-"
                "right) to capture just one corner — cheaper and less private. CAUTION: "
                "screenshots may contain sensitive on-screen data (passwords, private "
                "chats, financial info); do not include in error messages, third-party "
                "calls, or persistent logs. For text content prefer extract_screen_text "
                "(OCR) — smaller and more privacy-aware. Cross-platform via mss (macOS, "
                "Linux, Windows). Linux requires an X or Wayland display server. Under F1 "
                "ConsentGate (IMPLICIT tier)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "quadrant": {
                        "type": "string",
                        "description": (
                            "Optional screen quadrant to capture: "
                            "'top-left', 'top-right', 'bottom-left', 'bottom-right'"
                        ),
                        "enum": ["top-left", "top-right", "bottom-left", "bottom-right"],
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        quadrant = call.arguments.get("quadrant")
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]  # primary monitor
                if quadrant:
                    monitor = _quadrant_bounds(monitor, quadrant)
                shot = sct.grab(monitor)
                png = mss.tools.to_png(shot.rgb, shot.size)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return ToolResult(tool_call_id=call.id, content=base64.b64encode(png).decode("ascii"))


class ExtractScreenTextTool(BaseTool):
    """Extract text from the screen via OCR (mss + rapidocr-onnxruntime)."""

    consent_tier: int = 1
    # rapidocr-onnxruntime loads ~200MB of model weights per instance; running
    # multiple in parallel causes memory pressure on typical machines.
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.extract_screen_text",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Extract all visible text from the screen using OCR.",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="extract_screen_text",
            description=(
                "Extract visible text from the primary monitor via OCR. Returns plain "
                "text — much smaller and more focused than a screenshot. Use this when "
                "you need to read what an app is showing without grabbing pixel data — "
                "error dialog text, web page contents, terminal output the agent isn't "
                "directly attached to. Prefer extract_screen_text over screenshot when "
                "you only need the words; the OCR cost is paid once and the output is "
                "trivially diff-able. Cross-platform via mss + rapidocr-onnxruntime "
                "(no system Tesseract install required). First call in a process "
                "may take ~5s to load model weights. CAUTION: still extracts whatever's "
                "visible — same privacy concerns as screenshot. Linux requires an X or "
                "Wayland display server. Under F1 ConsentGate (IMPLICIT tier)."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            text = ocr_text_from_screen()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)
        return ToolResult(tool_call_id=call.id, content=text)


class ListRecentFilesTool(BaseTool):
    """List files modified in the last N hours (os.walk + stat)."""

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.list_recent_files",
            tier_required=ConsentTier.IMPLICIT,
            human_description="List files modified in the last N hours.",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_recent_files",
            description=(
                "List files modified in the last N hours under a directory, sorted by "
                "mtime (newest first). Use when the user references 'the file I just "
                "edited' / 'what changed today'. Default look-back is 8 hours, default "
                "directory is `~`, default cap is 50 results — narrow with `directory` "
                "and `hours` for cheaper queries. Returns JSON array of {path, mtime}. "
                "Skips noise dirs (.git, __pycache__, node_modules, .venv, Library, "
                "AppData) and hidden files; hard-caps file inspection at 50,000. "
                "Cross-platform via os.walk + pathlib (macOS, Linux, Windows). Under F1 "
                "ConsentGate (IMPLICIT tier)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours (default: 8)",
                        "default": 8,
                    },
                    "directory": {
                        "type": "string",
                        "description": "Directory to search (default: home dir)",
                        "default": "~",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default: 50)",
                        "default": 50,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        hours = int(call.arguments.get("hours", 8))
        directory = call.arguments.get("directory", "~")
        limit = int(call.arguments.get("limit", 50))

        base = Path(os.path.expanduser(directory))
        if not base.exists() or not base.is_dir():
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: directory not found: {directory}",
                is_error=True,
            )

        cutoff = time.time() - hours * 3600

        try:
            rows = _walk_recent_files(base, cutoff, limit)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"Error: {exc}", is_error=True)

        rows.sort(reverse=True)
        payload = [{"path": str(p), "mtime": m} for m, p in rows[:limit]]
        return ToolResult(tool_call_id=call.id, content=json.dumps(payload))
