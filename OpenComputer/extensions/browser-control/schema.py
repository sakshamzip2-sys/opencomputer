"""Pydantic models backing the single ``Browser`` discriminator tool.

Per BLUEPRINT §5 and the OpenClaw precedent we deliberately use a *flat*
schema (every field optional, the runtime validates per-action). OpenAI's
function-tool spec rejects nested ``anyOf`` discriminator unions, so a
discriminator tree is not viable across providers.

Two-level discriminator:

  - Outer ``action`` ∈ 16 values (the most common page-level + lifecycle
    operations).
  - Inner ``act.kind`` ∈ 11 values, only consulted when
    ``action == "act"``.

Both enums are ``str`` enums so OpenAI / Anthropic schema validators see
plain strings.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

try:
    from pydantic import BaseModel, ConfigDict, Field
except ImportError:  # pragma: no cover - browser extras gate this
    BaseModel = object  # type: ignore[assignment, misc]
    ConfigDict = dict  # type: ignore[assignment, misc]

    def Field(*_a: Any, **_kw: Any) -> Any:  # noqa: N802 — mirror pydantic's PascalCase
        return None


class BrowserAction(str, Enum):
    STATUS = "status"
    START = "start"
    STOP = "stop"
    PROFILES = "profiles"
    TABS = "tabs"
    OPEN = "open"
    FOCUS = "focus"
    CLOSE = "close"
    SNAPSHOT = "snapshot"
    SCREENSHOT = "screenshot"
    NAVIGATE = "navigate"
    CONSOLE = "console"
    PDF = "pdf"
    UPLOAD = "upload"
    DIALOG = "dialog"
    ACT = "act"


class BrowserActKind(str, Enum):
    CLICK = "click"
    TYPE = "type"
    PRESS = "press"
    HOVER = "hover"
    DRAG = "drag"
    SELECT = "select"
    FILL = "fill"
    RESIZE = "resize"
    WAIT = "wait"
    EVALUATE = "evaluate"
    CLOSE = "close"


# All known outer / inner enum string values, in declaration order. Used
# by the JSON Schema generator below so the LLM sees the exact set.
_ACTION_VALUES: tuple[str, ...] = tuple(a.value for a in BrowserAction)
_ACT_KIND_VALUES: tuple[str, ...] = tuple(k.value for k in BrowserActKind)


class ActRequest(BaseModel):
    """Body for ``action="act"``. Inner discriminator is ``kind``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    kind: BrowserActKind
    ref: str | None = None
    text: str | None = None
    key: str | None = None
    selector: str | None = None
    fields: list[dict[str, Any]] | None = None
    values: list[str] | None = None
    options: list[str] | None = None
    timeout_ms: int | None = Field(None, alias="timeoutMs")
    expression: str | None = None
    state: str | None = None
    target: str | None = None  # for drag / resize
    width: int | None = None
    height: int | None = None
    delta: int | None = None


Target = Literal["sandbox", "host", "node"]


class BrowserParams(BaseModel):
    """Flat input schema for the ``Browser`` tool — every field optional."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    action: BrowserAction
    target: Target | None = None
    profile: str | None = None
    target_id: str | None = Field(None, alias="targetId")
    target_url: str | None = Field(None, alias="targetUrl")
    url: str | None = None
    full_page: bool | None = Field(None, alias="fullPage")
    base_url: str | None = Field(None, alias="baseUrl")
    request: ActRequest | None = None
    # Convenience flat overrides for ``act`` so callers don't always need
    # ``request: {kind: ...}`` — the tool will compose ActRequest if
    # ``action="act"`` and ``request`` is omitted.
    kind: BrowserActKind | None = None
    ref: str | None = None
    text: str | None = None
    key: str | None = None
    selector: str | None = None
    fields: list[dict[str, Any]] | None = None
    values: list[str] | None = None
    options: list[str] | None = None
    expression: str | None = None
    state: str | None = None
    width: int | None = None
    height: int | None = None
    delta: int | None = None
    level: str | None = None
    clear: bool | None = None


def browser_params_json_schema() -> dict[str, Any]:
    """Build a hand-rolled OpenAI/Anthropic-friendly JSON schema.

    Pydantic's ``model_json_schema()`` produces a $defs-laden tree that
    some providers gag on; a flat hand-built schema is safer.
    """
    return {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTION_VALUES),
                "description": (
                    "Top-level operation. status/profiles read state. "
                    "start/stop manage the per-profile Chrome process. "
                    "tabs/open/focus/close manage tabs. snapshot returns the "
                    "ARIA-or-AI tree + refs for the current page. screenshot "
                    "returns a base64 PNG. navigate moves an existing tab to "
                    "a URL. console reads buffered console messages. pdf "
                    "saves the page to PDF. upload arms a file chooser. "
                    "dialog arms an alert/confirm/prompt response. act runs "
                    "an inner element-level operation (see request.kind)."
                ),
            },
            "target": {
                "type": "string",
                "enum": ["sandbox", "host", "node"],
                "description": (
                    "Where to run the browser. v0.1 only ships 'host'; the "
                    "schema accepts the other two so future versions don't "
                    "break callers."
                ),
            },
            "profile": {
                "type": "string",
                "description": (
                    "Browser profile name. Defaults to 'openclaw' (isolated, "
                    "agent-managed). Pass 'user' for the user's logged-in "
                    "Chrome (existing-session, host-only). Defer to the "
                    "operator on which to use."
                ),
            },
            "url": {
                "type": "string",
                "description": "Target URL for navigate / open.",
            },
            "targetId": {
                "type": "string",
                "description": (
                    "Chrome target id (returned by snapshot/open/tabs). "
                    "Pass the same id back to keep subsequent actions on "
                    "the same tab."
                ),
            },
            "targetUrl": {
                "type": "string",
                "description": "URL filter for tab lookup.",
            },
            "fullPage": {"type": "boolean"},
            "baseUrl": {
                "type": "string",
                "description": (
                    "Override the control service base URL. Leave empty to "
                    "use the in-process dispatcher (default)."
                ),
            },
            "request": {
                "type": "object",
                "additionalProperties": True,
                "description": (
                    "Inner request body when action='act'. Must include "
                    "'kind' (one of: " + ", ".join(_ACT_KIND_VALUES) + "). "
                    "Other fields depend on kind: ref/text/key/selector/"
                    "fields/values/options/timeoutMs/expression/state/...."
                ),
                "properties": {
                    "kind": {"type": "string", "enum": list(_ACT_KIND_VALUES)},
                    "ref": {"type": "string"},
                    "text": {"type": "string"},
                    "key": {"type": "string"},
                    "selector": {"type": "string"},
                    "fields": {"type": "array", "items": {"type": "object"}},
                    "values": {"type": "array", "items": {"type": "string"}},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "timeoutMs": {"type": "integer"},
                    "expression": {"type": "string"},
                    "state": {"type": "string"},
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "delta": {"type": "integer"},
                },
            },
            # Flat overrides — convenience for callers that don't want to
            # nest ``request: {kind, ...}`` for simple act calls.
            "kind": {"type": "string", "enum": list(_ACT_KIND_VALUES)},
            "ref": {"type": "string"},
            "text": {"type": "string"},
            "key": {"type": "string"},
            "selector": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "object"}},
            "values": {"type": "array", "items": {"type": "string"}},
            "options": {"type": "array", "items": {"type": "string"}},
            "expression": {"type": "string"},
            "state": {"type": "string"},
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "delta": {"type": "integer"},
            "level": {"type": "string"},
            "clear": {"type": "boolean"},
        },
        "required": ["action"],
    }


__all__ = [
    "ActRequest",
    "BrowserAction",
    "BrowserActKind",
    "BrowserParams",
    "browser_params_json_schema",
]
