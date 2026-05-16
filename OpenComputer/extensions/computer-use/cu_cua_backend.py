"""Cua-driver backend (macOS only).

Ported from hermes-agent ``tools/computer_use/cua_backend.py``. Speaks MCP
over stdio to ``cua-driver``. The Python ``mcp`` SDK is async, so we run a
dedicated asyncio event loop on a background thread and marshal sync calls
through it.

Install: ``/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"``

After install, ``cua-driver`` is on $PATH and supports ``cua-driver mcp``
(stdio transport) which is what we invoke.

The private SkyLight SPIs cua-driver uses (SLEventPostToPid, SLPSPostEvent-
RecordTo, _AXObserverAddNotificationAndCheckRemote) are not Apple-public and
can break on OS updates. Pin the installed version via
``OPENCOMPUTER_CUA_DRIVER_VERSION`` for reproducibility across an OS bump.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import threading
from concurrent.futures import Future
from typing import Any

from cu_backend import (  # type: ignore[import-not-found]
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)
from cu_installer import find_cua_driver  # type: ignore[import-not-found]

logger = logging.getLogger("opencomputer.computer_use.cua_backend")


# ---------------------------------------------------------------------------
# Version pinning
# ---------------------------------------------------------------------------

# Informational only — the backend speaks the cua-driver 0.1.9 MCP tool
# surface. ``find_cua_driver()`` does not enforce the version; this constant
# documents the surface the call sites below were reconciled against and is
# surfaced by ``oc doctor`` / the installer for reproducibility across an OS
# bump. The ``OPENCOMPUTER_CUA_DRIVER_VERSION`` env var overrides it.
PINNED_CUA_DRIVER_VERSION = os.environ.get("OPENCOMPUTER_CUA_DRIVER_VERSION", "0.1.9")

# The binary is resolved via ``find_cua_driver()`` (cu_installer.py), which
# honors the ``OPENCOMPUTER_CUA_DRIVER_CMD`` override and falls back to
# the upstream installer's well-known locations when not on ``$PATH``.
_CUA_DRIVER_ARGS = ["mcp"]  # stdio MCP transport

# Regex to parse element lines from a cua-driver 0.1.9 ``get_window_state``
# ``tree_markdown`` rendering. The real 0.1.9 format, confirmed against the
# installed binary, is one indented line per node, e.g.:
#
#   - AXApplication "Chrome"
#     - [0] AXWindow "Title…" actions=[AXRaise]
#       - [3] AXButton (Back) actions=[AXShowMenu]
#       - [8] AXTextField = "x.com/…" (Address and search bar) actions=[…]
#
# Only nodes the driver deemed *actionable* carry a ``[N]`` element_index
# token; container nodes have none and are skipped. A node's human label can
# arrive three ways — a quoted ``"title"``, a parenthesised ``(description)``,
# or an ``= "value"`` settable value — and any combination may co-occur.
_ELEMENT_LINE_RE = re.compile(
    r'^\s*-\s+\[(?P<index>\d+)\]\s+(?P<role>AX\w+)'
    r'(?:\s+=\s+"(?P<value>[^"]*)")?'
    r'(?:\s+"(?P<title>[^"]*)")?'
    r'(?:\s+\((?P<desc>[^)]*)\))?'
    r'(?:\s+actions=\[(?P<actions>[^\]]*)\])?',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_macos() -> bool:
    return sys.platform == "darwin"


def cua_driver_binary_available() -> bool:
    """True if the ``cua-driver`` binary can be resolved.

    Resolution (via ``find_cua_driver``) honors the
    ``OPENCOMPUTER_CUA_DRIVER_CMD`` override and the upstream installer's
    well-known locations — so this stays true even when ``~/.local/bin`` is
    not yet on ``$PATH`` in the current process.
    """
    return find_cua_driver() is not None


def cua_driver_install_hint() -> str:
    return (
        "cua-driver is not installed. Install with one of:\n"
        "  oc computer-use install\n"
        "Or run the upstream installer directly:\n"
        '  /bin/bash -c "$(curl -fsSL '
        'https://raw.githubusercontent.com/trycua/cua/main/libs/cua-driver/scripts/install.sh)"\n'
        "Or run `oc doctor --fix` and accept the cua-driver repair."
    )


def _coerce_window_record(w: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise one cua-driver 0.1.9 ``list_windows`` JSON record.

    Returns ``None`` for records missing the required addressing fields.
    """
    try:
        pid = int(w["pid"])
        window_id = int(w["window_id"])
    except (KeyError, TypeError, ValueError):
        return None
    bounds = w.get("bounds") or {}
    return {
        "app_name": str(w.get("app_name", "") or ""),
        "pid": pid,
        "window_id": window_id,
        "title": str(w.get("title", "") or ""),
        "off_screen": not bool(w.get("is_on_screen", True)),
        "z_index": int(w.get("z_index", 0) or 0),
        "layer": int(w.get("layer", 0) or 0),
        "bounds": {
            "x": int(bounds.get("x", 0) or 0),
            "y": int(bounds.get("y", 0) or 0),
            "width": int(bounds.get("width", 0) or 0),
            "height": int(bounds.get("height", 0) or 0),
        },
    }


def _parse_windows(out: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse window records from a cua-driver 0.1.9 ``list_windows`` result.

    0.1.9 returns JSON — either as MCP ``structuredContent`` or as a JSON
    text block (which ``_extract_tool_result`` already json-decodes into
    ``data``). The top-level shape is
    ``{"windows": [...], "current_space_id": <id|null>}``.
    """
    raw: list[Any] = []
    sc = out.get("structuredContent") or {}
    if isinstance(sc, dict) and isinstance(sc.get("windows"), list):
        raw = sc["windows"]
    elif isinstance(out.get("data"), dict) and isinstance(out["data"].get("windows"), list):
        raw = out["data"]["windows"]
    elif isinstance(out.get("data"), list):
        raw = out["data"]
    windows: list[dict[str, Any]] = []
    for w in raw:
        if isinstance(w, dict):
            rec = _coerce_window_record(w)
            if rec is not None:
                windows.append(rec)
    return windows


def _parse_elements_from_tree(markdown: str) -> list[UIElement]:
    """Parse the actionable ``UIElement`` list from a 0.1.9 ``tree_markdown``.

    cua-driver 0.1.9 tags actionable nodes ``- [N] AXRole …``. The AX tree
    carries no per-element pixel bounds in the Markdown rendering, so
    ``bounds`` stays ``(0, 0, 0, 0)`` — element-indexed actions address by
    ``element_index``, never by bounds, so this is correct, not lossy.
    """
    elements: list[UIElement] = []
    for m in _ELEMENT_LINE_RE.finditer(markdown):
        # Prefer the most descriptive label available.
        label = m.group("title") or m.group("desc") or m.group("value") or ""
        attrs: dict[str, Any] = {}
        if m.group("value") is not None:
            attrs["value"] = m.group("value")
        if m.group("actions"):
            attrs["actions"] = [
                a.strip() for a in m.group("actions").split(",") if a.strip()
            ]
        elements.append(UIElement(
            index=int(m.group("index")),
            role=m.group("role"),
            label=label,
            bounds=(0, 0, 0, 0),
            attributes=attrs,
        ))
    return elements


def _parse_key_combo(keys: str) -> tuple[str | None, list[str]]:
    """Parse a key string like 'cmd+s' into (key, modifiers).

    Returns (key, modifiers) where key is the non-modifier key and modifiers
    is a list of modifier names (cmd, shift, option, ctrl).
    """
    MODIFIER_NAMES = {"cmd", "command", "shift", "option", "alt", "ctrl", "control", "fn"}
    KEY_ALIASES = {"command": "cmd", "alt": "option", "control": "ctrl"}

    # The schema documents '+' as the combo separator ('cmd+s', 'ctrl+alt+t').
    # Split on '+' ONLY — never on '-' — so a literal '-' / '+' key survives:
    # 'cmd+-' (zoom out) parses as key='-' instead of a silently-dropped key.
    # A trailing empty segment after a '+' means the non-modifier key WAS '+'.
    segments = keys.split("+")
    parts: list[str] = []
    for i, seg in enumerate(segments):
        s = seg.strip().lower()
        if s:
            parts.append(s)
        elif 0 < i == len(segments) - 1:
            parts.append("+")
    modifiers = []
    key = None
    for part in parts:
        normalized = KEY_ALIASES.get(part, part)
        if normalized in MODIFIER_NAMES:
            modifiers.append(normalized)
        else:
            key = part  # last non-modifier wins
    return key, modifiers


# ---------------------------------------------------------------------------
# Asyncio bridge — one long-lived loop on a background thread
# ---------------------------------------------------------------------------

class _AsyncBridge:
    """Runs one asyncio loop on a daemon thread; marshals coroutines from the caller."""

    def __init__(self) -> None:
        self._loop = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()

        import asyncio

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            try:
                self._loop.run_forever()
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_run, daemon=True, name="cua-driver-loop")
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise RuntimeError("cua-driver asyncio bridge failed to start")

    def run(self, coro, timeout: float | None = 30.0) -> Any:
        import asyncio

        if not self._loop or not self._thread or not self._thread.is_alive():
            raise RuntimeError("cua-driver bridge not started")
        fut: Future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._loop = None


# ---------------------------------------------------------------------------
# MCP session (lazy, shared across tool calls)
# ---------------------------------------------------------------------------

class _CuaDriverSession:
    """Holds the mcp ClientSession. Spawned lazily; re-entered on drop."""

    def __init__(self, bridge: _AsyncBridge) -> None:
        self._bridge = bridge
        self._session = None
        self._exit_stack = None
        self._lock = threading.Lock()
        self._started = False

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("cua-driver session not started")

    async def _aenter(self) -> None:
        from contextlib import AsyncExitStack

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        binary = find_cua_driver()
        if binary is None:
            raise RuntimeError(cua_driver_install_hint())

        params = StdioServerParameters(
            command=binary,
            args=_CUA_DRIVER_ARGS,
            env={**os.environ},
        )
        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._exit_stack = stack
        self._session = session

    async def _aexit(self) -> None:
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception as e:
                logger.warning("cua-driver shutdown error: %s", e)
        self._exit_stack = None
        self._session = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._bridge.start()
            self._bridge.run(self._aenter(), timeout=15.0)
            self._started = True

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            try:
                self._bridge.run(self._aexit(), timeout=5.0)
            finally:
                self._started = False

    async def _call_tool_async(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self._session.call_tool(name, args)
        return _extract_tool_result(result)

    def call_tool(self, name: str, args: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        self._require_started()
        return self._bridge.run(self._call_tool_async(name, args), timeout=timeout)


def _extract_tool_result(mcp_result: Any) -> dict[str, Any]:
    """Convert an mcp CallToolResult into a plain dict.

    cua-driver returns a mix of text parts, image parts, and structuredContent.
    We flatten into:
      {
        "data": <text or parsed json>,
        "images": [b64, ...],
        "structuredContent": <dict|None>,
        "isError": bool,
      }
    """
    data: Any = None
    images: list[str] = []
    is_error = bool(getattr(mcp_result, "isError", False))
    structured: dict | None = getattr(mcp_result, "structuredContent", None) or None
    text_chunks: list[str] = []
    for part in getattr(mcp_result, "content", []) or []:
        ptype = getattr(part, "type", None)
        if ptype == "text":
            text_chunks.append(getattr(part, "text", "") or "")
        elif ptype == "image":
            b64 = getattr(part, "data", None)
            if b64:
                images.append(b64)
    if text_chunks:
        joined = "\n".join(t for t in text_chunks if t)
        try:
            data = json.loads(joined) if joined.strip().startswith(("{", "[")) else joined
        except json.JSONDecodeError:
            data = joined
    return {"data": data, "images": images, "structuredContent": structured, "isError": is_error}


def _error_message(out: dict[str, Any]) -> str:
    """Best-effort human-readable message from an ``isError`` tool result.

    cua-driver delivers error detail as the text content block (so it lands
    in ``data``) — either a plain string or a ``{"message": ...}`` JSON dict.
    """
    data = out.get("data")
    if isinstance(data, dict):
        msg = data.get("message") or data.get("error")
        if msg:
            return str(msg)
        return ""
    if isinstance(data, str):
        return data.strip()
    return ""


# ---------------------------------------------------------------------------
# The backend itself
# ---------------------------------------------------------------------------

class CuaDriverBackend(ComputerUseBackend):
    """Default computer-use backend. macOS-only via cua-driver MCP."""

    def __init__(self) -> None:
        self._bridge = _AsyncBridge()
        self._session = _CuaDriverSession(self._bridge)
        # Sticky context — updated by capture(), used by action tools.
        self._active_pid: int | None = None
        self._active_window_id: int | None = None

    # ── Lifecycle ──────────────────────────────────────────────────
    def start(self) -> None:
        self._session.start()

    def stop(self) -> None:
        try:
            self._session.stop()
        finally:
            self._bridge.stop()

    def is_available(self) -> bool:
        if not _is_macos():
            return False
        return cua_driver_binary_available()

    # ── Capture ────────────────────────────────────────────────────
    def _select_windows(self, app: str | None) -> list[dict[str, Any]]:
        """Resolve on-screen, layer-0 windows, frontmost-first, optional app filter."""
        lw_out = self._session.call_tool("list_windows", {"on_screen_only": True})
        windows = _parse_windows(lw_out)
        # Layer-0 only — menubar strips / dock shields are layer != 0 noise.
        windows = [w for w in windows if w["layer"] == 0]
        # Highest z_index = closest to front on the current Space (0.1.9 spec).
        windows.sort(key=lambda w: w["z_index"], reverse=True)
        if app:
            app_lower = app.lower()
            filtered = [w for w in windows if app_lower in w["app_name"].lower()]
            if filtered:
                windows = filtered
        return windows

    def capture(self, mode: str = "som", app: str | None = None) -> CaptureResult:
        """Capture the frontmost on-screen window (optionally filtered by app name).

        Maps ``capture(mode, app)`` → cua-driver 0.1.9 ``list_windows`` +
        ``get_window_state`` (som/ax) or ``screenshot`` (vision). Window
        geometry is derived from the ``list_windows`` ``bounds`` (logical
        screen points) and refined from ``get_window_state``'s
        ``screenshot_original_width/height`` when available — the
        screenshot-pixel space that ``click(x, y)`` / ``drag`` address.
        """
        windows = self._select_windows(app)
        if not windows:
            return CaptureResult(
                mode=mode, width=0, height=0, png_b64=None, elements=[],
                app="", window_title="", png_bytes_len=0,
                error=(f"no on-screen window found for app {app!r}"
                       if app else "no on-screen windows found"),
            )

        target = next((w for w in windows if not w["off_screen"]), windows[0])
        self._active_pid = target["pid"]
        self._active_window_id = target["window_id"]
        app_name = target["app_name"]
        # Baseline geometry: list_windows bounds in logical screen points.
        width = target["bounds"]["width"]
        height = target["bounds"]["height"]
        window_title = target["title"]

        png_b64: str | None = None
        elements: list[UIElement] = []
        error = ""

        # ``_select_windows`` falls back to ALL on-screen windows when an
        # ``app`` filter matches nothing (resilient targeting). For an
        # explicit ``capture(app=...)`` that fallback is misleading — the
        # caller asked for app X and would otherwise get app Y with no
        # signal. Surface the miss so the agent can correct the name.
        # ``list_windows`` records only carry ``app_name`` (no bundle_id),
        # so a bundle-ID form ("com.apple.Safari") is matched leniently
        # against the trailing identifier segment.
        if app:
            app_l = app.lower()
            name_l = app_name.lower()
            tail = app_l.rsplit(".", 1)[-1]
            if app_l not in name_l and name_l not in app_l and tail not in name_l:
                error = (f"no on-screen window matched app {app!r}; captured "
                         f"frontmost window ({app_name!r}) instead")

        if mode == "vision":
            sc_out = self._session.call_tool(
                "screenshot",
                {"window_id": self._active_window_id, "format": "jpeg", "quality": 85},
            )
            if sc_out["images"]:
                png_b64 = sc_out["images"][0]
            elif sc_out.get("isError"):
                # The documented macOS 26.4.x SCK -3801 regression lands
                # here — surface it instead of returning png_b64=None silently.
                error = (_error_message(sc_out)
                         or "screenshot capture failed (try capture mode 'ax')")
        else:
            gws_out = self._session.call_tool(
                "get_window_state",
                {"pid": self._active_pid, "window_id": self._active_window_id},
            )
            if gws_out.get("isError"):
                # window_id not on the current Space, pid mismatch, AX
                # walk refused — surface it rather than reporting "0
                # interactable elements" and misleading the agent.
                error = (_error_message(gws_out)
                         or "get_window_state failed for the target window")
            # 0.1.9 ships the structured payload (tree_markdown +
            # screenshot_* dims) as MCP ``structuredContent``; the text
            # block is a human summary with the tree embedded. Prefer the
            # structured dict, fall back to a json-decoded ``data`` dict,
            # then to the raw text block.
            data = gws_out["data"]
            sc = gws_out.get("structuredContent")
            payload: dict[str, Any] = {}
            if isinstance(sc, dict) and sc.get("tree_markdown") is not None:
                payload = sc
            elif isinstance(data, dict):
                payload = data

            tree = ""
            if payload:
                tree = str(payload.get("tree_markdown", "") or "")
                # The screenshot-pixel space click(x,y)/drag address: the
                # ORIGINAL (pre-resize) window pixels. Fall back to the
                # resized screenshot dims, then to list_windows bounds.
                ow = payload.get("screenshot_original_width")
                oh = payload.get("screenshot_original_height")
                if isinstance(ow, int) and ow > 0:
                    width = ow
                if isinstance(oh, int) and oh > 0:
                    height = oh
                if width == 0 and isinstance(payload.get("screenshot_width"), int):
                    width = payload["screenshot_width"]
                if height == 0 and isinstance(payload.get("screenshot_height"), int):
                    height = payload["screenshot_height"]
            elif isinstance(data, str):
                # Degraded transport — the tree is embedded in the text block.
                tree = data
            elements = _parse_elements_from_tree(tree)
            # The window line carries the AXWindow title; prefer it when
            # list_windows reported an empty title (chromeless surfaces).
            if not window_title:
                wt = re.search(r'\[\d+\]\s+AXWindow\s+"([^"]+)"', tree)
                if wt:
                    window_title = wt.group(1)
            if gws_out["images"]:
                png_b64 = gws_out["images"][0]

        png_bytes_len = 0
        if png_b64:
            try:
                png_bytes_len = len(base64.b64decode(png_b64, validate=False))
            except Exception:
                png_bytes_len = len(png_b64) * 3 // 4

        return CaptureResult(
            mode=mode,
            width=width,
            height=height,
            png_b64=png_b64,
            elements=elements,
            app=app_name,
            window_title=window_title,
            png_bytes_len=png_bytes_len,
            error=error,
        )

    # ── Pointer ────────────────────────────────────────────────────
    def click(
        self,
        *,
        element: int | None = None,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: list[str] | None = None,
    ) -> ActionResult:
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="click",
                                message="No active window — call capture() first.")

        # cua-driver 0.1.9 has no middle-click primitive — the ``click`` tool's
        # pixel path takes no ``button`` and there is no ``middle_click`` tool.
        # Reject explicitly rather than silently degrading to a left-click,
        # which would be a surprising wrong action.
        if button == "middle":
            return ActionResult(
                ok=False, action="middle_click",
                message="cua-driver 0.1.9 has no middle-click primitive — "
                        "middle_click is unsupported by this backend.",
            )

        if button == "right":
            tool = "right_click"
        elif click_count == 2:
            tool = "double_click"
        else:
            tool = "click"

        args: dict[str, Any] = {"pid": pid}
        if element is not None:
            if self._active_window_id is None:
                return ActionResult(ok=False, action=tool,
                                    message="No active window_id for element_index click.")
            args["element_index"] = element
            args["window_id"] = self._active_window_id
            # ``modifier``/``count`` are pixel-path-only in 0.1.9 — the AX
            # action path ignores them, so they're omitted here.
        elif x is not None and y is not None:
            args["x"] = x
            args["y"] = y
            # Multi-click on the pixel path is routed to the dedicated
            # ``double_click`` tool above (it has no ``count`` arg); 0.1.9's
            # ``click.count`` is only reached for an explicit triple-click,
            # which the public schema does not expose — so it is not sent.
            if modifiers:
                args["modifier"] = list(modifiers)
        else:
            return ActionResult(ok=False, action=tool,
                                message="click requires element= or x/y.")

        return self._action(tool, args)

    def drag(
        self,
        *,
        from_element: int | None = None,
        to_element: int | None = None,
        from_xy: tuple[int, int] | None = None,
        to_xy: tuple[int, int] | None = None,
        button: str = "left",
        modifiers: list[str] | None = None,
    ) -> ActionResult:
        """Press-drag-release gesture via cua-driver 0.1.9's ``drag`` tool.

        0.1.9's ``drag`` is pixel-only — macOS AX has no semantic drag
        action — so both endpoints must be window-local screenshot pixels.
        Element-indexed drag is rejected cleanly: the AX tree carries no
        per-element bounds for this backend to derive an endpoint from.
        """
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="drag",
                                message="No active window — call capture() first.")
        if from_element is not None or to_element is not None:
            return ActionResult(
                ok=False, action="drag",
                message="cua-driver 0.1.9 drag is pixel-only — pass "
                        "from_coordinate / to_coordinate, not element indices.",
            )
        if from_xy is None or to_xy is None:
            return ActionResult(
                ok=False, action="drag",
                message="drag requires from_coordinate and to_coordinate "
                        "(window-local screenshot pixels).",
            )
        args: dict[str, Any] = {
            "pid": pid,
            "from_x": int(from_xy[0]),
            "from_y": int(from_xy[1]),
            "to_x": int(to_xy[0]),
            "to_y": int(to_xy[1]),
        }
        if button in {"left", "right", "middle"} and button != "left":
            args["button"] = button
        if self._active_window_id is not None:
            args["window_id"] = self._active_window_id
        if modifiers:
            args["modifier"] = list(modifiers)
        return self._action("drag", args)

    def scroll(
        self,
        *,
        direction: str,
        amount: int = 3,
        element: int | None = None,
        x: int | None = None,
        y: int | None = None,
        modifiers: list[str] | None = None,
    ) -> ActionResult:
        """Scroll via cua-driver 0.1.9's keystroke-synthesised ``scroll`` tool.

        0.1.9's ``scroll`` schema is ``{pid, direction, by?, amount?,
        element_index?, window_id?}`` — there is NO pixel (x/y) addressing
        mode. ``amount`` is the keystroke repeat count (clamped 1–50);
        ``by`` defaults to ``line``. The ``element_index`` path focuses the
        scrollable element first; ``x``/``y`` are ignored (no pixel mode).
        """
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="scroll",
                                message="No active window — call capture() first.")
        if direction not in {"up", "down", "left", "right"}:
            return ActionResult(ok=False, action="scroll",
                                message=f"bad scroll direction {direction!r}; "
                                        "use up|down|left|right.")
        args: dict[str, Any] = {
            "pid": pid,
            "direction": direction,
            "by": "line",
            "amount": max(1, min(50, int(amount))),
        }
        if element is not None and self._active_window_id is not None:
            args["element_index"] = element
            args["window_id"] = self._active_window_id
        return self._action("scroll", args)

    # ── Keyboard ───────────────────────────────────────────────────
    def type_text(self, text: str) -> ActionResult:
        """Insert text via cua-driver 0.1.9's ``type_text`` tool.

        0.1.9 exposes a single ``type_text`` tool — there is NO
        ``type_text_chars``. ``type_text`` already tries an AX bulk write
        first and falls back to character-by-character ``CGEvent``
        synthesis internally when the target rejects the AX write (WebKit /
        Chromium inputs), so it is universal across macOS apps in
        background mode — exactly what the old comment wanted.
        """
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="type_text",
                                message="No active window — call capture() first.")
        return self._action("type_text", {"pid": pid, "text": text})

    def key(self, keys: str) -> ActionResult:
        """Send a key / key-combo via cua-driver 0.1.9.

        Single keys (``return``, ``tab``, ``a``) route through
        ``press_key``; multi-key combos (``cmd+s``) MUST route through
        ``hotkey`` — ``press_key`` only accepts one key. For combos the
        ``window_id`` is passed so the driver's FocusWithoutRaise fires,
        which is required for NSMenu key equivalents (Cmd+S/N/W) to reach
        a backgrounded app.
        """
        pid = self._active_pid
        if pid is None:
            return ActionResult(ok=False, action="key",
                                message="No active window — call capture() first.")

        key_name, modifiers = _parse_key_combo(keys)
        if not key_name:
            return ActionResult(ok=False, action="key",
                                message=f"Could not parse key from '{keys}'.")

        if modifiers:
            args: dict[str, Any] = {"pid": pid, "keys": [*modifiers, key_name]}
            # FocusWithoutRaise — required for menu key equivalents to land.
            if self._active_window_id is not None:
                args["window_id"] = self._active_window_id
            return self._action("hotkey", args)
        return self._action("press_key", {"pid": pid, "key": key_name})

    # ── Value setter ────────────────────────────────────────────────
    def set_value(self, value: str, element: int | None = None) -> ActionResult:
        """Set a value on an element. Handles AXPopUpButton selects natively."""
        pid = self._active_pid
        window_id = self._active_window_id
        if pid is None or window_id is None:
            return ActionResult(ok=False, action="set_value",
                                message="No active window — call capture() first.")
        if element is None:
            return ActionResult(ok=False, action="set_value",
                                message="set_value requires element= (element index).")
        args: dict[str, Any] = {
            "pid": pid,
            "window_id": window_id,
            "element_index": element,
            "value": value,
        }
        return self._action("set_value", args)

    # ── Introspection ──────────────────────────────────────────────
    def list_apps(self) -> list[dict[str, Any]]:
        """Return the running/installed app list from cua-driver 0.1.9.

        0.1.9's ``list_apps`` ships the structured app array as MCP
        ``structuredContent`` AND a human-readable text summary as the text
        block. Prefer ``structuredContent`` (and a json-decoded ``data``
        dict) — it carries ``bundle_id``/``running``/``active`` per record.
        The text path is a last-resort fallback for a degraded transport;
        each line is ``- Name (pid N) [bundle.id]``.
        """
        out = self._session.call_tool("list_apps", {})
        sc = out.get("structuredContent") or {}
        if isinstance(sc, dict) and isinstance(sc.get("apps"), list):
            return sc["apps"]
        data = out["data"]
        if isinstance(data, dict) and isinstance(data.get("apps"), list):
            return data["apps"]
        if isinstance(data, list):
            return data
        if isinstance(data, str):
            apps: list[dict[str, Any]] = []
            for line in data.splitlines():
                m = re.match(
                    r'^-\s+(?P<name>.+?)\s+\(pid\s+(?P<pid>-?\d+)\)'
                    r'(?:\s+\[(?P<bundle>[^\]]+)\])?\s*$',
                    line,
                )
                if m:
                    pid = int(m.group("pid"))
                    apps.append({
                        "name": m.group("name").strip(),
                        "pid": pid,
                        "bundle_id": m.group("bundle") or "",
                        "running": pid > 0,
                    })
            return apps
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        """Target an app for subsequent actions without stealing system focus.

        cua-driver 0.1.9 has NO ``focus_app`` tool — and deliberately so:
        every action tool is pid/window-addressed and "focus without raise"
        is a side-effect of passing ``window_id`` to ``hotkey`` / ``press_key``.
        So ``focus_app`` here is a pure window-selector: it enumerates
        on-screen windows via ``list_windows``, picks the best match for
        *app*, and stores its pid/window_id as the sticky target so the
        next click/type/key call addresses the right process — no focus
        steal, no window raise.

        ``raise_window=True`` is intentionally ignored: 0.1.9 exposes no
        raise primitive, and stealing the user's focus is exactly what this
        backend is designed to avoid. The returned message says so.
        """
        windows = self._select_windows(app)
        target = next((w for w in windows if not w["off_screen"]), None) \
            or (windows[0] if windows else None)
        if target:
            self._active_pid = target["pid"]
            self._active_window_id = target["window_id"]
            note = ""
            if raise_window:
                note = (" (raise_window ignored — cua-driver 0.1.9 has no "
                        "window-raise primitive; targeting without raising)")
            return ActionResult(
                ok=True, action="focus_app",
                message=f"Targeted {target['app_name']} (pid {self._active_pid}, "
                        f"window {self._active_window_id}) without raising window."
                        + note,
                meta={"pid": self._active_pid, "window_id": self._active_window_id,
                      "app_name": target["app_name"]},
            )
        return ActionResult(ok=False, action="focus_app",
                            message=f"No on-screen window found for app '{app}'.")

    # ── Internal ───────────────────────────────────────────────────
    def _action(self, name: str, args: dict[str, Any]) -> ActionResult:
        try:
            out = self._session.call_tool(name, args)
        except Exception as e:
            logger.exception("cua-driver %s call failed", name)
            return ActionResult(ok=False, action=name, message=f"cua-driver error: {e}")
        ok = not out["isError"]
        message = ""
        data = out["data"]
        if isinstance(data, dict):
            message = str(data.get("message", ""))
        elif isinstance(data, str):
            message = data
        return ActionResult(ok=ok, action=name, message=message,
                            meta=data if isinstance(data, dict) else {})


__all__ = [
    "CuaDriverBackend",
    "cua_driver_binary_available",
    "cua_driver_install_hint",
    "PINNED_CUA_DRIVER_VERSION",
]
