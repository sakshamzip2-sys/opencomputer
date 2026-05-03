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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ._site_memory import SiteMemory
from ._strategy import Strategy

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
            from extensions.browser_control._utils.errors import (  # type: ignore[import-not-found]
                AuthRequiredError,
            )

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
        data = await actions.browser_navigate(
            url=url, target_id=self.target_id, profile=self.profile
        )
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
        data = await actions.browser_act(
            {"kind": "evaluate", "expression": js},
            profile=self.profile,
        )
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
        data = await self.fetch_in_page(url, method="GET")
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
            try:
                from extensions.browser_control.client import (  # type: ignore[import-not-found]
                    BrowserActions,
                )
            except ImportError as exc:  # pragma: no cover - missing extras
                raise RuntimeError(
                    "browser-control plugin not installed — "
                    "this adapter requires browser=True / Strategy.COOKIE+"
                ) from exc
            self._browser_actions = BrowserActions()
        return self._browser_actions


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
