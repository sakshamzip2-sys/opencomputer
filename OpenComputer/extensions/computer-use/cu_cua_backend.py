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

# cua-driver 0.1.9, launched without CuaDriver.app's TCC grants, auto-relaunches
# its own daemon (``open -n -g -a CuaDriver --args serve``) and proxies MCP
# requests through it. That daemon brings up a full-screen, untitled,
# layer-0, on-screen helper window — verified live: app_name "Cua Driver",
# bundle_id ``com.trycua.driver``, ``{x:0, y:0, w:full, h:full}``, the HIGHEST
# z_index of any on-screen window. ``_is_system_chrome_strip`` does NOT catch
# it (full-height, not a thin strip), so the frontmost-first selector would
# pick the driver's OWN window and capture()/click() would operate on the
# driver instead of the user's app. Exclude it by owning-app identity.
_CUA_DRIVER_OWN_APP_NAMES = frozenset({"cua driver", "cuadriver"})

# Regex to parse element lines from a cua-driver 0.1.9 ``get_window_state``
# ``tree_markdown`` rendering. The real 0.1.9 format, confirmed live against
# the installed binary, is one indented line per node, e.g.:
#
#   - AXApplication "TextEdit"
#     - [0] AXWindow "Untitled 4" id=_NS:34 actions=[AXRaise]
#       - [2] AXTextArea id=First Text View actions=[AXShowMenu]
#       - [19] AXPopUpButton = "Helvetica" (typeface) help="Choose the
#              typeface" id=_NS:87 actions=[AXShowMenu]
#
# Only nodes the driver deemed *actionable* carry a ``[N]`` element_index
# token; container nodes have none and are skipped. A node's human label can
# arrive several ways — a quoted ``"title"``, a parenthesised
# ``(description)``, an ``= "value"`` settable value, or a ``help="…"``
# tooltip — and any combination may co-occur.
#
# CRITICAL (audit loop 8, found live): real 0.1.9 lines interleave ``id=…``
# and ``help="…"`` tokens BETWEEN the label and ``actions=[…]``. The ``id=``
# value is unquoted and may itself contain spaces (``id=First Text View``).
# An earlier regex anchored ``actions=`` directly after the optional
# label groups, so for the ~35% of elements that carry an ``id=`` token the
# ``actions`` list was silently dropped. The fix: an optional ``help="…"``
# capture, then a lazy ``[^\n]*?`` gap that swallows ``id=…`` (and any
# future inter-token noise) before the ``actions=`` group. The gap is
# newline-bounded so it never crosses into the next element's line.
_ELEMENT_LINE_RE = re.compile(
    r'^\s*-\s+\[(?P<index>\d+)\]\s+(?P<role>AX\w+)'
    r'(?:\s+=\s+"(?P<value>[^"]*)")?'
    r'(?:\s+"(?P<title>[^"]*)")?'
    r'(?:\s+\((?P<desc>[^)]*)\))?'
    r'(?:\s+help="(?P<help>[^"]*)")?'
    r'(?:[^\n]*?\sactions=\[(?P<actions>[^\]]*)\])?',
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
        # Prefer the most descriptive label available. ``help`` is the
        # 0.1.9 tooltip text — a useful last-resort label for an element
        # with no title/desc/value (e.g. a bare AXButton).
        label = (m.group("title") or m.group("desc")
                 or m.group("value") or m.group("help") or "")
        attrs: dict[str, Any] = {}
        if m.group("value") is not None:
            attrs["value"] = m.group("value")
        if m.group("help"):
            attrs["help"] = m.group("help")
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

    def _recover(self) -> None:
        """Tear down a wedged session and start a fresh one.

        cua-driver 0.1.9's keyboard/scroll tools can crash the relay daemon
        (a SkyLight SPI defect). When that happens the daemon process dies
        and the ``cua-driver mcp`` relay this session is bound to keeps
        returning "daemon closed connection"/"daemon not reachable" for
        EVERY subsequent call — the session is permanently wedged. A fresh
        session spawns a new ``cua-driver mcp`` relay, which transparently
        relaunches the daemon (verified live against 0.1.9). So recovery is
        a full stop + start of the MCP session, not a per-call retry.

        Caller already holds ``_lock``. ``_aexit`` swallows its own errors;
        ``_aenter`` may still raise (binary gone, bridge dead) and that
        propagates — an unrecoverable session is a real failure.
        """
        try:
            self._bridge.run(self._aexit(), timeout=5.0)
        except Exception as e:
            logger.warning("cua-driver session teardown during recovery failed: %s", e)
        self._exit_stack = None
        self._session = None
        self._bridge.start()
        self._bridge.run(self._aenter(), timeout=15.0)
        self._started = True
        logger.warning("cua-driver session recovered after a dead-daemon error")

    async def _call_tool_async(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self._session.call_tool(name, args)
        return _extract_tool_result(result)

    def call_tool(self, name: str, args: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        """Invoke a cua-driver MCP tool, recovering once from a dead daemon.

        If the cua-driver daemon crashed mid-session (keyboard/scroll SPI
        defect, OS sleep, manual ``cua-driver stop``), the relay surfaces a
        transport error — never a tool ``isError`` result. ``_is_dead_daemon_error``
        recognises that class; on a match the session is recycled (which
        relaunches the daemon) and the call is retried EXACTLY once. A
        second failure, or any non-transport error, propagates so the
        caller (``_action`` / ``capture``) can surface it cleanly.
        """
        with self._lock:
            self._require_started()
            try:
                return self._bridge.run(self._call_tool_async(name, args), timeout=timeout)
            except Exception as e:
                if not _is_dead_daemon_error(e):
                    raise
                logger.warning(
                    "cua-driver %s hit a dead-daemon error (%s) — recycling session",
                    name, e,
                )
                self._recover()
            # Retry once on the fresh session — outside the except so a
            # failure here is a clean, first-class exception, not chained.
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


# Substrings cua-driver 0.1.9 puts in a transport-level error when its relay
# daemon has died — keyboard/scroll SPI crash, OS sleep, manual ``stop``.
# These are recoverable by recycling the MCP session (which relaunches the
# daemon); a normal tool failure never raises — it comes back as ``isError``.
_DEAD_DAEMON_MARKERS = (
    "daemon closed connection",
    "daemon not reachable",
    "daemon transport",
    "connection closed",
    "broken pipe",
    "closedresourceerror",      # anyio — stdio write stream closed
    "brokenresourceerror",      # anyio — stdio stream broke mid-write
    "endofstream",              # anyio — stdio read stream hit EOF
)

# Exception *type* names that mean the MCP stdio transport died — verified
# live: when the cua-driver relay daemon is killed mid-workflow, the mcp
# SDK's anyio memory stream raises ``ClosedResourceError`` /
# ``BrokenResourceError`` / ``EndOfStream``. CRITICAL (audit loop 9, found
# live): these anyio errors stringify to "" — a message-substring match
# alone (``_DEAD_DAEMON_MARKERS`` against ``str(exc)``) NEVER fires for
# them, so the session-recovery path was silently skipped and the session
# wedged permanently after a daemon crash. Match the exception's class
# name (and its causes') so a daemon death is always recognised.
_DEAD_DAEMON_EXC_NAMES = frozenset({
    "closedresourceerror",
    "brokenresourceerror",
    "endofstream",
    "brokenpipeerror",
    "connectionreseterror",
})


class CuaDriverCallError(RuntimeError):
    """A cua-driver MCP call failed at the transport level (not a tool error).

    Raised by ``CuaDriverBackend._call`` when ``_session.call_tool`` throws
    even after a session-recovery retry. Read paths (``capture`` /
    ``list_apps`` / ``focus_app``) catch this and turn it into a clean
    error result instead of letting the raw ``McpError`` escape.
    """


def _is_dead_daemon_error(exc: BaseException) -> bool:
    """True if ``exc`` looks like a cua-driver relay-daemon death.

    Two discriminators, because daemon death surfaces two ways:

    * a generic ``McpError``/connection error whose *message* carries one
      of ``_DEAD_DAEMON_MARKERS`` (e.g. a press_key SPI crash);
    * an anyio stdio-stream error (``ClosedResourceError`` /
      ``BrokenResourceError`` / ``EndOfStream``) raised when the relay
      daemon process is killed — these stringify to "", so a message
      match never fires and the exception *type name* is the only signal.

    The exception's ``__cause__`` / ``__context__`` chain is walked: the
    mcp SDK wraps the underlying anyio error, so the transport failure can
    sit several links deep. A genuine tool error never reaches here —
    cua-driver returns those as an ``isError`` result, not an exception.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__.lower() in _DEAD_DAEMON_EXC_NAMES:
            return True
        text = str(cur).lower()
        if text and any(marker in text for marker in _DEAD_DAEMON_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


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
    @staticmethod
    def _is_system_chrome_strip(w: dict[str, Any]) -> bool:
        """True for the macOS menu bar / dock shield — NOT a real app window.

        Verified live against cua-driver 0.1.9: contrary to the upstream
        ``describe`` text (which claims menu-bar strips are ``layer != 0``
        and pre-filtered), the system menu bar surfaces as a *layer-0,
        on-screen* ``list_windows`` record — e.g. ``Code`` window 7891 with
        ``{x:0, y:-44, w:1920, h:44}``, empty title, and ``z_index`` HIGHER
        than the app's real window. Its ``get_window_state`` root is
        ``AXMenuBar``, not ``AXWindow``. Left in, it wins the frontmost-first
        sort and ``capture()`` returns the menu bar instead of the app.

        Discriminator (list_windows fields only — no extra round-trip): an
        untitled, top-anchored (``y <= 0``), thin (``height <= 50``) strip.
        Real app windows have a title, or sit below the menu bar, or are
        taller than a 44 px strip — none are dropped by this.
        """
        b = w.get("bounds") or {}
        try:
            y = int(b.get("y", 0) or 0)
            height = int(b.get("height", 0) or 0)
        except (TypeError, ValueError):
            return False
        return (not w.get("title")) and y <= 0 and 0 < height <= 50

    @staticmethod
    def _is_own_driver_window(w: dict[str, Any]) -> bool:
        """True for cua-driver's own relay-daemon window — never a target.

        The 0.1.9 daemon (``CuaDriver.app``) puts up a full-screen layer-0
        helper window with the highest z_index; left in, the frontmost-first
        sort picks it instead of the user's app. ``_is_system_chrome_strip``
        cannot catch it (it is full-height, not a thin strip), so it is
        filtered here by owning-app identity. See ``_CUA_DRIVER_OWN_APP_NAMES``.
        """
        name = str(w.get("app_name", "") or "").strip().lower()
        return name in _CUA_DRIVER_OWN_APP_NAMES

    def _select_windows(self, app: str | None) -> list[dict[str, Any]]:
        """Resolve on-screen, layer-0 windows, frontmost-first, optional app filter."""
        lw_out = self._call("list_windows", {"on_screen_only": True})
        windows = _parse_windows(lw_out)
        # Layer-0 only — most dock shields are layer != 0 noise. The macOS
        # menu bar, however, is reported as a layer-0 on-screen window by
        # 0.1.9 (see ``_is_system_chrome_strip``), so a second geometry
        # filter is required — the layer filter alone does NOT catch it.
        # cua-driver's own relay-daemon window is also layer-0/on-screen and
        # is excluded by owning-app identity (see ``_is_own_driver_window``).
        windows = [w for w in windows
                   if w["layer"] == 0
                   and not self._is_system_chrome_strip(w)
                   and not self._is_own_driver_window(w)]
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
        geometry starts from the ``list_windows`` ``bounds`` (logical
        screen points) but is OVERRIDDEN by ``get_window_state``'s
        ``screenshot_width``/``screenshot_height`` whenever present — those
        are the actual PNG-pixel dimensions, which is the space
        ``click(x, y)`` / ``drag`` address. On a Retina display the two
        differ by ``screenshot_scale_factor`` (logical points vs pixels),
        so reporting the screenshot dims is required for correct
        coordinate-space addressing.
        """
        # ``_select_windows`` calls ``list_windows`` over MCP. A dead-daemon
        # crash there (or any unrecoverable transport failure) must NOT
        # escape ``capture`` as a raw ``McpError`` — the contract is to
        # return a ``CaptureResult``. The session already self-recovers
        # once; an error that still escapes is genuinely unrecoverable, so
        # surface it cleanly with the same shape as a no-window result.
        try:
            windows = self._select_windows(app)
        except CuaDriverCallError as e:
            return CaptureResult(
                mode=mode, width=0, height=0, png_b64=None, elements=[],
                app="", window_title="", png_bytes_len=0,
                error=f"cua-driver capture failed: {e}",
            )
        if not windows:
            return CaptureResult(
                mode=mode, width=0, height=0, png_b64=None, elements=[],
                app="", window_title="", png_bytes_len=0,
                error=(f"no on-screen window found for app {app!r}"
                       if app else "no on-screen windows found"),
            )

        target = next((w for w in windows if not w["off_screen"]), windows[0])
        return self._capture_target(target, mode, app)

    def recapture_active(self, mode: str = "som") -> CaptureResult:
        """Re-capture the CURRENT sticky window (the one actions address).

        The tool layer's ``capture_after`` follow-up uses this so it
        verifies the EXACT window the just-run action touched — not
        whatever happens to be frontmost. A plain ``capture(mode='som')``
        re-runs frontmost-first window selection, which after a ``type``
        into a backgrounded app would silently verify the wrong window.

        ``get_window_state`` works on any window regardless of on-screen
        state, so this needs no ``list_windows`` round-trip; it builds a
        minimal ``target`` record from the sticky pid/window_id. Returns a
        clean error ``CaptureResult`` when no sticky window is set.
        """
        if self._active_pid is None or self._active_window_id is None:
            return CaptureResult(
                mode=mode, width=0, height=0, png_b64=None, elements=[],
                app="", window_title="", png_bytes_len=0,
                error="no active window — capture() first",
            )
        target = {
            "pid": self._active_pid,
            "window_id": self._active_window_id,
            "app_name": "",
            "title": "",
            "bounds": {"x": 0, "y": 0, "width": 0, "height": 0},
        }
        return self._capture_target(target, mode, app=None)

    def _capture_target(self, target: dict[str, Any], mode: str,
                        app: str | None) -> CaptureResult:
        """Run the get_window_state / screenshot capture for one resolved window."""
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
            try:
                sc_out = self._call(
                    "screenshot",
                    {"window_id": self._active_window_id,
                     "format": "jpeg", "quality": 85},
                )
            except CuaDriverCallError as e:
                return CaptureResult(
                    mode=mode, width=width, height=height, png_b64=None,
                    elements=[], app=app_name, window_title=window_title,
                    png_bytes_len=0, error=f"cua-driver screenshot failed: {e}",
                )
            if sc_out["images"]:
                png_b64 = sc_out["images"][0]
            elif sc_out.get("isError"):
                # The documented macOS 26.4.x SCK -3801 regression lands
                # here — surface it instead of returning png_b64=None silently.
                error = (_error_message(sc_out)
                         or "screenshot capture failed (try capture mode 'ax')")
        else:
            try:
                gws_out = self._call(
                    "get_window_state",
                    {"pid": self._active_pid, "window_id": self._active_window_id},
                )
            except CuaDriverCallError as e:
                return CaptureResult(
                    mode=mode, width=width, height=height, png_b64=None,
                    elements=[], app=app_name, window_title=window_title,
                    png_bytes_len=0,
                    error=f"cua-driver get_window_state failed: {e}",
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
                # click(x,y)/drag address the screenshot-pixel space of the
                # image get_window_state returns. Verified live against
                # cua-driver 0.1.9: the structuredContent carries BOTH
                # ``screenshot_width``/``screenshot_height`` AND
                # ``screenshot_original_width``/``screenshot_original_height``.
                # The delivered image's actual pixel dimensions equal
                # ``screenshot_width``/``screenshot_height`` (the downscaled
                # form, capped by ``max_image_dimension``); the ``_original_*``
                # pair is the pre-downscale window size and is NOT the
                # coordinate space click(x,y) uses. So this reads
                # ``screenshot_width``/``screenshot_height`` and deliberately
                # ignores ``screenshot_original_*``. Prefer them over the
                # list_windows ``bounds``, which are logical screen points
                # and diverge from screenshot pixels by
                # ``screenshot_scale_factor`` on a Retina display. The
                # bounds remain the fallback for the degraded transport
                # where structuredContent is absent (``ax`` capture_mode
                # also omits ``screenshot_*`` — bounds are the only signal).
                sw = payload.get("screenshot_width")
                sh = payload.get("screenshot_height")
                if isinstance(sw, int) and sw > 0:
                    width = sw
                if isinstance(sh, int) and sh > 0:
                    height = sh
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
            # ``som`` is the only ``get_window_state`` mode that promises a
            # screenshot. But 0.1.9's ``get_window_state`` takes NO per-call
            # mode arg — its response shape is dictated by the PERSISTENT
            # ``capture_mode`` config. If that config was left at ``ax`` by an
            # earlier ``set_config`` (the daemon persists it across restarts
            # and shares it with every client), a ``capture(mode='som')`` call
            # comes back with the AX tree but NO ``screenshot_*``/image — the
            # caller asked for a screenshot and silently got none. Surface the
            # miss with an actionable hint rather than returning a clean-looking
            # screenshot-less ``som`` result. ``ax`` mode never expects an
            # image, so it is exempt. (A successful screenshot-less ``som`` from
            # an SCK failure is already covered: 0.1.9 still ships the tree and
            # the summary line carries its own hint.)
            if mode == "som" and png_b64 is None and not error:
                error = (
                    "capture mode 'som' returned no screenshot — the "
                    "cua-driver daemon's persistent capture_mode is not 'som' "
                    "(run `cua-driver config set capture_mode som`), or the "
                    "macOS ScreenCaptureKit grant is missing. Element-indexed "
                    "actions still work; for pixels retry after fixing the "
                    "config or use mode='vision'."
                )

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
        # A dead-daemon crash must not escape as a raw ``McpError`` — the
        # contract is to return a list. The session self-recovers once;
        # an unrecoverable failure degrades to an empty list (the agent
        # then sees "no apps" and can retry, rather than an uncaught crash).
        try:
            out = self._call("list_apps", {})
        except CuaDriverCallError as e:
            logger.warning("cua-driver list_apps unrecoverable: %s", e)
            return []
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
        # A dead-daemon crash in ``list_windows`` must surface as a clean
        # failed ActionResult, not a raw ``McpError`` escaping the method.
        try:
            windows = self._select_windows(app)
        except CuaDriverCallError as e:
            return ActionResult(ok=False, action="focus_app",
                                message=f"cua-driver focus_app failed: {e}")
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
    def _call(self, name: str, args: dict[str, Any],
              timeout: float = 30.0) -> dict[str, Any]:
        """Invoke a cua-driver tool, raising ``CuaDriverCallError`` on transport failure.

        ``_session.call_tool`` already recovers ONCE from a dead daemon
        (see ``_CuaDriverSession.call_tool``). Anything that still escapes
        — an unrecoverable session, a second crash, a timeout — is wrapped
        in a typed ``CuaDriverCallError`` so the read paths (``capture`` /
        ``list_apps`` / ``focus_app`` / ``_select_windows``) can surface it
        as a clean error result rather than letting a raw ``McpError``
        propagate out of a method whose contract is to return a result.
        """
        try:
            return self._session.call_tool(name, args, timeout=timeout)
        except CuaDriverCallError:
            raise
        except Exception as e:
            # A bare ``McpError`` / closed-pipe error can stringify to "" —
            # ``repr`` is the fallback so the wrapped message always names
            # the failure, never an empty "cua-driver X failed: ".
            detail = str(e) or repr(e)
            logger.warning("cua-driver %s call failed: %s", name, detail)
            raise CuaDriverCallError(f"cua-driver {name} failed: {detail}") from e

    def _action(self, name: str, args: dict[str, Any]) -> ActionResult:
        try:
            out = self._call(name, args)
        except Exception as e:
            logger.exception("cua-driver %s call failed", name)
            return ActionResult(ok=False, action=name,
                                message=f"cua-driver error: {str(e) or repr(e)}")
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
    "CuaDriverCallError",
    "cua_driver_binary_available",
    "cua_driver_install_hint",
    "PINNED_CUA_DRIVER_VERSION",
]
