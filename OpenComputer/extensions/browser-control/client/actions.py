"""Thin wrappers around the browser control HTTP routes.

One method per route. Each method composes path + body, then funnels the
call through :func:`fetch_browser_json` so callers can swap transports
(in-process dispatcher vs HTTP) and provide auth uniformly.

Per-call timeouts mirror OpenClaw's per-route latency budget — status /
profile reads use a tight 1.5s, Chrome cold-start gets 15s, snapshot /
act / screenshot get 20s.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from ..server.auth import BrowserAuth
from .fetch import fetch_browser_json

# Per-route timeouts (seconds). Calibrated from OpenClaw client.ts.
_T_STATUS = 1.5
_T_PROFILES = 3.0
_T_TABS = 3.0
_T_FOCUS_CLOSE = 5.0
_T_START_STOP = 15.0
_T_OPEN = 15.0
_T_RESET = 20.0
_T_SNAPSHOT = 20.0
_T_ACT = 20.0
_T_SCREENSHOT = 20.0
_T_NAVIGATE = 20.0
_T_DOWNLOAD = 20.0
_T_GENERIC = 10.0


def _profile_query(profile: str | None) -> str:
    if not profile:
        return ""
    return f"?profile={quote(profile, safe='')}"


def _path(base_url: str | None, path: str) -> str:
    """Compose a request target.

    If ``base_url`` is empty/None we return the bare path so the
    dispatcher transport handles it; otherwise we concat base + path
    and the HTTP transport takes over.
    """
    if not base_url:
        return path
    return base_url.rstrip("/") + path


def _merge_profile(body: Mapping[str, Any] | None, profile: str | None) -> dict[str, Any]:
    out: dict[str, Any] = dict(body or {})
    if profile and "profile" not in out:
        out["profile"] = profile
    return out


class BrowserActions:
    """Wrappers for the ~38 control routes.

    Construct once per (base_url, auth) pair. Methods accept ``profile=``
    + ``base_url=`` overrides per-call so a single instance can target
    multiple profiles.
    """

    def __init__(
        self,
        base_url: str | None = None,
        auth: BrowserAuth | None = None,
    ) -> None:
        self._base_url = base_url
        self._auth = auth

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def auth(self) -> BrowserAuth | None:
        return self._auth

    # ─── profile lifecycle ─────────────────────────────────────────

    async def browser_status(
        self, *, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "GET",
            _path(base_url or self._base_url, "/" + _profile_query(profile).lstrip("?")) if profile
            else _path(base_url or self._base_url, "/"),
            timeout=_T_STATUS,
            auth=self._auth,
        )

    async def browser_profiles(
        self, *, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "GET",
            _path(base_url or self._base_url, "/profiles"),
            timeout=_T_PROFILES,
            auth=self._auth,
        )

    async def browser_start(
        self, *, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/start"),
            body={"profile": profile} if profile else None,
            timeout=_T_START_STOP,
            auth=self._auth,
        )

    async def browser_stop(
        self, *, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/stop"),
            body={"profile": profile} if profile else None,
            timeout=_T_START_STOP,
            auth=self._auth,
        )

    async def browser_reset_profile(
        self, *, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/reset-profile"),
            body={"profile": profile} if profile else None,
            timeout=_T_RESET,
            auth=self._auth,
        )

    async def browser_create_profile(
        self, *, name: str, base_url: str | None = None, **extras: Any
    ) -> Any:
        body = {"name": name, **extras}
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/profiles/create"),
            body=body,
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_delete_profile(
        self, *, name: str, base_url: str | None = None
    ) -> Any:
        encoded = quote(name, safe="")
        return await fetch_browser_json(
            "DELETE",
            _path(base_url or self._base_url, f"/profiles/{encoded}"),
            timeout=_T_RESET,
            auth=self._auth,
        )

    # ─── tabs ──────────────────────────────────────────────────────

    async def browser_tabs(
        self, *, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        path = "/tabs" + _profile_query(profile)
        return await fetch_browser_json(
            "GET",
            _path(base_url or self._base_url, path),
            timeout=_T_TABS,
            auth=self._auth,
        )

    async def browser_open_tab(
        self,
        *,
        url: str,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/tabs/open"),
            body=_merge_profile({"url": url}, profile),
            timeout=_T_OPEN,
            auth=self._auth,
        )

    async def browser_focus_tab(
        self,
        *,
        target_id: str,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/tabs/focus"),
            body=_merge_profile({"targetId": target_id}, profile),
            timeout=_T_FOCUS_CLOSE,
            auth=self._auth,
        )

    async def browser_close_tab(
        self,
        *,
        target_id: str,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        encoded = quote(target_id, safe="")
        path = f"/tabs/{encoded}" + _profile_query(profile)
        return await fetch_browser_json(
            "DELETE",
            _path(base_url or self._base_url, path),
            timeout=_T_FOCUS_CLOSE,
            auth=self._auth,
        )

    # ─── snapshot / navigate / act / screenshot ───────────────────

    async def browser_snapshot(
        self,
        *,
        target_id: str | None = None,
        mode: str | None = None,
        profile: str | None = None,
        base_url: str | None = None,
        **extras: Any,
    ) -> Any:
        body: dict[str, Any] = {}
        if target_id is not None:
            body["targetId"] = target_id
        if mode is not None:
            body["mode"] = mode
        body.update(extras)
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/snapshot"),
            body=_merge_profile(body, profile),
            timeout=_T_SNAPSHOT,
            auth=self._auth,
        )

    async def browser_navigate(
        self,
        *,
        url: str,
        target_id: str | None = None,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {"url": url}
        if target_id is not None:
            body["targetId"] = target_id
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/navigate"),
            body=_merge_profile(body, profile),
            timeout=_T_NAVIGATE,
            auth=self._auth,
        )

    async def browser_screenshot(
        self,
        *,
        target_id: str | None = None,
        full_page: bool | None = None,
        ref: str | None = None,
        profile: str | None = None,
        base_url: str | None = None,
        **extras: Any,
    ) -> Any:
        body: dict[str, Any] = {}
        if target_id is not None:
            body["targetId"] = target_id
        if full_page is not None:
            body["fullPage"] = full_page
        if ref is not None:
            body["ref"] = ref
        body.update(extras)
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/screenshot"),
            body=_merge_profile(body, profile),
            timeout=_T_SCREENSHOT,
            auth=self._auth,
        )

    async def browser_pdf(
        self,
        *,
        target_id: str | None = None,
        profile: str | None = None,
        base_url: str | None = None,
        **extras: Any,
    ) -> Any:
        body: dict[str, Any] = {}
        if target_id is not None:
            body["targetId"] = target_id
        body.update(extras)
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/pdf"),
            body=_merge_profile(body, profile),
            timeout=_T_ACT,
            auth=self._auth,
        )

    async def browser_act(
        self,
        request: dict[str, Any],
        *,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/act"),
            body=_merge_profile(request, profile),
            timeout=_T_ACT,
            auth=self._auth,
        )

    async def browser_arm_dialog(
        self, *, profile: str | None = None, base_url: str | None = None, **body: Any
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/hooks/dialog"),
            body=_merge_profile(body, profile),
            timeout=_T_ACT,
            auth=self._auth,
        )

    async def browser_arm_file_chooser(
        self, *, profile: str | None = None, base_url: str | None = None, **body: Any
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/hooks/file-chooser"),
            body=_merge_profile(body, profile),
            timeout=_T_ACT,
            auth=self._auth,
        )

    async def browser_download(
        self, *, profile: str | None = None, base_url: str | None = None, **body: Any
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/download"),
            body=_merge_profile(body, profile),
            timeout=_T_DOWNLOAD,
            auth=self._auth,
        )

    async def browser_response_body(
        self, *, profile: str | None = None, base_url: str | None = None, **body: Any
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/response/body"),
            body=_merge_profile(body, profile),
            timeout=_T_ACT,
            auth=self._auth,
        )

    # ─── observe (read) ────────────────────────────────────────────

    async def browser_console(
        self,
        *,
        target_id: str | None = None,
        level: str | None = None,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        params: list[str] = []
        if target_id is not None:
            params.append(f"targetId={quote(target_id, safe='')}")
        if level is not None:
            params.append(f"level={quote(level, safe='')}")
        if profile is not None:
            params.append(f"profile={quote(profile, safe='')}")
        suffix = ("?" + "&".join(params)) if params else ""
        return await fetch_browser_json(
            "GET",
            _path(base_url or self._base_url, "/console" + suffix),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_errors(
        self,
        *,
        target_id: str | None = None,
        clear: bool | None = None,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        params: list[str] = []
        if target_id is not None:
            params.append(f"targetId={quote(target_id, safe='')}")
        if clear is not None:
            params.append(f"clear={'true' if clear else 'false'}")
        if profile is not None:
            params.append(f"profile={quote(profile, safe='')}")
        suffix = ("?" + "&".join(params)) if params else ""
        return await fetch_browser_json(
            "GET",
            _path(base_url or self._base_url, "/errors" + suffix),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_requests(
        self,
        *,
        target_id: str | None = None,
        clear: bool | None = None,
        filter: str | None = None,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        params: list[str] = []
        if target_id is not None:
            params.append(f"targetId={quote(target_id, safe='')}")
        if clear is not None:
            params.append(f"clear={'true' if clear else 'false'}")
        if filter is not None:
            params.append(f"filter={quote(filter, safe='')}")
        if profile is not None:
            params.append(f"profile={quote(profile, safe='')}")
        suffix = ("?" + "&".join(params)) if params else ""
        return await fetch_browser_json(
            "GET",
            _path(base_url or self._base_url, "/requests" + suffix),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_trace_start(
        self, *, profile: str | None = None, base_url: str | None = None, **body: Any
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/trace/start"),
            body=_merge_profile(body, profile),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_trace_stop(
        self, *, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/trace/stop"),
            body=_merge_profile({}, profile),
            timeout=_T_ACT,
            auth=self._auth,
        )

    # ─── state / emulation ────────────────────────────────────────

    async def browser_cookies(
        self, *, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        path = "/cookies" + _profile_query(profile)
        return await fetch_browser_json(
            "GET",
            _path(base_url or self._base_url, path),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_cookies_set(
        self, *, cookie: dict[str, Any], profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/cookies/set"),
            body=_merge_profile({"cookie": cookie}, profile),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_cookies_clear(
        self, *, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/cookies/clear"),
            body=_merge_profile({}, profile),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_storage_get(
        self,
        *,
        kind: str,
        key: str | None = None,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        params: list[str] = []
        if key is not None:
            params.append(f"key={quote(key, safe='')}")
        if profile is not None:
            params.append(f"profile={quote(profile, safe='')}")
        suffix = ("?" + "&".join(params)) if params else ""
        return await fetch_browser_json(
            "GET",
            _path(base_url or self._base_url, f"/storage/{kind}" + suffix),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_storage_set(
        self,
        *,
        kind: str,
        key: str,
        value: str,
        profile: str | None = None,
        base_url: str | None = None,
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, f"/storage/{kind}/set"),
            body=_merge_profile({"key": key, "value": value}, profile),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_storage_clear(
        self, *, kind: str, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, f"/storage/{kind}/clear"),
            body=_merge_profile({}, profile),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_set_offline(
        self, *, offline: bool, profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/set/offline"),
            body=_merge_profile({"offline": offline}, profile),
            timeout=_T_GENERIC,
            auth=self._auth,
        )

    async def browser_set_headers(
        self, *, headers: dict[str, str], profile: str | None = None, base_url: str | None = None
    ) -> Any:
        return await fetch_browser_json(
            "POST",
            _path(base_url or self._base_url, "/set/headers"),
            body=_merge_profile({"headers": headers}, profile),
            timeout=_T_GENERIC,
            auth=self._auth,
        )


__all__ = ["BrowserActions"]
