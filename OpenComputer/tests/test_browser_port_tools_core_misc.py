"""Tests for trace, state (emulation knobs), and responses (body reader)."""

from __future__ import annotations

import asyncio
from typing import Any

import extensions.browser_control.tools_core.trace as trace_mod
import pytest
from extensions.browser_control.tools_core.responses import read_response_body
from extensions.browser_control.tools_core.state import (
    emulate_color_scheme,
    emulate_device,
    set_extra_http_headers,
    set_geolocation,
    set_http_credentials,
    set_locale,
    set_offline,
    set_timezone,
)
from extensions.browser_control.tools_core.trace import (
    TraceAlreadyRunningError,
    TraceNotRunningError,
    is_trace_active,
    start_trace,
    stop_trace,
)

# ─── trace ───────────────────────────────────────────────────────────


class _MockTracing:
    def __init__(self) -> None:
        self.start_calls: list[dict[str, Any]] = []
        self.stop_paths: list[str] = []

    async def start(self, **kw: Any) -> None:
        self.start_calls.append(kw)

    async def stop(self, **kw: Any) -> None:
        self.stop_paths.append(kw.get("path", ""))


class _MockContext:
    def __init__(self) -> None:
        self.tracing = _MockTracing()


@pytest.mark.asyncio
async def test_trace_start_then_stop(tmp_path: Any) -> None:
    trace_mod._reset_for_tests()
    ctx = _MockContext()
    await start_trace(ctx)
    assert is_trace_active(ctx)
    out = await stop_trace(ctx, path=str(tmp_path / "t.zip"))
    assert out.endswith("t.zip")
    assert ctx.tracing.stop_paths


@pytest.mark.asyncio
async def test_double_start_raises() -> None:
    trace_mod._reset_for_tests()
    ctx = _MockContext()
    await start_trace(ctx)
    with pytest.raises(TraceAlreadyRunningError):
        await start_trace(ctx)


@pytest.mark.asyncio
async def test_stop_when_not_running_raises() -> None:
    trace_mod._reset_for_tests()
    ctx = _MockContext()
    with pytest.raises(TraceNotRunningError):
        await stop_trace(ctx, path="/tmp/foo.zip")


# ─── state / emulation ──────────────────────────────────────────────


class _MockBrowserContext:
    def __init__(self) -> None:
        self.offline = False
        self.headers: dict[str, str] | None = None
        self.creds: Any = "unset"
        self.geo: Any = "unset"
        self.permissions_cleared = 0

    async def set_offline(self, v: bool) -> None:
        self.offline = v

    async def set_extra_http_headers(self, h: dict[str, str]) -> None:
        self.headers = dict(h)

    async def set_http_credentials(self, c: Any) -> None:
        self.creds = c

    async def set_geolocation(self, g: Any) -> None:
        self.geo = g

    async def grant_permissions(self, perms: list[str], **kw: Any) -> None:
        pass

    async def clear_permissions(self) -> None:
        self.permissions_cleared += 1


class _MockCdp:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def send(self, method: str, params: dict[str, Any]) -> None:
        self.sent.append((method, params))


class _MockPageForState:
    def __init__(self) -> None:
        self.context = _MockBrowserContext()
        self.cdp = _MockCdp()
        self.media_calls: list[str] = []
        self.viewport_calls: list[dict[str, int]] = []

    async def emulate_media(self, **kw: Any) -> None:
        self.media_calls.append(kw.get("color_scheme", ""))

    async def set_viewport_size(self, vp: dict[str, int]) -> None:
        self.viewport_calls.append(vp)

    # context.new_cdp_session(page)
    def __getattr__(self, item: str) -> Any:
        # Provide context.new_cdp_session via the BrowserContext
        raise AttributeError(item)


def _attach_cdp(page: _MockPageForState) -> None:
    async def new_cdp_session(_p: Any) -> _MockCdp:
        return page.cdp

    page.context.new_cdp_session = new_cdp_session  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_set_offline_round_trip() -> None:
    page = _MockPageForState()
    await set_offline(page.context, True)
    assert page.context.offline is True


@pytest.mark.asyncio
async def test_set_extra_http_headers_validates_dict() -> None:
    page = _MockPageForState()
    with pytest.raises(TypeError):
        await set_extra_http_headers(page.context, ["bad"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_set_extra_http_headers_normalizes() -> None:
    page = _MockPageForState()
    await set_extra_http_headers(page.context, {"X-Test": 7})  # type: ignore[arg-type]
    assert page.context.headers == {"X-Test": "7"}


@pytest.mark.asyncio
async def test_set_http_credentials_clear() -> None:
    page = _MockPageForState()
    await set_http_credentials(page.context, clear=True)
    assert page.context.creds is None


@pytest.mark.asyncio
async def test_set_http_credentials_set() -> None:
    page = _MockPageForState()
    await set_http_credentials(page.context, username="u", password="p")
    assert page.context.creds == {"username": "u", "password": "p"}


@pytest.mark.asyncio
async def test_set_geolocation_clear() -> None:
    page = _MockPageForState()
    await set_geolocation(page.context, clear=True)
    assert page.context.geo is None


@pytest.mark.asyncio
async def test_set_geolocation_with_coords() -> None:
    page = _MockPageForState()
    await set_geolocation(page.context, latitude=10.0, longitude=20.0, accuracy=5.0)
    assert page.context.geo == {"latitude": 10.0, "longitude": 20.0, "accuracy": 5.0}


@pytest.mark.asyncio
async def test_emulate_color_scheme_dark() -> None:
    page = _MockPageForState()
    await emulate_color_scheme(page, "dark")
    assert page.media_calls == ["dark"]


@pytest.mark.asyncio
async def test_emulate_color_scheme_rejects_unknown() -> None:
    page = _MockPageForState()
    with pytest.raises(ValueError):
        await emulate_color_scheme(page, "ultra-violet")


@pytest.mark.asyncio
async def test_set_locale_calls_cdp() -> None:
    page = _MockPageForState()
    _attach_cdp(page)
    await set_locale(page, "fr-FR")
    assert page.cdp.sent == [("Emulation.setLocaleOverride", {"locale": "fr-FR"})]


@pytest.mark.asyncio
async def test_set_locale_rejects_blank() -> None:
    page = _MockPageForState()
    _attach_cdp(page)
    with pytest.raises(ValueError):
        await set_locale(page, "")


@pytest.mark.asyncio
async def test_set_timezone_calls_cdp() -> None:
    page = _MockPageForState()
    _attach_cdp(page)
    await set_timezone(page, "Europe/Paris")
    assert page.cdp.sent == [("Emulation.setTimezoneOverride", {"timezoneId": "Europe/Paris"})]


@pytest.mark.asyncio
async def test_emulate_device_sets_viewport_and_ua() -> None:
    page = _MockPageForState()
    _attach_cdp(page)
    descriptor = {
        "viewport": {"width": 375, "height": 667},
        "user_agent": "Mozilla/5.0 (iPhone)",
        "device_scale_factor": 2,
        "is_mobile": True,
        "has_touch": True,
    }
    await emulate_device(page, descriptor)
    assert page.viewport_calls == [{"width": 375, "height": 667}]
    methods = [m for m, _ in page.cdp.sent]
    assert "Emulation.setUserAgentOverride" in methods
    assert "Emulation.setDeviceMetricsOverride" in methods
    assert "Emulation.setTouchEmulationEnabled" in methods


# ─── responses ──────────────────────────────────────────────────────


class _MockResponse:
    def __init__(self, url: str, body: bytes = b"hello", status: int = 200) -> None:
        self.url = url
        self.status = status
        self._body = body

    async def body(self) -> bytes:
        return self._body

    async def all_headers(self) -> dict[str, str]:
        return {"content-type": "text/plain"}


class _PageWithResponse:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Any]] = {}

    def on(self, event: str, h: Any) -> None:
        self._listeners.setdefault(event, []).append(h)

    def remove_listener(self, event: str, h: Any) -> None:
        try:
            self._listeners[event].remove(h)
        except (KeyError, ValueError):
            pass

    off = remove_listener

    def fire(self, event: str, payload: Any) -> None:
        for h in list(self._listeners.get(event, [])):
            h(payload)


@pytest.mark.asyncio
async def test_read_response_body_substring_match() -> None:
    page = _PageWithResponse()

    async def fire_later() -> None:
        await asyncio.sleep(0.01)
        page.fire("response", _MockResponse("https://x.com/api/data", b"payload"))

    asyncio.create_task(fire_later())
    out = await read_response_body(page, url_pattern="api/data", timeout_ms=2000)
    assert out["body"] == "payload"
    assert out["status"] == 200
    assert out["truncated"] is False


@pytest.mark.asyncio
async def test_read_response_body_truncation() -> None:
    page = _PageWithResponse()

    async def fire_later() -> None:
        await asyncio.sleep(0.01)
        page.fire("response", _MockResponse("https://x.com/big", b"x" * 5000))

    asyncio.create_task(fire_later())
    out = await read_response_body(
        page, url_pattern="big", timeout_ms=2000, max_bytes=100
    )
    assert out["truncated"] is True
    assert len(out["body"]) == 100


@pytest.mark.asyncio
async def test_read_response_body_timeout() -> None:
    page = _PageWithResponse()
    with pytest.raises(TimeoutError):
        await read_response_body(page, url_pattern="never", timeout_ms=200)
