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
import os
import warnings
from typing import Any, ClassVar

# Imports go through the ``extensions.browser_control`` package
# (synthesised in ``plugin.py::_bootstrap_package_namespace`` at
# runtime, registered by ``tests/conftest.py`` under tests). The
# package form is required for the relative imports inside ``client/``
# and ``server/`` to resolve.
from extensions.browser_control.client import (  # type: ignore[import-not-found]
    BrowserActions,
    BrowserServiceError,
)
from extensions.browser_control.schema import (  # type: ignore[import-not-found]
    BrowserAction,
    BrowserActKind,
    browser_params_json_schema,
)

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.browser_control.tool")

#: Module-level dedupe — each warning fires once per process.
_emitted: set[str] = set()


async def _ensure_dispatcher_ready_or_raise() -> None:
    """Lazy-init the in-process dispatcher app on first Browser call.

    Wraps any bootstrap failure as a ``BrowserServiceError`` so the
    Browser ``execute()`` error path renders it as a model-visible tool
    error rather than a tool internal exception. Re-import locally so
    test reloads of ``_dispatcher_bootstrap`` (which swaps the module
    in ``sys.modules``) take effect — the import is cheap.
    """
    try:
        from extensions.browser_control._dispatcher_bootstrap import (  # type: ignore[import-not-found]
            ensure_dispatcher_app_ready,
        )

        await ensure_dispatcher_app_ready()
    except BrowserServiceError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise BrowserServiceError(
            f"Failed to initialize browser-control dispatcher: {exc}"
        ) from exc


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
        # Track whether the caller injected a custom actions object — in
        # that case (unit tests with a fake) we skip the dispatcher
        # bootstrap because the fake never routes through the in-process
        # FastAPI app anyway.
        self._actions_injected = actions is not None
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
            # Lazy bootstrap of the in-process dispatcher app. ``register()``
            # in plugin.py only registers the tool surface; the FastAPI app
            # is built (idempotently, single-flight) on first use here so
            # we don't pay the init cost when Browser is never invoked.
            # Skip when caller injected a custom actions stub — that's
            # the unit-test path; the stub never routes through the
            # in-process FastAPI app, so building it would be wasted.
            if not self._actions_injected:
                await _ensure_dispatcher_ready_or_raise()
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
        # ─── Wave 4 — adapter promotion + recon surface ─────────────
        elif action is BrowserAction.NETWORK_START:
            data = await _do_network_start(actions, args, profile, base_url)
        elif action is BrowserAction.NETWORK_LIST:
            data = await _do_network_list(actions, args, profile, base_url)
        elif action is BrowserAction.NETWORK_DETAIL:
            data = await _do_network_detail(actions, args, profile, base_url)
        elif action is BrowserAction.RESOURCE_TIMING:
            data = await _do_resource_timing(actions, args, profile, base_url)
        elif action is BrowserAction.ANALYZE:
            data = await _do_analyze(actions, args, profile, base_url)
        elif action is BrowserAction.ADAPTER_NEW:
            data = _do_adapter_new(args)
        elif action is BrowserAction.ADAPTER_SAVE:
            data = _do_adapter_save(args)
        elif action is BrowserAction.ADAPTER_VALIDATE:
            data = _do_adapter_validate(args)
        elif action is BrowserAction.VERIFY:
            data = await _do_verify(args, profile)
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


# ─── Wave 4 action handlers ────────────────────────────────────────────


async def _do_network_start(
    actions: Any, args: dict[str, Any], profile: str | None, base_url: str | None
) -> Any:
    """Begin capturing network requests on the active page.

    Implementation: clears the existing buffer + arms a fresh capture.
    The control service already buffers requests via Network.* events;
    we use the existing /requests endpoint with ``clear=true`` to reset
    the buffer, leaving capture armed for a subsequent ``network_list``.
    """
    target_id = _opt_str(args.get("targetId") or args.get("target_id"))
    return await actions.browser_requests(
        target_id=target_id, clear=True, profile=profile, base_url=base_url
    )


async def _do_network_list(
    actions: Any, args: dict[str, Any], profile: str | None, base_url: str | None
) -> Any:
    """Return captured requests (URL/method/status/...). Optional URL filter."""
    target_id = _opt_str(args.get("targetId") or args.get("target_id"))
    url_filter = _opt_str(args.get("filter") or args.get("url_pattern"))
    return await actions.browser_requests(
        target_id=target_id, filter=url_filter, profile=profile, base_url=base_url
    )


async def _do_network_detail(
    actions: Any, args: dict[str, Any], profile: str | None, base_url: str | None
) -> Any:
    """Get the full body for one request (by request_id or URL)."""
    request_id = _opt_str(args.get("requestId") or args.get("request_id"))
    if not request_id:
        raise BrowserServiceError(
            "network_detail requires 'requestId' (from network_list output)"
        )
    return await actions.browser_response_body(
        request_id=request_id, profile=profile, base_url=base_url
    )


async def _do_resource_timing(
    actions: Any, args: dict[str, Any], profile: str | None, base_url: str | None
) -> Any:
    """Read ``performance.getEntriesByType('resource')`` from page context.

    THE killer recon move per the user's BUILD.md — works on already-loaded
    pages where live ``network_list`` capture misses everything.
    """
    pattern = _opt_str(args.get("filter") or args.get("url_pattern"))
    if pattern:
        # Best-effort substring filter inside the page expression.
        js_filter = (
            f".filter(r => r.name && r.name.indexOf({json.dumps(pattern)}) !== -1)"
        )
    else:
        js_filter = ""
    expression = (
        "Array.from(performance.getEntriesByType('resource'))"
        + js_filter
        + ".map(r => ({name: r.name, type: r.initiatorType, "
        "duration: Math.round(r.duration), size: r.transferSize}))"
    )
    return await actions.browser_act(
        {"kind": "evaluate", "expression": expression},
        profile=profile,
        base_url=base_url,
    )


async def _do_analyze(
    actions: Any, args: dict[str, Any], profile: str | None, base_url: str | None
) -> dict[str, Any]:
    """One-shot site recon (BLUEPRINT §11).

    navigate(url) → resource_timing → neighbor adapters → anti-bot signals
    → returns a structured "use Pattern X, endpoint Y" report.
    """
    url = _required(args, "url")
    out: dict[str, Any] = {"url": url, "candidate_endpoints": []}
    # 1) Navigate. Best-effort: don't fail the analyze on a transient
    # navigation hiccup — we still get partial data from the existing
    # tab.
    try:
        nav = await actions.browser_navigate(
            url=url, target_id=None, profile=profile, base_url=base_url
        )
        if isinstance(nav, dict):
            out["targetId"] = nav.get("targetId") or nav.get("target_id")
    except BrowserServiceError as exc:
        out["navigate_error"] = str(exc)

    # 2) Resource timing — find API URLs the page already fetched.
    try:
        timing = await _do_resource_timing(actions, {}, profile, base_url)
    except BrowserServiceError as exc:
        timing = {"error": str(exc)}
    api_calls: list[dict[str, Any]] = []
    if isinstance(timing, dict):
        raw = timing.get("result") or timing.get("value") or timing
        candidates = raw if isinstance(raw, list) else []
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            if any(
                marker in name
                for marker in ("/api/", "/trpc/", "/graphql", "/v1/", "/v2/")
            ):
                api_calls.append(entry)
    out["candidate_endpoints"] = api_calls[:10]

    # 3) Neighbor adapters — same domain matches in the registry.
    try:
        from extensions.adapter_runner import (  # type: ignore[import-not-found]
            get_registered_adapters,
        )

        host = url.split("/")[2] if "://" in url else ""
        neighbors = []
        for spec in get_registered_adapters():
            if spec.domain and spec.domain in host:
                neighbors.append(f"{spec.site}/{spec.name}")
        out["neighbor_adapters"] = neighbors
    except Exception:  # noqa: BLE001
        out["neighbor_adapters"] = []

    # 4) Anti-bot signals — trivial heuristic on document.title (real
    # detection lands in v0.5).
    try:
        anti = await actions.browser_act(
            {
                "kind": "evaluate",
                "expression": (
                    "({title: document.title, body: "
                    "document.body && document.body.innerText.slice(0, 500)})"
                ),
            },
            profile=profile,
            base_url=base_url,
        )
        body_lower = ""
        if isinstance(anti, dict):
            inner = anti.get("result") or anti.get("value") or anti
            if isinstance(inner, dict):
                body_lower = (inner.get("body") or "").lower()
        indicators: list[str] = []
        for marker in ("captcha", "are you human", "cloudflare"):
            if marker in body_lower:
                indicators.append(marker)
        out["anti_bot"] = {"detected": bool(indicators), "indicators": indicators}
    except BrowserServiceError:
        out["anti_bot"] = {"detected": False, "indicators": []}

    # 5) Pattern hint — pure heuristic (real classification lands in
    # v0.5; this is enough to nudge the agent the right way).
    if api_calls:
        out["pattern"] = "A"  # §1 network — page calls API directly
    else:
        out["pattern"] = "B"  # §2 state — likely embedded in __INITIAL_STATE__
    return out


def _do_adapter_new(args: dict[str, Any]) -> dict[str, Any]:
    """Scaffold a new adapter file at ``<adapters_root>/<site>/<name>.py``.

    Default ``adapters_root`` is ``~/.opencomputer/<profile>/adapters``;
    callers can override with ``adapters_root`` (e.g. tests) or
    ``path`` (full file path).
    """
    site = _required(args, "site")
    name = _required(args, "name")
    description = _opt_str(args.get("description")) or f"{site} {name} adapter"
    domain = _opt_str(args.get("domain")) or f"{site}.example"
    strategy = _opt_str(args.get("strategy")) or "public"
    explicit_path = _opt_str(args.get("path"))
    adapters_root = _opt_str(args.get("adapters_root"))

    from pathlib import Path

    if explicit_path:
        target = Path(explicit_path)
    else:
        if adapters_root:
            root = Path(adapters_root)
        else:
            home = os.environ.get("OPENCOMPUTER_HOME") or str(
                Path.home() / ".opencomputer" / "default"
            )
            root = Path(home) / "adapters"
        target = root / site / f"{name}.py"

    if target.exists() and not bool(args.get("overwrite", False)):
        raise BrowserServiceError(
            f"adapter file already exists at {target} (pass overwrite=true)"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_adapter_stub(site, name, description, domain, strategy))
    return {"path": str(target), "site": site, "name": name}


def _render_adapter_stub(
    site: str, name: str, description: str, domain: str, strategy: str
) -> str:
    browser_flag = "False" if strategy.lower() == "public" else "True"
    return (
        '"""Adapter: '
        + f"{site}/{name}.\n"
        + '\n'
        + f"{description}\n"
        + '"""\n\n'
        + 'from __future__ import annotations\n\n'
        + 'from extensions.adapter_runner import adapter, Strategy\n\n\n'
        + '@adapter(\n'
        + f'    site="{site}",\n'
        + f'    name="{name}",\n'
        + f'    description="{description}",\n'
        + f'    domain="{domain}",\n'
        + f'    strategy=Strategy.{strategy.upper()},\n'
        + f'    browser={browser_flag},\n'
        + '    args=[\n'
        + '        # {"name": "limit", "type": "int", "default": 20, "help": "..."},\n'
        + '    ],\n'
        + '    columns=[\n'
        + '        # "rank", "title", ...\n'
        + '    ],\n'
        + ')\n'
        + 'async def run(args, ctx):\n'
        + '    """Implement the adapter logic and return list[dict] rows."""\n'
        + f'    raise NotImplementedError("fill in {site}/{name} adapter logic")\n'
    )


def _ensure_adapter_runner_namespace() -> None:
    """Eagerly bootstrap ``extensions.adapter_runner`` before importing it.

    Bug 1 fix — ``Browser(action="adapter_validate")`` and
    ``adapter_save`` import ``extensions.adapter_runner._validation``
    (and friends) and then ``_import_adapter_file()`` exec's a user
    adapter file whose first line reads
    ``from extensions.adapter_runner import adapter, Strategy``. Both
    paths require the hyphenated-on-disk plugin dir to be reachable
    via the underscore module name. The adapter-runner plugin's
    ``register()`` does this at boot, but ``_tool.py`` may run before
    that plugin loaded (load order isn't guaranteed) — so we
    self-bootstrap here. Idempotent.

    Mirrors ``extensions/adapter-runner/plugin.py::_bootstrap_package_namespace``
    but kept self-contained (no ``from extensions.adapter_runner...``
    import) so it works even when the alias isn't registered yet.
    """
    import sys
    import types
    from pathlib import Path

    # ``extensions/browser-control/_tool.py`` → walk up two parents to
    # reach ``extensions/`` then into the sibling adapter-runner plugin.
    extensions_root = Path(__file__).resolve().parent.parent
    plugin_root = extensions_root / "adapter-runner"
    if not plugin_root.is_dir():
        return  # plugin missing on disk — nothing to bootstrap

    extensions_root_str = str(extensions_root)
    if extensions_root_str not in sys.path:
        sys.path.insert(0, extensions_root_str)

    if "extensions" not in sys.modules:
        parent = types.ModuleType("extensions")
        parent.__path__ = [extensions_root_str]
        parent.__package__ = "extensions"
        sys.modules["extensions"] = parent

    pkg = sys.modules.get("extensions.adapter_runner")
    if pkg is None:
        pkg = types.ModuleType("extensions.adapter_runner")
        pkg.__path__ = [str(plugin_root)]
        pkg.__package__ = "extensions.adapter_runner"
        sys.modules["extensions.adapter_runner"] = pkg
        sys.modules["extensions"].adapter_runner = pkg  # type: ignore[attr-defined]

    if not hasattr(pkg, "adapter"):
        init_file = plugin_root / "__init__.py"
        if init_file.is_file():
            try:
                source = init_file.read_text(encoding="utf-8")
                code = compile(source, str(init_file), "exec")
                exec(code, pkg.__dict__)
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "adapter-runner namespace bootstrap failed: %s", exc
                )


def _do_adapter_save(args: dict[str, Any]) -> dict[str, Any]:
    """Replay last successful flow as a recipe; write Python module to disk.

    v0.4 ships the structural plumbing. The recorder side (capturing a
    sequence of Browser calls into a replayable trace) lands as the
    autofix flow in v0.5; for now ``adapter_save`` writes a stub
    populated with the user-provided ``run_body`` string. The agent
    that just figured out a flow can pass the JS / fetch sequence it
    used as the body.

    Bug 2 fix — after writing the adapter file, eagerly import it so
    the ``@adapter`` decorator runs and the spec lands in the
    process-wide registry, then promote the spec to a callable tool
    via ``register_adapter_at_runtime``. The new tool is callable in
    the same session; ``tool_name`` (or ``already_registered`` /
    ``register_error``) is bubbled up so the agent knows what to call.
    """
    _ensure_adapter_runner_namespace()
    site = _required(args, "site")
    name = _required(args, "name")
    body = _opt_str(args.get("run_body"))
    description = _opt_str(args.get("description")) or f"{site} {name} adapter"
    domain = _opt_str(args.get("domain")) or f"{site}.example"
    strategy = _opt_str(args.get("strategy")) or "cookie"

    from pathlib import Path

    explicit_path = _opt_str(args.get("path"))
    if explicit_path:
        target = Path(explicit_path)
    else:
        home = os.environ.get("OPENCOMPUTER_HOME") or str(
            Path.home() / ".opencomputer" / "default"
        )
        target = Path(home) / "adapters" / site / f"{name}.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    if body:
        # Indent the user-provided body so it sits inside ``async def run``.
        indented = "\n".join("    " + line for line in body.splitlines())
    else:
        indented = (
            '    # TODO: paste the successful flow here.\n'
            '    raise NotImplementedError("fill me in")'
        )
    # Strategy.PUBLIC adapters don't need browser=True; everything else does.
    browser_flag = "False" if strategy.lower() == "public" else "True"
    source = (
        '"""Adapter: '
        + f"{site}/{name} (saved from a successful Browser flow)."
        + '"""\n\n'
        + 'from __future__ import annotations\n\n'
        + 'from extensions.adapter_runner import adapter, Strategy\n\n\n'
        + '@adapter(\n'
        + f'    site="{site}",\n'
        + f'    name="{name}",\n'
        + f'    description="{description}",\n'
        + f'    domain="{domain}",\n'
        + f'    strategy=Strategy.{strategy.upper()},\n'
        + f'    browser={browser_flag},\n'
        + ')\n'
        + 'async def run(args, ctx):\n'
        + indented
        + '\n'
    )
    target.write_text(source)

    out: dict[str, Any] = {"path": str(target)}

    # ── hot-reload (Bug 2): import the file so the decorator runs +
    # promote the spec to a synthetic tool on the live PluginAPI. The
    # agent can then invoke ``<Site><Name>`` in this same session.
    try:
        from extensions.adapter_runner._decorator import (  # type: ignore[import-not-found]
            get_adapter,
        )
        from extensions.adapter_runner._discovery import (  # type: ignore[import-not-found]
            _import_adapter_file,
        )
        from extensions.adapter_runner.plugin import (  # type: ignore[import-not-found]
            register_adapter_at_runtime,
        )
    except ImportError as exc:
        out["hot_reload"] = {
            "registered": False,
            "reason": f"adapter-runner not importable: {exc}",
        }
        return out

    import_err = _import_adapter_file(target, prefix="hotreload")
    if import_err:
        out["hot_reload"] = {"registered": False, "reason": import_err}
        return out
    spec = get_adapter(site, name)
    if spec is None:
        out["hot_reload"] = {
            "registered": False,
            "reason": (
                f"adapter file imported but no spec for ({site}, {name}) "
                "landed in the registry — check the @adapter decorator"
            ),
        }
        return out
    out["hot_reload"] = register_adapter_at_runtime(spec)
    return out


def _do_adapter_validate(args: dict[str, Any]) -> dict[str, Any]:
    """Static checks on a saved adapter source file."""
    _ensure_adapter_runner_namespace()
    path = _required(args, "path")
    skip_import = bool(args.get("skip_import", False))
    from pathlib import Path

    from extensions.adapter_runner._validation import (  # type: ignore[import-not-found]
        validate_adapter_file,
    )

    result = validate_adapter_file(Path(path), skip_import=skip_import)
    return {
        "ok": result.ok,
        "errors": result.errors,
        "warnings": result.warnings,
        "tool_name": result.spec.tool_name if result.spec else None,
    }


async def _do_verify(args: dict[str, Any], profile: str | None) -> dict[str, Any]:
    """Run an adapter against its ``verify/<name>.json`` fixture."""
    site = _required(args, "site")
    name = _required(args, "name")
    from pathlib import Path

    from extensions.adapter_runner._decorator import (  # type: ignore[import-not-found]
        get_adapter,
    )
    from extensions.adapter_runner._verify import (  # type: ignore[import-not-found]
        verify_adapter,
    )

    spec = get_adapter(site, name)
    if spec is None:
        raise BrowserServiceError(
            f"no registered adapter for ({site}, {name}); "
            "import the adapter file first or run discovery"
        )
    home = os.environ.get("OPENCOMPUTER_HOME") or str(
        Path.home() / ".opencomputer" / "default"
    )
    result = await verify_adapter(
        spec, profile_home=Path(home), profile=profile
    )
    return {
        "ok": result.ok,
        "failures": result.failures,
        "warnings": result.warnings,
        "rows_returned": result.rows_returned,
    }


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


_SHIM_SUNSET_SUFFIX = (
    " Deprecated in v0.3 of browser-control; sunsets next minor release. "
    "The Browser tool covers this surface with a richer two-level "
    "discriminator — see plugin README for the migration table."
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
    # Always append the sunset suffix so every shim description carries
    # the migration nudge AND clears the 120-char audit floor enforced by
    # tests/test_tool_descriptions_audit.py.
    full_description = description + _SHIM_SUNSET_SUFFIX

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
                description=full_description,
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
