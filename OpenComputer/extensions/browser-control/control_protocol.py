"""Wire types for the browser-control extension protocol (Wave 6).

Mirrors the TypeScript ``protocol.ts`` shipped inside
``extensions/browser-control/extension/src/protocol.ts`` (which was
adapted from OpenCLI under Apache License 2.0).

Daemon ↔ extension communication:
  - Transport: WebSocket at ``ws://127.0.0.1:<control_port>/ext``
  - Format: JSON messages, one per WS frame
  - Correlation: each ``Command`` carries an ``id`` echoed in its
    ``Result``, so multiple in-flight commands demux cleanly
  - Health: ``GET /ping`` on the same port returns daemon metadata

The daemon side hosts the WS endpoint and dispatches commands. Adapter
calls / Browser tool actions translate into Commands; Results translate
back into Browser tool responses.

v0.6 ships 8 of 14 actions (the rest will land in v0.6.x as adapters
demand them):
  - ``exec``                  — run JS in the page (Runtime.evaluate)
  - ``navigate``              — Page.navigate to URL
  - ``tabs``                  — list/new/close/select tabs
  - ``cookies``               — chrome.cookies.getAll(domain)
  - ``screenshot``            — Page.captureScreenshot
  - ``network-capture-start`` — turn on Network.* event capture
  - ``network-capture-read``  — drain captured entries
  - ``cdp``                   — raw CDP method passthrough
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# 8 of OpenCLI's 14 actions ship in v0.6 MVP. The full type lists all
# 14 so the wire schema matches OpenCLI verbatim — daemon will just
# refuse the unimplemented ones with a clear error code.
Action = Literal[
    "exec",
    "navigate",
    "tabs",
    "cookies",
    "screenshot",
    "close-window",
    "sessions",
    "set-file-input",
    "insert-text",
    "bind",
    "network-capture-start",
    "network-capture-read",
    "cdp",
    "frames",
]

# Subset that v0.6 actually supports end-to-end. Daemon validates against
# this; extension doesn't care (TS code handles all 14).
SUPPORTED_ACTIONS_V0_6: frozenset[Action] = frozenset(
    {
        "exec",
        "navigate",
        "tabs",
        "cookies",
        "screenshot",
        "network-capture-start",
        "network-capture-read",
        "cdp",
    }
)

TabsOp = Literal["list", "new", "close", "select"]
ScreenshotFormat = Literal["png", "jpeg"]


@dataclass(slots=True)
class Command:
    """One request from daemon → extension. Mirrors ``protocol.ts``'s ``Command``.

    All fields except ``id`` and ``action`` are optional and only set
    when the action needs them.
    """

    id: str
    action: Action
    page: str | None = None
    code: str | None = None
    workspace: str | None = None
    url: str | None = None
    op: TabsOp | None = None
    index: int | None = None
    domain: str | None = None
    match_domain: str | None = None
    match_path_prefix: str | None = None
    format: ScreenshotFormat | None = None
    quality: int | None = None
    full_page: bool | None = None
    files: list[str] | None = None
    selector: str | None = None
    text: str | None = None
    pattern: str | None = None
    cdp_method: str | None = None
    cdp_params: dict[str, Any] | None = None
    window_focused: bool | None = None
    idle_timeout: int | None = None
    allow_bound_navigation: bool | None = None
    frame_index: int | None = None
    context_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Serialize to the WS JSON shape the extension expects.

        Field-name mapping: snake_case → camelCase to match the TS side.
        Empty/None fields are omitted to keep the wire clean.
        """
        out: dict[str, Any] = {"id": self.id, "action": self.action}
        # snake_case → camelCase mapping for the optional fields
        mapping = {
            "page": self.page,
            "code": self.code,
            "workspace": self.workspace,
            "url": self.url,
            "op": self.op,
            "index": self.index,
            "domain": self.domain,
            "matchDomain": self.match_domain,
            "matchPathPrefix": self.match_path_prefix,
            "format": self.format,
            "quality": self.quality,
            "fullPage": self.full_page,
            "files": self.files,
            "selector": self.selector,
            "text": self.text,
            "pattern": self.pattern,
            "cdpMethod": self.cdp_method,
            "cdpParams": self.cdp_params,
            "windowFocused": self.window_focused,
            "idleTimeout": self.idle_timeout,
            "allowBoundNavigation": self.allow_bound_navigation,
            "frameIndex": self.frame_index,
            "contextId": self.context_id,
        }
        for key, value in mapping.items():
            if value is not None:
                out[key] = value
        return out


@dataclass(slots=True)
class Result:
    """One response from extension → daemon. Mirrors ``protocol.ts``'s ``Result``."""

    id: str
    ok: bool
    data: Any = None
    error: str | None = None
    error_code: str | None = None
    error_hint: str | None = None
    page: str | None = None

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> Result:
        """Parse a Result from its JSON wire shape (camelCase → snake_case)."""
        return cls(
            id=str(raw["id"]),
            ok=bool(raw.get("ok", False)),
            data=raw.get("data"),
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
            error_code=raw.get("errorCode") if isinstance(raw.get("errorCode"), str) else None,
            error_hint=raw.get("errorHint") if isinstance(raw.get("errorHint"), str) else None,
            page=raw.get("page") if isinstance(raw.get("page"), str) else None,
        )


@dataclass(slots=True)
class HelloMessage:
    """Sent by extension → daemon on WebSocket connect. Confirms version + contextId.

    Wire shape: ``{"type": "hello", "contextId": "<id>", "version": "<x.y.z>", "compatRange": "^0.6.0"}``
    """

    context_id: str
    version: str
    compat_range: str

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> HelloMessage:
        return cls(
            context_id=str(raw.get("contextId") or ""),
            version=str(raw.get("version") or ""),
            compat_range=str(raw.get("compatRange") or ""),
        )


@dataclass(slots=True)
class LogMessage:
    """Console-log forwarded from extension → daemon (debugging aid).

    Wire shape: ``{"type": "log", "level": "info"|"warn"|"error", "msg": "...", "ts": <ms>}``
    """

    level: Literal["info", "warn", "error"]
    msg: str
    ts: int

    @classmethod
    def from_wire(cls, raw: dict[str, Any]) -> LogMessage:
        level = raw.get("level")
        if level not in ("info", "warn", "error"):
            level = "info"
        return cls(
            level=level,
            msg=str(raw.get("msg") or ""),
            ts=int(raw.get("ts") or 0),
        )


# Default daemon port — matches browser-control's existing daemon
# (see DEFAULT_BROWSER_CONTROL_PORT in profiles/config.py).
DEFAULT_CONTROL_DAEMON_PORT: int = 18792
DEFAULT_CONTROL_WS_PATH: str = "/ext"
DEFAULT_CONTROL_PING_PATH: str = "/ping"

# How long a single command may sit awaiting a response from the
# extension before the daemon raises a timeout. Matches OpenCLI's
# ``IDLE_TIMEOUT_DEFAULT = 30_000`` ms — same wire-protocol contract.
DEFAULT_COMMAND_TIMEOUT_S: float = 30.0


@dataclass(slots=True)
class ConnectedExtension:
    """Daemon-side view of one connected extension (one Chrome instance).

    Tracked per-WebSocket. ``context_id`` lets the daemon route commands
    to the right Chrome when multiple are connected (one per OpenComputer
    profile).
    """

    context_id: str
    extension_version: str
    compat_range: str
    pending: dict[str, Any] = field(default_factory=dict)
