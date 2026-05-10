"""``AdapterContext`` — the ``ctx`` argument passed to ``async def run``.

Provides the four primitives an adapter needs:

  - ``ctx.fetch(url, ...)`` — plain HTTP via ``httpx``. PUBLIC adapters
    use this exclusively. No browser ever spawned.
  - ``ctx.fetch_in_page(url, ...)`` — HTTP from inside the browser tab
    (so cookies ride along automatically). Auto-pre-warms the origin
    via ``page.goto`` first — solves the LearnX BUILD.md "cannot find
    default execution context" dead-end transparently.
  - ``ctx.evaluate(js)`` — ``Runtime.evaluate`` in the active page.
  - ``ctx.navigate(url)`` — ``Page.navigate`` equivalent.
  - ``ctx.network_list(filter=...)`` — captured requests since the
    capture started (see ``Browser(action="network_start")``).
  - ``ctx.trpc_query(procedure, input=...)`` — convenience helper for
    tRPC sites; ports the user's LearnX ``trpcQuery`` helper verbatim.
  - ``ctx.site_memory`` — read/write to ``~/.opencomputer/<profile>/
    sites/<site>/`` (endpoints.json / field-map.json / notes.md).
  - ``ctx.click(ref)`` — for Strategy.UI adapters.

The ctx is constructed by ``_runner.py`` per-call, never reused
across invocations. PUBLIC adapters never trigger the browser
bootstrap; everything else falls through to ``BrowserActions``
under the hood (which lazy-bootstraps the dispatcher on first use).
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

_DEBUG_TIMING = os.environ.get("OC_ADAPTER_TIMING") == "1"


def _t(label: str, t0: float) -> None:
    if _DEBUG_TIMING:
        dt = time.monotonic() - t0
        sys.stderr.write(f"[ADAPTER-TIMING] {label}: {dt*1000:.0f}ms\n")
        sys.stderr.flush()

# debug-timing helper above must define _DEBUG_TIMING first; imports
# below must run after that block, hence the noqa-suppressed E402.
from ._site_memory import SiteMemory  # noqa: E402
from ._strategy import Strategy  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ._decorator import AdapterSpec


@dataclass(slots=True)
class AdapterContext:
    """Per-call context. Built by ``_runner.py``; owned by one adapter run."""

    site: str
    spec: AdapterSpec
    profile_home: Path
    site_memory: SiteMemory
    profile: str | None = None
    target_id: str | None = None
    _origin_warmed: bool = field(default=False, repr=False)
    # Optional injection points so tests can swap real network /
    # browser drivers for fakes. ``_browser_actions`` mirrors
    # ``extensions.browser_control.client.BrowserActions`` shape.
    _browser_actions: Any = field(default=None, repr=False)
    _http_client: Any = field(default=None, repr=False)
    _trace: Any = field(default=None, repr=False)

    # ─── factory ────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        *,
        spec: AdapterSpec,
        profile_home: Path,
        profile: str | None = None,
        browser_actions: Any | None = None,
        http_client: Any | None = None,
        trace: Any | None = None,
    ) -> AdapterContext:
        return cls(
            site=spec.site,
            spec=spec,
            profile_home=Path(profile_home),
            site_memory=SiteMemory.for_site(Path(profile_home), spec.site),
            profile=profile,
            _browser_actions=browser_actions,
            _http_client=http_client,
            _trace=trace,
        )

    # ─── pure-HTTP path (Strategy.PUBLIC) ───────────────────────

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: Any = None,
        timeout: float = 20.0,
    ) -> Any:
        """Plain HTTP request via httpx. Returns parsed JSON (or text).

        For PUBLIC strategy adapters. Body is auto-JSON-encoded if it's
        a dict or list. Response is auto-parsed as JSON when the
        ``Content-Type`` says so; otherwise returned as ``str``.
        """
        client = self._http_client
        own_client = False
        if client is None:
            try:
                import httpx
            except ImportError as exc:
                raise RuntimeError(
                    "httpx not installed — install with `pip install httpx`"
                ) from exc
            client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
            own_client = True
        try:
            req_kwargs: dict[str, Any] = {"method": method.upper(), "url": url}
            if headers:
                req_kwargs["headers"] = headers
            if body is not None:
                if isinstance(body, (dict, list)):
                    req_kwargs["json"] = body
                else:
                    req_kwargs["content"] = body
            resp = await client.request(**req_kwargs)
        finally:
            if own_client:
                await client.aclose()

        if self._trace is not None:
            try:
                self._trace.record_fetch(url=url, method=method, status=resp.status_code)
            except Exception:  # noqa: BLE001
                pass

        # Auth-error mapping — let runner translate via typed error
        if resp.status_code in (401, 403):
            AuthRequiredError = _typed_browser_errors().AuthRequiredError

            raise AuthRequiredError(
                f"{url} returned HTTP {resp.status_code} — log in and retry",
                status=resp.status_code,
            )
        resp.raise_for_status()

        ctype = resp.headers.get("content-type", "")
        if "json" in ctype.lower():
            return resp.json()
        return resp.text

    # ─── browser-backed path (COOKIE / UI / INTERCEPT) ─────────

    async def navigate(self, url: str) -> dict[str, Any]:
        """Drive the browser: ``Page.navigate``."""
        actions = self._actions()
        t0 = time.monotonic()
        if _DEBUG_TIMING:
            sys.stderr.write(
                f"[ADAPTER-TIMING] navigate START url={url!r} target_id={self.target_id!r}\n"
            )
            sys.stderr.flush()
        data = await actions.browser_navigate(
            url=url, target_id=self.target_id, profile=self.profile
        )
        _t(f"navigate END url={url!r}", t0)
        # Capture target_id from the response so subsequent calls
        # stay on the same tab (mirrors the BUILD.md guidance).
        if isinstance(data, dict):
            tid = data.get("targetId") or data.get("target_id")
            if isinstance(tid, str) and tid:
                self.target_id = tid
        # Pre-warm flag — once we've navigated, fetch_in_page can
        # skip the auto-warm.
        self._origin_warmed = True
        return data if isinstance(data, dict) else {}

    async def evaluate(self, js: str) -> Any:
        """``Runtime.evaluate`` in the active page; returns the result."""
        actions = self._actions()
        t0 = time.monotonic()
        if _DEBUG_TIMING:
            sys.stderr.write(f"[ADAPTER-TIMING] evaluate START (js_len={len(js)})\n")
            sys.stderr.flush()
        data = await actions.browser_act(
            {"kind": "evaluate", "expression": js},
            profile=self.profile,
        )
        _t("evaluate END", t0)
        # Server returns the raw evaluated value under ``result`` /
        # ``value`` depending on the path. Normalize.
        if isinstance(data, dict):
            for key in ("result", "value", "data"):
                if key in data:
                    return data[key]
        return data

    async def click(self, ref: str) -> dict[str, Any]:
        """Click an indexed ref from a snapshot."""
        actions = self._actions()
        data = await actions.browser_act(
            {"kind": "click", "ref": ref},
            profile=self.profile,
        )
        return data if isinstance(data, dict) else {}

    async def fetch_in_page(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: Any = None,
        warm_origin: str | None = None,
    ) -> Any:
        """HTTP from inside the page origin via ``Runtime.evaluate(fetch())``.

        Cookies ride along automatically (``credentials: 'include'``).
        Auto-pre-warms the target origin once per ctx — solves the
        "cannot find default execution context" dead-end.

        ``warm_origin`` overrides the auto-derived origin (rare; useful
        when the API is on a different subdomain than the UI).
        """
        if not self._origin_warmed:
            origin = warm_origin or _origin_of(url)
            if origin:
                if _DEBUG_TIMING:
                    sys.stderr.write(
                        f"[ADAPTER-TIMING] fetch_in_page WARM origin={origin!r}\n"
                    )
                    sys.stderr.flush()
                try:
                    await self.navigate(origin)
                except Exception:  # noqa: BLE001 — best-effort warm
                    pass

        # Build the JS expression that actually performs the fetch.
        opts: dict[str, Any] = {"method": method.upper(), "credentials": "include"}
        if headers:
            opts["headers"] = headers
        if body is not None:
            if isinstance(body, (dict, list)):
                opts["body"] = json.dumps(body)
                opts.setdefault("headers", {}).setdefault(
                    "Content-Type", "application/json"
                )
            else:
                opts["body"] = body
        js = (
            "(async () => { "
            f"const res = await fetch({json.dumps(url)}, {json.dumps(opts)}); "
            "if (!res.ok) return { __httpError: res.status }; "
            "const ct = res.headers.get('content-type') || ''; "
            "if (ct.includes('json')) return await res.json(); "
            "return await res.text(); "
            "})()"
        )
        result = await self.evaluate(js)

        if self._trace is not None:
            try:
                self._trace.record_fetch_in_page(
                    url=url,
                    method=method,
                    error=(result.get("__httpError") if isinstance(result, dict) else None),
                )
            except Exception:  # noqa: BLE001
                pass

        if isinstance(result, dict) and "__httpError" in result:
            status = result["__httpError"]
            if status in (401, 403):
                from extensions.browser_control._utils.errors import (  # type: ignore[import-not-found]
                    AuthRequiredError,
                )

                raise AuthRequiredError(
                    f"{url} returned HTTP {status} from page context — "
                    f"log in to {self.spec.domain} and retry",
                    status=status,
                )
            raise RuntimeError(f"fetch_in_page {url} HTTP {status}")
        return result

    async def network_list(
        self, url_pattern: str | None = None
    ) -> list[dict[str, Any]]:
        """Captured requests since ``network_start``. Optional URL filter."""
        actions = self._actions()
        data = await actions.browser_requests(
            target_id=self.target_id,
            filter=url_pattern,
            profile=self.profile,
        )
        if isinstance(data, dict):
            reqs = data.get("requests")
            if isinstance(reqs, list):
                return reqs
        if isinstance(data, list):
            return data
        return []

    # ─── tRPC convenience (matches LearnX trpcQuery) ───────────

    async def trpc_query(
        self,
        procedure: str,
        *,
        input: dict[str, Any] | None = None,
        domain: str | None = None,
    ) -> Any:
        """Issue a tRPC GET request and unwrap the standard envelope.

        Mirrors the ``trpcQuery`` helper from the user's LearnX adapter
        (BUILD.md Phase 5). Standard URL shape:

            GET https://<domain>/api/trpc/<procedure>?batch=1&input=...

        Input encoding:
            - ``None``:  ``{ "0": { "json": null, "meta": ... } }``
            - dict:      ``{ "0": { "json": <input> } }``
        """
        host = (domain or self.spec.domain).strip()
        if input is None:
            input_obj = {
                "0": {
                    "json": None,
                    "meta": {"values": ["undefined"], "v": 1},
                }
            }
        else:
            input_obj = {"0": {"json": input}}
        url = (
            f"https://{host}/api/trpc/{procedure}"
            f"?batch=1&input={quote(json.dumps(input_obj), safe='')}"
        )
        # Navigate to /learn before the first tRPC call in this ctx.
        # fetch_in_page's warm path navigates to the bare origin, which can
        # land on /login or trigger a redirect, leaving the page mid-load
        # when evaluate fires → 20s dispatcher timeout on the second adapter
        # invocation. Navigating to /learn (matching opencli utils.js:6)
        # puts the page in a stable auth state. _origin_warmed gates this to
        # once per ctx so subsequent trpc_query calls in the same adapter run
        # skip the navigate and don't steal focus again.
        trpc_t0 = time.monotonic()
        if _DEBUG_TIMING:
            sys.stderr.write(
                f"[ADAPTER-TIMING] trpc_query START procedure={procedure!r} "
                f"warmed={self._origin_warmed} target_id={self.target_id!r}\n"
            )
            sys.stderr.flush()
        if not self._origin_warmed:
            warm_t0 = time.monotonic()
            try:
                await self.navigate(f"https://{host}/learn")
            except Exception:  # noqa: BLE001 — best-effort; evaluate will fail cleanly if broken
                pass
            _t("trpc_query /learn warm-navigate", warm_t0)
        fetch_t0 = time.monotonic()
        data = await self.fetch_in_page(url, method="GET", warm_origin=None)
        _t("trpc_query fetch_in_page", fetch_t0)
        _t(f"trpc_query TOTAL procedure={procedure!r}", trpc_t0)
        if not isinstance(data, list) or not data:
            return None
        first = data[0]
        if isinstance(first, dict):
            err = first.get("error")
            if isinstance(err, dict):
                msg = err.get("json", {}).get("message") if isinstance(
                    err.get("json"), dict
                ) else None
                raise RuntimeError(f"tRPC {procedure} error: {msg or 'unknown'}")
            result = first.get("result", {})
            if isinstance(result, dict):
                inner = result.get("data", {})
                if isinstance(inner, dict) and "json" in inner:
                    return inner["json"]
                return inner
        return first

    # ─── helpers ────────────────────────────────────────────────

    def _actions(self) -> Any:
        if self._browser_actions is None:
            # Per-profile / env-var backend selection. When
            # OPENCOMPUTER_USE_BROWSER_HARNESS=1 is set, route browser
            # ops through the new browser-harness plugin (Hermes-derived,
            # uses agent-browser CLI). Otherwise fall back to the legacy
            # Playwright-based browser-control plugin.
            self._browser_actions = _resolve_browser_actions()
        return self._browser_actions


_TYPED_ERRORS_CACHE: Any = None


def _typed_browser_errors() -> Any:
    """Resolve the typed-error module that adapter-runner raises / catches.

    Browser-harness ships a byte-identical copy of browser-control's
    ``_utils/errors.py`` so adapter-runner doesn't need browser-control
    to be loaded at all. Resolution order:

      1. ``extensions/browser-harness/errors.py`` (lifted, primary).
      2. ``extensions/browser-control/_utils/errors.py`` (legacy fallback;
         only reachable when browser-control's plugin is loaded and has
         bootstrapped its package namespace into sys.modules).
      3. A bare-bones synthetic module with all six classes aliased to
         ``Exception`` — keeps adapter-runner working in pure-PUBLIC
         test contexts where no browser plugin is on disk.

    Cached at module scope so repeat calls are free.
    """
    global _TYPED_ERRORS_CACHE
    if _TYPED_ERRORS_CACHE is not None:
        return _TYPED_ERRORS_CACHE

    # Path 1 — browser-harness (primary).
    try:
        import sys as _sys
        from pathlib import (
            Path as _P,  # noqa: N814 — local-scope alias for compactness in this multi-path try/except
        )
        bh_dir = str(_P(__file__).resolve().parent.parent / "browser-harness")
        if bh_dir not in _sys.path:
            _sys.path.insert(0, bh_dir)
        # Use a unique synthetic name to avoid colliding with any
        # ``errors`` module another plugin may have loaded.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_oc_adapter_runner_errors",
            _P(bh_dir) / "errors.py",
        )
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _TYPED_ERRORS_CACHE = mod
            return mod
    except Exception:  # noqa: BLE001
        pass

    # Path 2 — browser-control legacy.
    try:
        from extensions.browser_control._utils import (
            errors as _legacy,  # type: ignore[import-not-found]
        )
        _TYPED_ERRORS_CACHE = _legacy
        return _legacy
    except ImportError:
        pass

    # Path 3 — synthetic stub. Keeps `raise X(...)` and `except X:` clean.
    import types as _types
    stub = _types.SimpleNamespace(
        BrowserServiceError=Exception,
        AuthRequiredError=Exception,
        AdapterEmptyResultError=Exception,
        AdapterTimeoutError=Exception,
        AdapterConfigError=Exception,
        AdapterNotFoundError=Exception,
    )
    _TYPED_ERRORS_CACHE = stub
    return stub


def _resolve_browser_actions() -> Any:
    """Pick which BrowserActions backend the adapter should use.

    Selection (first match wins):
      1. ``OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY=1`` env var → legacy
         ``extensions.browser_control.client.BrowserActions`` (Playwright/CDP
         path; only here as an escape hatch during migration to browser-harness).
      2. Default → ``BrowserHarnessActions`` (Hermes-derived, agent-browser
         CLI). Immune to the renderer-suspension bug that wedged
         ``browser-control`` in long-lived chat sessions.

    The previous arrangement gated browser-harness behind
    ``OPENCOMPUTER_USE_BROWSER_HARNESS=1``; that flag was flipped to be the
    default at 2026-05-08 once the wiring was end-to-end verified.
    Setting ``OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY=1`` reinstates the
    old behaviour for diagnostic purposes.
    """
    if os.environ.get("OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY") == "1":
        try:
            from extensions.browser_control.client import (  # type: ignore[import-not-found]
                BrowserActions,
            )
            return BrowserActions()
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "OPENCOMPUTER_USE_BROWSER_CONTROL_LEGACY=1 but browser-control "
                "plugin is not loadable. Either remove the env var or re-enable "
                "the plugin (it was set enabled_by_default=false during the "
                "browser-harness migration)."
            ) from exc

    try:
        return _build_browser_harness_actions()
    except Exception as exc:  # noqa: BLE001
        # Don't crash the adapter run on an integration glitch — fall
        # back to the legacy path with a loud warning. The real
        # diagnostic surface is `opencomputer doctor`.
        import logging
        logging.getLogger("opencomputer.adapter_runner").warning(
            "browser-harness init failed (%s); falling back to browser-control. "
            "Run `opencomputer doctor` and check that agent-browser is installed "
            "(`npm install agent-browser` in the OC repo root).",
            exc,
        )
        try:
            from extensions.browser_control.client import (  # type: ignore[import-not-found]
                BrowserActions,
            )
            return BrowserActions()
        except ImportError as exc2:  # pragma: no cover
            raise RuntimeError(
                "browser-harness initialization failed AND legacy browser-control "
                "plugin is not loadable. No browser backend available."
            ) from exc2


_BROWSER_HARNESS_ACTIONS_CACHE: Any = None


def _build_browser_harness_actions() -> Any:
    """Construct a ``BrowserHarnessActions`` from the browser-harness plugin.

    The plugin lives at ``extensions/browser-harness/`` (hyphenated dir,
    so not directly importable as a Python module). Loading via
    ``importlib.util`` with explicit synthetic module names avoids two
    pitfalls:

      * Collision with OC's plugin-loader cache, which clears short names
        like ``plugin`` between plugin loads but doesn't expect a third
        party (us) to add another ``plugin`` to ``sys.modules``.
      * Collision with another plugin that happens to expose the same
        sibling-file names (e.g. ``actions``).

    Cached at module scope so subsequent adapter calls don't re-load.
    """
    global _BROWSER_HARNESS_ACTIONS_CACHE
    if _BROWSER_HARNESS_ACTIONS_CACHE is not None:
        return _BROWSER_HARNESS_ACTIONS_CACHE

    import importlib.util as _ilu
    import sys as _sys
    from pathlib import Path as _Path

    plugin_dir = _Path(__file__).resolve().parent.parent / "browser-harness"
    if not plugin_dir.is_dir():
        raise RuntimeError(
            f"browser-harness plugin directory not found at {plugin_dir}"
        )

    # The plugin dir must be on sys.path for the lifted dispatcher's
    # sibling-imports (compat, redact, browser_camofox, browser_providers)
    # to resolve. Idempotent — only adds once.
    if str(plugin_dir) not in _sys.path:
        _sys.path.insert(0, str(plugin_dir))

    def _load(name: str, path: _Path):
        synth = f"_oc_browser_harness_{name}"
        if synth in _sys.modules:
            return _sys.modules[synth]
        spec = _ilu.spec_from_file_location(synth, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"could not build spec for {path}")
        mod = _ilu.module_from_spec(spec)
        _sys.modules[synth] = mod
        spec.loader.exec_module(mod)
        return mod

    # plugin.py side-effect: prepends node_modules/.bin to PATH so the
    # dispatcher can find agent-browser. Skip the loader's register()
    # call — we just want the import-time side effect.
    _load("plugin", plugin_dir / "plugin.py")
    actions_mod = _load("actions", plugin_dir / "actions.py")

    _BROWSER_HARNESS_ACTIONS_CACHE = actions_mod.BrowserHarnessActions()
    return _BROWSER_HARNESS_ACTIONS_CACHE


def _origin_of(url: str) -> str | None:
    """Extract scheme://host[:port] from a URL. Returns None for malformed."""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return None
        return f"{parts.scheme}://{parts.netloc}"
    except (ValueError, AttributeError):
        return None


__all__ = ["AdapterContext", "Strategy"]
