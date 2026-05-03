"""The single ``Browser`` discriminator tool + deprecation shims.

Filename note: this lives at ``_tool.py`` (singular, leading-underscore)
even though BLUEPRINT §3 / BRIEF-06 call it ``tools.py``. PR #394 burned
in the lesson that a top-level ``tools`` module collides with
coding-harness's ``tools/`` subpackage via Python's ``sys.modules``
cache. The leading ``_`` keeps us out of that race the same way
``_tools.py`` / ``_browser_session.py`` did pre-W3.

Surface registered:

  - ``Browser`` — one tool with two-level discriminator (16 outer
    ``action`` values + 11 inner ``act.kind`` values per BLUEPRINT §5).
  - 11 deprecation shims that accept the old per-tool args, dispatch to
    ``Browser``, and emit ``DeprecationWarning`` once per process.

The shims unblock the soft-cutover migration path: skills + docs that
still reference ``browser_navigate`` / ``browser_click`` / etc. continue
to work for one minor release, with a loud-once warning so authors know
to migrate. They sunset in 0.X+1.
"""

from __future__ import annotations

import json
import logging
import warnings
from typing import Any, ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# Imports go through the ``extensions.browser_control`` package
# (synthesised in ``plugin.py::_bootstrap_package_namespace`` at
# runtime, registered by ``tests/conftest.py`` under tests). The
# package form is required for the relative imports inside ``client/``
# and ``server/`` to resolve.
from extensions.browser_control.client import (  # type: ignore[import-not-found]
    BrowserActions,
    BrowserAuth,
    BrowserServiceError,
)
from extensions.browser_control.schema import (  # type: ignore[import-not-found]
    BrowserAction,
    BrowserActKind,
    browser_params_json_schema,
)

_log = logging.getLogger("opencomputer.browser_control.tool")

#: Module-level dedupe — each warning fires once per process.
_emitted: set[str] = set()


# ─── Browser tool — the single discriminator surface ──────────────────


_BROWSER_TOOL_DESCRIPTION = (
    "Control the browser via OpenComputer's browser control service "
    "(status/start/stop/profiles/tabs/open/snapshot/screenshot/navigate/"
    "act/...). Profile defaults to 'openclaw' (isolated, agent-managed). "
    "Use profile='user' for the user's logged-in Chrome (host-only; "
    "existing-session). When using refs returned by snapshot (e.g. "
    "'e12'), keep the same tab: pass targetId from the snapshot response "
    "into subsequent actions. For element-level operations, set "
    "action='act' and provide either nested 'request: {kind: ...}' or "
    "the flat-form sibling fields (kind/ref/text/.../selector/etc)."
)


class Browser(BaseTool):
    """Single discriminator tool covering the full browser surface."""

    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="browser.navigate",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Drive the browser (navigate, click, fill, ...).",
        ),
    )

    def __init__(
        self,
        *,
        actions: BrowserActions | None = None,
        consent_gate: Any | None = None,
        sandbox: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        self._actions = actions or BrowserActions()
        self._consent_gate = consent_gate
        self._sandbox = sandbox
        self._audit = audit

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Browser",
            description=_BROWSER_TOOL_DESCRIPTION,
            parameters=browser_params_json_schema(),
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            return await self._dispatch(call)
        except BrowserServiceError as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Browser error: {exc}",
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            _log.exception("Browser tool raised", exc_info=exc)
            return ToolResult(
                tool_call_id=call.id,
                content=f"Browser internal error: {exc}",
                is_error=True,
            )

    async def _dispatch(self, call: ToolCall) -> ToolResult:
        args = dict(call.arguments or {})
        raw_action = args.get("action")
        if not raw_action:
            return ToolResult(
                tool_call_id=call.id,
                content="Browser error: missing required field 'action'",
                is_error=True,
            )
        try:
            action = BrowserAction(raw_action)
        except ValueError:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Browser error: unknown action {raw_action!r}. "
                    f"Expected one of: {', '.join(a.value for a in BrowserAction)}"
                ),
                is_error=True,
            )

        profile = _opt_str(args.get("profile"))
        base_url = _opt_str(args.get("baseUrl") or args.get("base_url"))

        actions = self._actions

        if action is BrowserAction.STATUS:
            data = await actions.browser_status(profile=profile, base_url=base_url)
        elif action is BrowserAction.PROFILES:
            data = await actions.browser_profiles(base_url=base_url)
        elif action is BrowserAction.START:
            data = await actions.browser_start(profile=profile, base_url=base_url)
        elif action is BrowserAction.STOP:
            data = await actions.browser_stop(profile=profile, base_url=base_url)
        elif action is BrowserAction.TABS:
            data = await actions.browser_tabs(profile=profile, base_url=base_url)
        elif action is BrowserAction.OPEN:
            url = _required(args, "url")
            data = await actions.browser_open_tab(
                url=url, profile=profile, base_url=base_url
            )
        elif action is BrowserAction.FOCUS:
            target_id = _required(args, "targetId", "target_id")
            data = await actions.browser_focus_tab(
                target_id=target_id, profile=profile, base_url=base_url
            )
        elif action is BrowserAction.CLOSE:
            target_id = _required(args, "targetId", "target_id")
            data = await actions.browser_close_tab(
                target_id=target_id, profile=profile, base_url=base_url
            )
        elif action is BrowserAction.SNAPSHOT:
            data = await actions.browser_snapshot(
                target_id=_opt_str(args.get("targetId") or args.get("target_id")),
                mode=_opt_str(args.get("mode")),
                profile=profile,
                base_url=base_url,
            )
        elif action is BrowserAction.SCREENSHOT:
            data = await actions.browser_screenshot(
                target_id=_opt_str(args.get("targetId") or args.get("target_id")),
                full_page=args.get("fullPage") if args.get("fullPage") is not None
                else args.get("full_page"),
                ref=_opt_str(args.get("ref")),
                profile=profile,
                base_url=base_url,
            )
        elif action is BrowserAction.NAVIGATE:
            url = _required(args, "url")
            data = await actions.browser_navigate(
                url=url,
                target_id=_opt_str(args.get("targetId") or args.get("target_id")),
                profile=profile,
                base_url=base_url,
            )
        elif action is BrowserAction.CONSOLE:
            data = await actions.browser_console(
                target_id=_opt_str(args.get("targetId") or args.get("target_id")),
                level=_opt_str(args.get("level")),
                profile=profile,
                base_url=base_url,
            )
        elif action is BrowserAction.PDF:
            data = await actions.browser_pdf(
                target_id=_opt_str(args.get("targetId") or args.get("target_id")),
                profile=profile,
                base_url=base_url,
            )
        elif action is BrowserAction.UPLOAD:
            # arm a file chooser and stage paths for the next file-input click
            paths = args.get("paths") or args.get("files")
            if paths is None:
                return ToolResult(
                    tool_call_id=call.id,
                    content="Browser error: action='upload' requires 'paths'",
                    is_error=True,
                )
            data = await actions.browser_arm_file_chooser(
                paths=paths,
                ref=_opt_str(args.get("ref")),
                profile=profile,
                base_url=base_url,
            )
        elif action is BrowserAction.DIALOG:
            data = await actions.browser_arm_dialog(
                accept=bool(args.get("accept", True)),
                promptText=_opt_str(args.get("promptText") or args.get("prompt_text")),
                profile=profile,
                base_url=base_url,
            )
        elif action is BrowserAction.ACT:
            request = _build_act_request(args)
            if request is None:
                return ToolResult(
                    tool_call_id=call.id,
                    content=(
                        "Browser error: action='act' requires 'request: {kind: ...}' "
                        "or a flat 'kind' field with the matching parameters."
                    ),
                    is_error=True,
                )
            data = await actions.browser_act(
                request, profile=profile, base_url=base_url
            )
        else:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Browser error: action {action.value!r} not yet wired",
                is_error=True,
            )

        return ToolResult(tool_call_id=call.id, content=_jsonify(data))


def _required(args: dict[str, Any], *names: str) -> str:
    for n in names:
        v = args.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise BrowserServiceError(
        f"missing required field(s): {' or '.join(names)}"
    )


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return str(v)


def _build_act_request(args: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the inner act-request out of ``args``.

    Accepts either a nested ``request: {...}`` blob OR flat sibling
    fields (``kind`` + the relevant act-shape fields).
    """
    nested = args.get("request")
    if isinstance(nested, dict):
        if not nested.get("kind"):
            return None
        return dict(nested)
    raw_kind = args.get("kind")
    if not raw_kind:
        return None
    try:
        kind = BrowserActKind(raw_kind)
    except ValueError:
        return None
    out: dict[str, Any] = {"kind": kind.value}
    for k in (
        "ref", "text", "key", "selector", "fields", "values", "options",
        "timeoutMs", "timeout_ms", "expression", "state", "width", "height",
        "delta", "target",
    ):
        if k in args and args[k] is not None:
            # normalize timeout_ms → timeoutMs for the wire
            wire_key = "timeoutMs" if k == "timeout_ms" else k
            out[wire_key] = args[k]
    return out


def _jsonify(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, (bytes, bytearray)):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", errors="replace")
    try:
        return json.dumps(data, default=str)
    except (TypeError, ValueError):
        return str(data)


# ─── deprecation shims ─────────────────────────────────────────────────


def _emit_deprecation_once(name: str, replacement: str) -> None:
    """Fire DeprecationWarning at most once per process per name."""
    if name in _emitted:
        return
    _emitted.add(name)
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead. "
        "The legacy name will be removed in the next minor release.",
        DeprecationWarning,
        stacklevel=3,
    )


def _make_shim(
    *,
    legacy_name: str,
    replacement_hint: str,
    capability_id: str,
    tier_required: ConsentTier,
    human_description: str,
    description: str,
    parameters: dict[str, Any],
    build_browser_args: Any,
    consent_tier_attr: int = 2,
) -> type[BaseTool]:
    """Construct a one-off ``BaseTool`` subclass that shims to ``Browser``."""

    cls_capability_claims: tuple[CapabilityClaim, ...] = (
        CapabilityClaim(
            capability_id=capability_id,
            tier_required=tier_required,
            human_description=human_description,
        ),
    )

    class _Shim(BaseTool):
        consent_tier: int = consent_tier_attr
        parallel_safe: bool = True
        capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = cls_capability_claims

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
            self._inner = Browser()

        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(
                name=legacy_name,
                description=description,
                parameters=parameters,
            )

        async def execute(self, call: ToolCall) -> ToolResult:
            _emit_deprecation_once(legacy_name, replacement_hint)
            browser_args = build_browser_args(call.arguments or {})
            wrapped = ToolCall(id=call.id, name="Browser", arguments=browser_args)
            return await self._inner.execute(wrapped)

    _Shim.__name__ = legacy_name
    _Shim.__qualname__ = legacy_name
    return _Shim


def _navigate_args(a: dict[str, Any]) -> dict[str, Any]:
    return {"action": "navigate", "url": a.get("url", "")}


def _click_args(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "act",
        "kind": "click",
        "selector": a.get("selector"),
        "ref": a.get("ref"),
        "url": a.get("url"),
    }


def _fill_args(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "act",
        "kind": "fill",
        "selector": a.get("selector"),
        "text": a.get("value") or a.get("text"),
        "ref": a.get("ref"),
        "url": a.get("url"),
    }


def _snapshot_args(a: dict[str, Any]) -> dict[str, Any]:
    return {"action": "snapshot", "url": a.get("url")}


def _scrape_args(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "snapshot",
        "url": a.get("url"),
        "selector": a.get("css_selector") or a.get("selector"),
    }


def _scroll_args(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "act",
        "kind": "press",
        "key": _scroll_direction_to_key(a.get("direction", "down")),
        "url": a.get("url"),
    }


def _scroll_direction_to_key(direction: str) -> str:
    direction = (direction or "down").strip().lower()
    return {
        "down": "PageDown",
        "up": "PageUp",
        "top": "Home",
        "bottom": "End",
    }.get(direction, "PageDown")


def _back_args(a: dict[str, Any]) -> dict[str, Any]:
    # browser-back maps best to ``act/press`` of Alt+Left in legacy semantics;
    # there's no first-class back action in the discriminator surface.
    return {
        "action": "act",
        "kind": "press",
        "key": "Alt+ArrowLeft",
        "url": a.get("url"),
    }


def _press_args(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "act",
        "kind": "press",
        "key": a.get("key"),
        "selector": a.get("selector"),
        "url": a.get("url"),
    }


def _get_images_args(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "act",
        "kind": "evaluate",
        "expression": (
            "Array.from(document.images).slice(0, "
            f"{int(a.get('max_images') or 20)})"
            ".map(i => ({src: i.src, alt: i.alt, width: i.width, height: i.height}))"
        ),
        "url": a.get("url"),
    }


def _vision_args(a: dict[str, Any]) -> dict[str, Any]:
    return {"action": "screenshot", "url": a.get("url"), "fullPage": False}


def _console_args(a: dict[str, Any]) -> dict[str, Any]:
    return {"action": "console", "url": a.get("url")}


_SHIM_DEFS = (
    {
        "legacy_name": "browser_navigate",
        "replacement_hint": "Browser(action='navigate', url=...)",
        "capability_id": "browser.navigate",
        "tier_required": ConsentTier.EXPLICIT,
        "human_description": "Open a URL in the browser.",
        "description": (
            "[DEPRECATED — use Browser(action='navigate', url=...).] "
            "Navigate to a URL and return a snapshot. Sunsets next minor."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "build_browser_args": _navigate_args,
    },
    {
        "legacy_name": "browser_click",
        "replacement_hint": "Browser(action='act', kind='click', ...)",
        "capability_id": "browser.click",
        "tier_required": ConsentTier.EXPLICIT,
        "human_description": "Click an element.",
        "description": (
            "[DEPRECATED — use Browser(action='act', kind='click', ...).] "
            "Click an element by CSS selector after navigating to a URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "selector": {"type": "string"}},
            "required": ["url", "selector"],
        },
        "build_browser_args": _click_args,
    },
    {
        "legacy_name": "browser_fill",
        "replacement_hint": "Browser(action='act', kind='fill', ...)",
        "capability_id": "browser.fill",
        "tier_required": ConsentTier.EXPLICIT,
        "human_description": "Fill a form field.",
        "description": (
            "[DEPRECATED — use Browser(action='act', kind='fill', ...).] "
            "Fill a text input by selector after navigating to a URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "selector": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["url", "selector", "value"],
        },
        "build_browser_args": _fill_args,
    },
    {
        "legacy_name": "browser_snapshot",
        "replacement_hint": "Browser(action='snapshot', ...)",
        "capability_id": "browser.snapshot",
        "tier_required": ConsentTier.IMPLICIT,
        "human_description": "Read-only snapshot.",
        "description": (
            "[DEPRECATED — use Browser(action='snapshot', ...).] "
            "Read-only snapshot of a URL."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "build_browser_args": _snapshot_args,
        "consent_tier_attr": 1,
    },
    {
        "legacy_name": "browser_scrape",
        "replacement_hint": "Browser(action='snapshot', ...)",
        "capability_id": "browser.scrape",
        "tier_required": ConsentTier.IMPLICIT,
        "human_description": "Scrape page text.",
        "description": (
            "[DEPRECATED — use Browser(action='snapshot', ...).] "
            "Scrape text from a URL with optional selector."
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "css_selector": {"type": "string"}},
            "required": ["url"],
        },
        "build_browser_args": _scrape_args,
        "consent_tier_attr": 1,
    },
    {
        "legacy_name": "browser_scroll",
        "replacement_hint": "Browser(action='act', kind='press', key='PageDown', ...)",
        "capability_id": "browser.scroll",
        "tier_required": ConsentTier.IMPLICIT,
        "human_description": "Scroll the page.",
        "description": (
            "[DEPRECATED — use Browser(action='act', kind='press', "
            "key='PageDown'/'PageUp'/'Home'/'End', ...).]"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": ["up", "down", "top", "bottom"],
                },
                "amount_px": {"type": "integer"},
            },
            "required": ["url"],
        },
        "build_browser_args": _scroll_args,
    },
    {
        "legacy_name": "browser_back",
        "replacement_hint": "Browser(action='act', kind='press', key='Alt+ArrowLeft', ...)",
        "capability_id": "browser.navigate",
        "tier_required": ConsentTier.EXPLICIT,
        "human_description": "Navigate back.",
        "description": (
            "[DEPRECATED — use Browser(action='act', kind='press', "
            "key='Alt+ArrowLeft', ...).]"
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "build_browser_args": _back_args,
    },
    {
        "legacy_name": "browser_press",
        "replacement_hint": "Browser(action='act', kind='press', key=..., ...)",
        "capability_id": "browser.fill",
        "tier_required": ConsentTier.EXPLICIT,
        "human_description": "Press a key.",
        "description": (
            "[DEPRECATED — use Browser(action='act', kind='press', "
            "key=..., ...).]"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "key": {"type": "string"},
                "selector": {"type": "string"},
            },
            "required": ["url", "key"],
        },
        "build_browser_args": _press_args,
    },
    {
        "legacy_name": "browser_get_images",
        "replacement_hint": "Browser(action='act', kind='evaluate', expression='document.images...', ...)",
        "capability_id": "browser.scrape",
        "tier_required": ConsentTier.IMPLICIT,
        "human_description": "List images.",
        "description": (
            "[DEPRECATED — use Browser(action='act', kind='evaluate', ...).]"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_images": {"type": "integer"},
            },
            "required": ["url"],
        },
        "build_browser_args": _get_images_args,
        "consent_tier_attr": 1,
    },
    {
        "legacy_name": "browser_vision",
        "replacement_hint": "Browser(action='screenshot', ...)",
        "capability_id": "browser.screenshot",
        "tier_required": ConsentTier.EXPLICIT,
        "human_description": "Capture a screenshot.",
        "description": (
            "[DEPRECATED — use Browser(action='screenshot', ...).]"
        ),
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "build_browser_args": _vision_args,
    },
    {
        "legacy_name": "browser_console",
        "replacement_hint": "Browser(action='console', ...)",
        "capability_id": "browser.scrape",
        "tier_required": ConsentTier.IMPLICIT,
        "human_description": "Read console messages.",
        "description": (
            "[DEPRECATED — use Browser(action='console', ...).]"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_messages": {"type": "integer"},
            },
            "required": ["url"],
        },
        "build_browser_args": _console_args,
        "consent_tier_attr": 1,
    },
)


DEPRECATION_SHIMS: tuple[type[BaseTool], ...] = tuple(
    _make_shim(**defn) for defn in _SHIM_DEFS  # type: ignore[arg-type]
)


def reset_deprecation_warnings_for_tests() -> None:
    """Test helper — clears the once-per-process dedupe set."""
    _emitted.clear()


__all__ = [
    "Browser",
    "DEPRECATION_SHIMS",
    "reset_deprecation_warnings_for_tests",
]
