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
import uuid
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


# ─── screenshot storage ──────────────────────────────────────────────
#
# Screenshots are written to ``<profile_home>/tool_result_storage/screenshots/``
# rather than returned as base64 in the tool result. A 1080p PNG is ~280 KB
# base64-encoded — that's larger than the entire model context. Surfacing a
# path keeps conversation history small (just the path), lets the Anthropic
# provider re-load the image at request time and pay the per-image vision
# token rate (~1500 tokens), and lets the same screenshot be referenced in
# later turns without compounding cost. See plugin_sdk/core.py
# ``ToolResult.attachments`` for the full design.

_SCREENSHOT_TTL_SECONDS = 24 * 60 * 60  # 24h — older shots get cleaned up


def _resolve_screenshots_dir() -> Path:
    """Return ``<profile_home>/tool_result_storage/screenshots/``.

    Resolved on every call so test fixtures that monkeypatch
    ``OPENCOMPUTER_HOME`` (which ``_home()`` honours) take effect without
    needing to reset module-level state.
    """
    from opencomputer.agent.config import _home  # noqa: PLC0415 — lazy

    return _home() / "tool_result_storage" / "screenshots"


def _prune_stale_screenshots(directory: Path, ttl_seconds: int = _SCREENSHOT_TTL_SECONDS) -> int:
    """Delete screenshots older than ``ttl_seconds``. Returns count removed.

    Best-effort: silently skips files we can't stat or unlink (another
    process holding them, permission issues, race with concurrent
    capture). The cleanup never raises — a busy agent shouldn't crash
    because of housekeeping.
    """
    if not directory.exists():
        return 0
    cutoff = time.time() - ttl_seconds
    removed = 0
    try:
        entries = list(directory.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            # File vanished mid-iteration, or denied — fine.
            continue
    return removed


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


# Basenames to skip ANYWHERE in the tree — these are universally noise:
# version-control metadata, build artifacts, package bundles, virtual envs.
# A user is extremely unlikely to have legitimate content in a dir literally
# named ``__pycache__``.
_SKIP_DIR_NAMES: frozenset[str] = frozenset({
    # Generic dot/dunder
    ".git", ".hg", ".svn", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    # Build / packaging
    "node_modules", "dist", "build", ".next", ".nuxt",
    # macOS Library children — these only ever appear inside ``~/Library``
    # in normal use; skipping them as basenames is safe noise reduction.
    "Mail", "Caches", "Containers",
})

# Directories to skip ONLY when they appear directly inside the user's home.
# Prevents the false-positive case where a user has a project literally named
# ``Library`` (e.g. ``~/Projects/Library/``) — the previous basename-only rule
# silently pruned it. Path-scoped checks make ``~/Library`` (the macOS app
# data dir, real noise) and ``~/Projects/Library`` (real user code) behave
# differently, which is what users expect.
_HOME_SKIP_RELATIVE: frozenset[str] = frozenset({
    "Library",  # macOS application support, mail, caches, containers
    "AppData",  # Windows local + roaming app data
})

# Hard cap on files inspected per call. Walks halt early once exceeded;
# the partial sorted result is returned. Tuned for "responsive" (~<1s)
# on developer machines.
_WALK_FILE_BUDGET: int = 50_000


def _get_home() -> Path | None:
    """Resolve the user's home directory or return ``None`` if unresolvable.

    Indirected through this helper so tests can monkeypatch the home
    location without having to reach into ``pathlib.Path.home`` (which
    is a classmethod and affects every other test in the suite).

    ``Path.home()`` raises ``RuntimeError`` when the HOME / USERPROFILE
    env var is unset (rare; minimal CI containers). In that case fall
    back to "no home-relative skip applied", which is safer than crashing.
    """
    try:
        return Path.home().resolve()
    except (RuntimeError, OSError):
        return None


def _walk_recent_files(base: Path, cutoff: float, limit: int) -> list[tuple[float, Path]]:
    """Return [(mtime, path), ...] for files under ``base`` modified after ``cutoff``.

    Skips directories named in ``_SKIP_DIR_NAMES`` and any starting with '.'.
    Additionally skips ``_HOME_SKIP_RELATIVE`` entries (Library, AppData) but
    ONLY when the parent directory is the user's home — preserves projects
    that happen to be named ``Library`` somewhere deeper in the tree.

    Returns at most ``limit * 2`` entries (caller sorts + truncates to ``limit``).
    Halts early at ``_WALK_FILE_BUDGET`` files inspected.

    Permission errors during directory traversal are logged at DEBUG level
    rather than swallowed silently — this matters on Windows where locked
    subtrees (System Volume Information, etc.) would otherwise vanish from
    the walk without any signal that results may be incomplete.
    """
    def _onerror(exc: OSError) -> None:
        _log.debug("list_recent_files: skipped subtree due to %s", exc)

    home = _get_home()

    out: list[tuple[float, Path]] = []
    cap = max(limit * 2, limit + 10)
    inspected = 0
    for root, dirs, files in os.walk(base, onerror=_onerror):  # followlinks=False (default) — safe
        # Resolve once per iteration to compare against home accurately.
        try:
            root_resolved = Path(root).resolve()
        except OSError:
            root_resolved = Path(root)
        is_home = home is not None and root_resolved == home

        # Prune in-place so os.walk doesn't recurse into them
        dirs[:] = [
            d for d in dirs
            if d not in _SKIP_DIR_NAMES
            and not d.startswith(".")
            and not (is_home and d in _HOME_SKIP_RELATIVE)
        ]
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
    """Capture a screenshot, run vision-LLM analysis, return text + path.

    The tool captures the image, calls a vision model INTERNALLY with
    the user's question, and returns a JSON tool result containing the
    text analysis and the on-disk path. The agent that invoked the tool
    sees TEXT in conversation history — never the raw image — keeping
    per-turn token cost flat regardless of how many screenshots have
    been taken in the session.

    The path is surfaced so the agent can re-share the screenshot to
    the user via ``MEDIA:<path>`` in its next reply (see the outbound
    extractor at ``plugin_sdk/channel_contract.py:extract_media``), and
    so a follow-up ``VisionAnalyze(image_path=...)`` call can re-analyze
    the same image with a different prompt without re-capturing.
    """

    consent_tier: int = 1
    parallel_safe: bool = True
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="introspection.screenshot",
            tier_required=ConsentTier.IMPLICIT,
            human_description="Capture a screenshot and analyze it via a vision model.",
        ),
    )

    def __init__(
        self,
        *,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
        vision_api_key: str | None = None,
        vision_model: str | None = None,
    ) -> None:
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit
        # Vision-call config. Defaults to ANTHROPIC_API_KEY env at
        # request time + the same default model VisionAnalyze uses.
        # Constructor-injected values win for test fixturing.
        self._vision_api_key = vision_api_key
        self._vision_model = vision_model

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="screenshot",
            description=(
                "Capture a screenshot of the primary monitor and run vision "
                "analysis on it. The capture is saved to "
                "<profile_home>/tool_result_storage/screenshots/ (auto-deleted "
                "after 24h) and the path is returned alongside the text "
                "analysis. Use when the user asks 'what's on my screen?' or "
                "when you need to verify GUI state. Pass `prompt` to steer "
                "the analysis (default: a generic description). Pass `quadrant` "
                "(top-left/top-right/bottom-left/bottom-right) to capture just "
                "one corner — cheaper and less private. To share the screenshot "
                "with the user in your reply, include MEDIA:<screenshot_path> "
                "in your response. CAUTION: screenshots may contain sensitive "
                "on-screen data (passwords, private chats, financial info) AND "
                "are sent to the configured vision API; do not include in error "
                "messages, third-party calls, or persistent logs. For text "
                "content prefer extract_screen_text (OCR) — smaller and more "
                "privacy-aware. Cross-platform via mss (macOS, Linux, Windows). "
                "Linux requires an X or Wayland display server. Under F1 "
                "ConsentGate (IMPLICIT tier)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Question or instruction to steer the vision "
                            "analysis. Default: 'Describe what is visible on "
                            "the screen in detail.'"
                        ),
                    },
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
        # ── 1. Capture screenshot ────────────────────────────────────
        quadrant = call.arguments.get("quadrant")
        prompt = (
            call.arguments.get("prompt")
            or "Describe what is visible on the screen in detail."
        )
        try:
            with mss.mss() as sct:
                monitor = sct.monitors[1]  # primary monitor
                if quadrant:
                    monitor = _quadrant_bounds(monitor, quadrant)
                shot = sct.grab(monitor)
                png = mss.tools.to_png(shot.rgb, shot.size)
                shot_w, shot_h = shot.size
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({"success": False, "error": f"capture failed: {exc}"}),
                is_error=True,
            )

        # ── 2. Persist to disk so it can be re-shared / re-analyzed ──
        try:
            screenshots_dir = _resolve_screenshots_dir()
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            _prune_stale_screenshots(screenshots_dir)
            path = screenshots_dir / f"oc-screen-{uuid.uuid4().hex}.png"
            path.write_bytes(png)
        except OSError as exc:
            _log.warning("screenshot disk write failed: %s", exc)
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({
                    "success": False,
                    "error": f"failed to persist screenshot: {exc}",
                }),
                is_error=True,
            )

        # ── 3. Vision-model analysis ────────────────────────────────
        api_key = self._vision_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        size_kb = len(png) // 1024
        screenshot_path = str(path)

        if not api_key:
            # No vision API configured — return path only with a clear
            # note so the agent knows the analysis step was skipped. The
            # capture itself succeeded; the agent can still share the
            # file via MEDIA:<path>.
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({
                    "success": True,
                    "analysis": (
                        "(vision analysis skipped — ANTHROPIC_API_KEY not set; "
                        "screenshot was captured and saved)"
                    ),
                    "screenshot_path": screenshot_path,
                    "dimensions": [shot_w, shot_h],
                    "size_kb": size_kb,
                }),
            )

        # Lazy import to avoid pulling httpx into hot paths that don't
        # need vision. Module-level import would also create a circular
        # risk if vision_analyze ever needed to import from this module.
        from opencomputer.tools.vision_analyze import (  # noqa: PLC0415
            analyze_image_bytes,
        )

        image_b64 = base64.b64encode(png).decode("ascii")
        # PNG magic header is the only option mss produces; no need to sniff.
        result = await analyze_image_bytes(
            image_b64=image_b64,
            mime="image/png",
            prompt=prompt,
            api_key=api_key,
            model=self._vision_model or "claude-haiku-4-5",
        )

        if isinstance(result, tuple):
            # Vision call failed but capture succeeded. Graceful
            # degradation: surface both — the agent sees the error AND
            # can still share the screenshot via MEDIA:<path>.
            error_text, _is_err = result
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({
                    "success": False,
                    "error": error_text,
                    "screenshot_path": screenshot_path,
                    "note": (
                        "Screenshot was captured but vision analysis failed. "
                        "You can still share it via MEDIA:<screenshot_path>."
                    ),
                }),
            )

        # Happy path: vision call returned analysis text.
        return ToolResult(
            tool_call_id=call.id,
            content=json.dumps({
                "success": True,
                "analysis": result,
                "screenshot_path": screenshot_path,
                "dimensions": [shot_w, shot_h],
                "size_kb": size_kb,
            }),
        )


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
