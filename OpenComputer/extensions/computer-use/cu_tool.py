"""``ComputerUseTool`` — universal (any-model) macOS desktop control.

Ported from hermes-agent ``tools/computer_use/tool.py``. The hermes handler
``handle_computer_use(args) -> dict`` is restructured here as
``BaseTool.execute(call: ToolCall) -> ToolResult``.

Return contract
---------------
``ToolResult.content`` is a plain string in OpenComputer, so this tool
returns JSON strings for every action. For capture / capture_after results,
the PNG is written to disk and the path is surfaced as ``screenshot_path``
in the JSON — the agent shares it with the user via ``MEDIA:<path>`` (the
same convention ``browser-harness``'s ``browser_vision`` uses). The base64
image is NOT inlined into ``content`` (would blow the token budget and
``ToolResult`` carries no image field).

Safety
------
* Mutating actions (click/type/scroll/drag/key/set_value/focus_app) are
  declared under a single ``CapabilityClaim`` at ``ConsentTier.EXPLICIT``
  on the tool class — the core ConsentGate gates the whole tool. ``capture``,
  ``wait`` and ``list_apps`` are read-only but share the claim because a
  single ``BaseTool`` cannot differentiate per-action (the spec's "claim
  EXPLICIT for the whole tool — safer" rule).
* Destructive shell ``type`` patterns and destructive system key combos are
  hard-blocked regardless of consent.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar

from cu_backend import (  # type: ignore[import-not-found]
    ActionResult,
    CaptureResult,
    ComputerUseBackend,
    UIElement,
)

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger("opencomputer.computer_use.tool")


# ---------------------------------------------------------------------------
# Safety constants
# ---------------------------------------------------------------------------

# Actions that read, not mutate.
_SAFE_ACTIONS = frozenset({"capture", "wait", "list_apps"})

# Actions that mutate user-visible state.
_DESTRUCTIVE_ACTIONS = frozenset({
    "click", "double_click", "right_click", "middle_click",
    "drag", "scroll", "type", "key", "set_value", "focus_app",
})

# Hard-blocked key combinations — destructive regardless of consent
# (e.g. logout kills the session OpenComputer runs in).
_BLOCKED_KEY_COMBOS = {
    frozenset({"cmd", "shift", "backspace"}),    # empty trash
    frozenset({"cmd", "option", "backspace"}),   # force delete
    frozenset({"cmd", "ctrl", "q"}),             # lock screen
    frozenset({"cmd", "shift", "q"}),            # log out
    frozenset({"cmd", "option", "shift", "q"}),  # force log out
}

_KEY_ALIASES = {"command": "cmd", "control": "ctrl", "alt": "option", "⌘": "cmd", "⌥": "option"}

# Dangerous text patterns for free-text-entry actions (`type`, `set_value`).
# The curl/wget remote-pipe patterns cover BOTH ``| bash`` and ``| sh``
# (and ``| zsh``) — an attacker piping a fetched script to ``sh`` is exactly
# as dangerous as piping it to ``bash``, so a ``bash``-only wget rule was a
# blocklist gap.
_BLOCKED_TYPE_PATTERNS = [
    re.compile(r"curl\s+[^|]*\|\s*(?:bash|sh|zsh)\b", re.IGNORECASE),
    re.compile(r"wget\s+[^|]*\|\s*(?:bash|sh|zsh)\b", re.IGNORECASE),
    re.compile(r"\bsudo\s+rm\s+-[rf]", re.IGNORECASE),
    re.compile(r"\brm\s+-rf\s+/\s*$", re.IGNORECASE),
    re.compile(r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}", re.IGNORECASE),  # fork bomb
]


def _canon_key_combo(keys: str) -> frozenset:
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", keys) if p.strip()]
    parts = [_KEY_ALIASES.get(p, p) for p in parts]
    return frozenset(parts)


def _is_blocked_type(text: str) -> str | None:
    for pat in _BLOCKED_TYPE_PATTERNS:
        if pat.search(text):
            return pat.pattern
    return None


# ---------------------------------------------------------------------------
# Backend selection — env-swappable for tests
# ---------------------------------------------------------------------------

_backend_lock = threading.Lock()
_backend: ComputerUseBackend | None = None


def _get_backend() -> ComputerUseBackend:
    """Return the per-process cached backend, instantiating + starting it once."""
    global _backend
    with _backend_lock:
        if _backend is None:
            backend_name = os.environ.get("OPENCOMPUTER_COMPUTER_USE_BACKEND", "cua").lower()
            if backend_name in {"cua", "cua-driver", ""}:
                from cu_cua_backend import CuaDriverBackend  # type: ignore[import-not-found]
                _backend = CuaDriverBackend()
            elif backend_name == "noop":
                _backend = NoopBackend()
            else:
                raise RuntimeError(
                    f"Unknown OPENCOMPUTER_COMPUTER_USE_BACKEND={backend_name!r}"
                )
            _backend.start()
        return _backend


def reset_backend_for_tests() -> None:
    """Test helper — tear down the cached backend."""
    global _backend
    with _backend_lock:
        if _backend is not None:
            try:
                _backend.stop()
            except Exception:
                pass
        _backend = None


class NoopBackend(ComputerUseBackend):
    """Test/CI stub. Records calls; returns trivial results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._started = False

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def is_available(self) -> bool:
        return True

    def capture(self, mode: str = "som", app: str | None = None) -> CaptureResult:
        self.calls.append(("capture", {"mode": mode, "app": app}))
        return CaptureResult(mode=mode, width=1024, height=768, png_b64=None,
                             elements=[], app=app or "", window_title="")

    def click(self, **kw: Any) -> ActionResult:
        self.calls.append(("click", kw))
        return ActionResult(ok=True, action="click")

    def drag(self, **kw: Any) -> ActionResult:
        self.calls.append(("drag", kw))
        return ActionResult(ok=True, action="drag")

    def scroll(self, **kw: Any) -> ActionResult:
        self.calls.append(("scroll", kw))
        return ActionResult(ok=True, action="scroll")

    def type_text(self, text: str) -> ActionResult:
        self.calls.append(("type", {"text": text}))
        return ActionResult(ok=True, action="type")

    def key(self, keys: str) -> ActionResult:
        self.calls.append(("key", {"keys": keys}))
        return ActionResult(ok=True, action="key")

    def set_value(self, value: str, element: int | None = None) -> ActionResult:
        self.calls.append(("set_value", {"value": value, "element": element}))
        return ActionResult(ok=True, action="set_value")

    def list_apps(self) -> list[dict[str, Any]]:
        self.calls.append(("list_apps", {}))
        return []

    def focus_app(self, app: str, raise_window: bool = False) -> ActionResult:
        self.calls.append(("focus_app", {"app": app, "raise": raise_window}))
        return ActionResult(ok=True, action="focus_app")


# ---------------------------------------------------------------------------
# Screenshot persistence
# ---------------------------------------------------------------------------

def _screenshots_dir() -> Path | None:
    """Directory where capture PNGs are persisted, or ``None`` if uncreatable.

    Honors ``OPENCOMPUTER_PROFILE_HOME`` (set by the hook env / runtime) so
    captures land inside the active profile; falls back to the system
    temp dir when no profile home is known. Returns ``None`` (rather than
    raising) when the directory cannot be created — a non-writable cache
    must degrade to "no screenshot path", never kill the whole capture.
    """
    base = os.environ.get("OPENCOMPUTER_PROFILE_HOME")
    if base:
        out = Path(base) / "cache" / "computer_use_screenshots"
    else:
        import tempfile
        out = Path(tempfile.gettempdir()) / "opencomputer_computer_use_screenshots"
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("computer_use screenshot dir uncreatable (%s): %s", out, e)
        return None
    return out


def _cleanup_old_screenshots(directory: Path, max_age_hours: float = 24.0) -> None:
    """Prune capture images older than ``max_age_hours`` to bound disk usage.

    Globs both ``.png`` and ``.jpg`` — ``_persist_png`` writes either,
    depending on whether cua-driver returned PNG or JPEG bytes.
    """
    cutoff = time.time() - max_age_hours * 3600.0
    try:
        stale = [p for p in directory.iterdir()
                 if p.name.startswith("computer_use_")
                 and p.suffix in (".png", ".jpg")]
    except OSError:
        return
    for p in stale:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def _persist_png(png_b64: str) -> str | None:
    """Decode + write a base64 image to disk, return its absolute path."""
    try:
        raw = base64.b64decode(png_b64, validate=False)
    except Exception:
        return None
    if not raw:
        return None
    # Detect format from magic bytes — cua-driver may return JPEG or PNG.
    ext = "jpg" if raw[:3] == b"\xff\xd8\xff" else "png"
    directory = _screenshots_dir()
    if directory is None:
        return None
    _cleanup_old_screenshots(directory)
    path = directory / f"computer_use_{uuid.uuid4().hex}.{ext}"
    try:
        path.write_bytes(raw)
    except OSError as e:
        logger.warning("failed to persist computer_use screenshot: %s", e)
        return None
    return str(path)


# ---------------------------------------------------------------------------
# Response shaping
# ---------------------------------------------------------------------------

def _format_elements(elements: list[UIElement], max_lines: int = 40) -> list[str]:
    out: list[str] = []
    for e in elements[:max_lines]:
        label = e.label.replace("\n", " ")[:60]
        # The cua-driver backend's AX-tree rendering carries no per-element
        # pixel bounds — `bounds` is the (0,0,0,0) sentinel there. Emitting
        # ``@ (0, 0, 0, 0)`` for every row would falsely suggest every
        # element sits at the origin; only show bounds when they are real.
        bounds = "" if e.bounds == (0, 0, 0, 0) else f" @ {e.bounds}"
        out.append(f"  #{e.index} {e.role} {label!r}{bounds}"
                   + (f" [{e.app}]" if e.app else ""))
    if len(elements) > max_lines:
        out.append(f"  ... +{len(elements) - max_lines} more (call capture with app= to narrow)")
    return out


def _element_to_dict(e: UIElement) -> dict[str, Any]:
    return {
        "index": e.index,
        "role": e.role,
        "label": e.label,
        "bounds": list(e.bounds),
        "app": e.app,
    }


def _capture_payload(cap: CaptureResult) -> dict[str, Any]:
    """Build the JSON-serialisable payload for a CaptureResult."""
    element_index = _format_elements(cap.elements)
    summary_lines = [
        f"capture mode={cap.mode} {cap.width}x{cap.height}"
        + (f" app={cap.app}" if cap.app else "")
        + (f" window={cap.window_title!r}" if cap.window_title else ""),
        f"{len(cap.elements)} interactable element(s):",
    ]
    if element_index:
        summary_lines.extend(element_index)
    summary = "\n".join(summary_lines)

    payload: dict[str, Any] = {
        "mode": cap.mode,
        "width": cap.width,
        "height": cap.height,
        "app": cap.app,
        "window_title": cap.window_title,
        "elements": [_element_to_dict(e) for e in cap.elements],
        "summary": summary,
        "png_bytes": cap.png_bytes_len,
    }
    # A failed capture must not masquerade as a clean "0 elements" result —
    # surface the backend's error so the agent retries (re-list windows,
    # switch capture mode) instead of acting on stale assumptions.
    if cap.error:
        payload["error"] = cap.error
        payload["hint"] = (
            "capture did not complete — re-run capture (optionally with a "
            "different app=) or, for screenshot failures, try mode='ax'."
        )
    if cap.png_b64 and cap.mode != "ax":
        screenshot_path = _persist_png(cap.png_b64)
        if screenshot_path:
            payload["screenshot_path"] = screenshot_path
            payload["share_hint"] = (
                "Include MEDIA:" + screenshot_path
                + " in your reply to show the user this screenshot."
            )
    return payload


def _action_payload(res: ActionResult) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": res.ok, "action": res.action}
    if res.message:
        payload["message"] = res.message
    if res.meta:
        payload["meta"] = res.meta
    return payload


# ---------------------------------------------------------------------------
# Dispatch — pure functions, exercised directly by tests
# ---------------------------------------------------------------------------

def _coerce_element(raw: Any) -> int | None:
    """Coerce an ``element`` / ``from_element`` / ``to_element`` arg to an int.

    The schema types these ``integer``, but ``strict_mode`` is off (the
    action discriminator makes most params conditionally-unused), so the API
    does NOT enforce it. A model can hand us ``"14"`` (string), ``14.0``
    (float), or ``[14]`` (list). cua-driver's MCP ``element_index`` arg is a
    strict ``integer`` — a mistyped value is a bad-typed MCP call exactly
    like a mistyped coordinate. Coerce to a clean ``int`` or ``None`` (the
    "no element index" sentinel) so the backend never forwards a bad type.
    A bare ``bool`` is rejected: ``True``/``False`` are not element indices
    even though ``isinstance(True, int)`` holds.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw.strip())
        except ValueError:
            return None
    return None


def _coerce_xy(raw: Any) -> tuple[int, int] | None:
    """Coerce a ``coordinate``-shaped arg into an ``(x, y)`` int pair.

    ``strict_mode`` is off (the action discriminator makes most params
    conditionally-unused), so the API does NOT enforce the schema's
    ``[integer, integer]`` ``minItems/maxItems`` on coordinate fields. A
    model can therefore hand us a scalar, a one-element list, or strings.
    Coerce defensively: return a clean ``(x, y)`` or ``None`` — never let a
    malformed coordinate raise inside ``_dispatch`` or reach the backend as
    a bad-typed MCP arg.
    """
    if raw is None:
        return None
    if not isinstance(raw, list | tuple) or len(raw) != 2:
        return None
    try:
        return int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None


def _dispatch(backend: ComputerUseBackend, action: str, args: dict[str, Any]) -> dict[str, Any]:
    """Route one action to the backend and return a JSON-serialisable dict."""
    capture_after = bool(args.get("capture_after"))

    if action == "capture":
        mode = str(args.get("mode", "som"))
        if mode not in {"som", "vision", "ax"}:
            return {"error": f"bad mode {mode!r}; use som|vision|ax"}
        raw_app = args.get("app")
        # strict_mode is off, so the schema's ``string`` type on ``app`` is
        # not API-enforced — normalise a non-string/empty form to None so the
        # backend's ``app.lower()`` filter never trips on a bad type.
        app = str(raw_app) if isinstance(raw_app, str) and raw_app.strip() else None
        cap = backend.capture(mode=mode, app=app)
        return _capture_payload(cap)

    if action == "wait":
        try:
            seconds = float(args.get("seconds", 1.0))
        except (TypeError, ValueError):
            return {"error": "wait `seconds` must be a number"}
        return _action_payload(backend.wait(seconds))

    if action == "list_apps":
        apps = backend.list_apps()
        return {"apps": apps, "count": len(apps)}

    if action == "focus_app":
        raw_app = args.get("app")
        if not isinstance(raw_app, str) or not raw_app.strip():
            return {"error": "focus_app requires `app` (a non-empty app name or bundle ID)"}
        res = backend.focus_app(raw_app, raise_window=bool(args.get("raise_window")))
        return _maybe_follow_capture(backend, res, capture_after)

    if action in {"click", "double_click", "right_click", "middle_click"}:
        button = args.get("button")
        click_count = 1
        if action == "double_click":
            click_count = 2
        elif action == "right_click":
            button = "right"
        elif action == "middle_click":
            button = "middle"
        else:
            button = button or "left"
        element = _coerce_element(args.get("element"))
        xy = _coerce_xy(args.get("coordinate"))
        x, y = xy if xy is not None else (None, None)
        res = backend.click(
            element=element,
            x=x, y=y, button=button or "left", click_count=click_count,
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "drag":
        res = backend.drag(
            from_element=_coerce_element(args.get("from_element")),
            to_element=_coerce_element(args.get("to_element")),
            from_xy=_coerce_xy(args.get("from_coordinate")),
            to_xy=_coerce_xy(args.get("to_coordinate")),
            button=args.get("button", "left"),
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "scroll":
        xy = _coerce_xy(args.get("coordinate"))
        try:
            amount = int(args.get("amount", 3))
        except (TypeError, ValueError):
            amount = 3
        res = backend.scroll(
            direction=args.get("direction", "down"),
            amount=amount,
            element=_coerce_element(args.get("element")),
            x=xy[0] if xy is not None else None,
            y=xy[1] if xy is not None else None,
            modifiers=args.get("modifiers"),
        )
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "type":
        res = backend.type_text(args.get("text", ""))
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "key":
        res = backend.key(args.get("keys", ""))
        return _maybe_follow_capture(backend, res, capture_after)

    if action == "set_value":
        value = args.get("value")
        if value is None:
            return {"error": "set_value requires `value`"}
        # set_value is a CuaDriverBackend-specific extension to the ABC.
        set_value = getattr(backend, "set_value", None)
        if set_value is None:
            return {"error": "set_value is not supported by the active backend"}
        res = set_value(value=str(value), element=_coerce_element(args.get("element")))
        return _maybe_follow_capture(backend, res, capture_after)

    return {"error": f"unknown action {action!r}"}


def _maybe_follow_capture(
    backend: ComputerUseBackend, res: ActionResult, do_capture: bool,
) -> dict[str, Any]:
    payload = _action_payload(res)
    if not do_capture:
        return payload
    # The follow-up MUST verify the window the action just touched — not
    # whatever is frontmost. ``backend.capture(mode='som')`` re-runs
    # frontmost-first window selection, so a ``type``+``capture_after``
    # against a backgrounded app would silently come back showing the
    # frontmost window instead — the model would "verify" the wrong UI.
    # ``recapture_active`` pins to the sticky pid/window_id the action
    # addressed. It is a CuaDriverBackend extension; backends without it
    # (NoopBackend) fall back to the plain frontmost capture.
    recapture = getattr(backend, "recapture_active", None)
    try:
        cap = recapture(mode="som") if recapture is not None \
            else backend.capture(mode="som")
    except Exception as e:
        logger.warning("follow-up capture failed: %s", e)
        return payload
    cap_payload = _capture_payload(cap)
    cap_payload["action"] = res.action
    cap_payload["ok"] = res.ok
    if res.message:
        cap_payload["message"] = res.message
    return cap_payload


def run_computer_use(args: dict[str, Any]) -> dict[str, Any]:
    """Validate + dispatch one ``computer_use`` call. Returns a result dict.

    This is the synchronous core; ``ComputerUseTool.execute`` wraps it in the
    async ``ToolCall``/``ToolResult`` contract.
    """
    # ``strict_mode`` is off (the action discriminator makes most params
    # conditionally-unused), so the API does NOT enforce the schema's
    # ``string`` type on ``action``. A model can hand us ``123``, a list,
    # or a dict — ``(123 or "").strip()`` raises a raw ``AttributeError``.
    # ``execute`` would catch that as defence-in-depth, but ``run_computer_use``
    # is also a direct entry point (tests, and any future caller); it must
    # never leak a raw exception. Coerce a non-string ``action`` to str so
    # the validation below (and the unknown-action branch) handles it cleanly.
    raw_action = args.get("action")
    if raw_action is None:
        return {"error": "missing `action`"}
    action = (raw_action if isinstance(raw_action, str) else str(raw_action)).strip().lower()
    if not action:
        return {"error": "missing `action`"}

    # Safety: validate destructive payloads before touching the backend.
    # Both `type` (text=) and `set_value` (value=) inject arbitrary free
    # text into a UI element — a Terminal text field accepts a piped-curl
    # payload just as readily through `set_value` as through `type`, so the
    # dangerous-shell-pattern guard MUST cover both. Checking only `type`
    # would leave `set_value` as an open bypass of the hard block.
    if action in {"type", "set_value"}:
        injected = args.get("text" if action == "type" else "value", "")
        pat = _is_blocked_type("" if injected is None else str(injected))
        if pat:
            field = "type text" if action == "type" else "set_value value"
            return {
                "error": f"blocked pattern in {field}: {pat!r}",
                "hint": "Dangerous shell patterns cannot be entered via computer_use.",
            }

    if action == "key":
        # strict_mode is off — coerce a non-string ``keys`` to str so the
        # combo parse (here and in the backend) never trips on a bad type
        # and the hard-block check still runs on the security-critical path.
        raw_keys = args.get("keys", "")
        keys = raw_keys if isinstance(raw_keys, str) else str(raw_keys or "")
        args["keys"] = keys
        combo = _canon_key_combo(keys)
        for blocked in _BLOCKED_KEY_COMBOS:
            if blocked.issubset(combo) and len(blocked) <= len(combo):
                return {
                    "error": f"blocked key combo: {sorted(blocked)}",
                    "hint": "Destructive system shortcuts are hard-blocked.",
                }

    if action not in _SAFE_ACTIONS and action not in _DESTRUCTIVE_ACTIONS:
        return {"error": f"unknown action {action!r}"}

    try:
        backend = _get_backend()
    except Exception as e:
        # Some backend-start failures (a closed stdio pipe, a bare
        # ``McpError``, a cancelled async context) stringify to "" —
        # ``f"...: {e}"`` would then leave the model an unactionable
        # "backend unavailable: " with nothing after the colon. Fall
        # back to ``repr`` (always carries the exception type) so the
        # error is never empty.
        detail = str(e) or repr(e)
        return {
            "error": f"computer_use backend unavailable: {detail}",
            "hint": "Run `oc doctor --fix` and accept the cua-driver repair, "
                    "or `oc computer-use install`.",
        }

    try:
        return _dispatch(backend, action, args)
    except Exception as e:
        logger.exception("computer_use %s failed", action)
        return {"error": f"{action} failed: {e}"}


# ---------------------------------------------------------------------------
# The BaseTool
# ---------------------------------------------------------------------------

#: F1 capability — the whole tool gates at EXPLICIT. A BaseTool cannot
#: vary its claim per-action, and the destructive action set dominates,
#: so the spec's "claim EXPLICIT for the whole tool — safer" rule applies.
COMPUTER_USE_CAPABILITY = CapabilityClaim(
    capability_id="computer_use.macos_desktop_control",
    tier_required=ConsentTier.EXPLICIT,
    human_description=(
        "Control the macOS desktop in the background — take screenshots, "
        "move/click the mouse, type, scroll, and send keystrokes to any app "
        "(including hidden / off-Space windows) without stealing your cursor "
        "or keyboard focus."
    ),
    data_scope="macos:desktop",
)


def _import_schema() -> dict[str, Any]:
    from cu_schema import COMPUTER_USE_SCHEMA  # type: ignore[import-not-found]
    return COMPUTER_USE_SCHEMA


class ComputerUseTool(BaseTool):
    """Universal macOS desktop control via the cua-driver MCP backend."""

    #: Desktop actions mutate global UI state — never run two concurrently.
    parallel_safe: ClassVar[bool] = False

    #: Whole-tool EXPLICIT consent gate (mutating actions dominate).
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (COMPUTER_USE_CAPABILITY,)

    #: The schema does NOT set ``additionalProperties: false`` (the action
    #: discriminator means most properties are conditionally-unused), so
    #: strict mode stays off — opting in would reject valid calls.
    strict_mode: ClassVar[bool] = False

    @property
    def schema(self) -> ToolSchema:
        raw = _import_schema()
        return ToolSchema(
            name=raw["name"],
            description=raw["description"],
            parameters=raw["parameters"],
        )

    @staticmethod
    def is_available() -> bool:
        """True iff computer_use can run on this host (macOS + cua-driver)."""
        if sys.platform != "darwin":
            return False
        try:
            from cu_cua_backend import (  # type: ignore[import-not-found]
                cua_driver_binary_available,
            )
        except Exception:
            return False
        return cua_driver_binary_available()

    async def execute(self, call: ToolCall) -> ToolResult:
        args = dict(call.arguments or {})
        if sys.platform != "darwin":
            return ToolResult(
                tool_call_id=call.id,
                content=json.dumps({
                    "error": "computer_use is macOS-only",
                    "hint": "This tool drives the macOS desktop via cua-driver "
                            "and is unavailable on this platform.",
                }),
                is_error=True,
            )
        try:
            result = run_computer_use(args)
        except Exception as e:  # defence in depth — execute must never raise
            logger.exception("computer_use execute failed")
            result = {"error": f"computer_use failed: {e}"}

        # A result is an error if it carries an ``error`` key (bad args,
        # blocked pattern, failed capture) OR if it is an action payload
        # whose backend ``ActionResult.ok`` came back ``False`` — a click
        # that AXPress-failed, a type with no active window, a focus_app
        # that matched nothing. Without the ``ok is False`` arm a failed
        # mutating action would reach the model as a clean (non-error)
        # tool result, and the model's error-handling path (re-capture,
        # retry, switch strategy) would never fire — the exact silent
        # failure that makes a multi-step desktop workflow flail.
        is_error = ("error" in result) or (result.get("ok") is False)
        return ToolResult(
            tool_call_id=call.id,
            content=json.dumps(result, ensure_ascii=False),
            is_error=is_error,
        )


__all__ = [
    "ComputerUseTool",
    "COMPUTER_USE_CAPABILITY",
    "NoopBackend",
    "run_computer_use",
    "reset_backend_for_tests",
    "_get_backend",
    "_dispatch",
]
