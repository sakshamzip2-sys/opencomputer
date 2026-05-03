"""Chrome readiness probes + graceful stop.

  is_chrome_reachable      HTTP GET /json/version (lightweight liveness).
  is_chrome_cdp_ready      Stronger: open the WS, send Browser.getVersion,
                           wait for matching id reply. Used as the launch gate.
  is_running_alive         Local subprocess-state probe — does NOT touch the
                           network. Detects out-of-band Chrome death (kill -9,
                           crash, OS sigkill) so callers don't hand back a
                           cached handle pointing at a dead WS. Wave 3.3.
  stop_openclaw_chrome     SIGTERM, poll until reachable=False, then SIGKILL.

`ssrf_policy` is accepted for API stability with W1's nav_guard work; this
foundation slice trusts loopback / config-validated remote URLs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from .launch import RunningChrome

_log = logging.getLogger("opencomputer.browser_control.chrome.lifecycle")

_DEFAULT_REACHABLE_TIMEOUT_MS = 1500
_DEFAULT_CDP_HANDSHAKE_TIMEOUT_MS = 3000
_DEFAULT_STOP_TIMEOUT_MS = 2500
_STOP_POLL_INTERVAL_S = 0.05


# ─── local subprocess-state probe (no network) ────────────────────────


def is_running_alive(running: Any) -> bool:
    """Probe whether a `RunningChrome`'s subprocess is alive.

    Wave 3.3 — out-of-band Chrome death (user `kill -9`, OS sigkill,
    crash) leaves cached state pointing at a dead WebSocket. Anything
    calling over that WS hangs until timeout. Cache-read paths in both
    `_dispatcher_bootstrap` and `server_context.lifecycle` use this
    probe to decide whether to evict + relaunch.

    Local-only: reads `proc.returncode` (asyncio.subprocess.Process
    shape — None while alive, integer once exited) and falls back to
    `proc.poll()` (sync Popen analogue). Defensive on missing
    attributes — treats unknown state as dead, since handing back a
    possibly-stale entry is the worse failure mode.
    """
    if running is None:
        return False
    proc = getattr(running, "proc", None)
    if proc is None:
        return False
    rc = getattr(proc, "returncode", None)
    if rc is not None:
        return False
    poll = getattr(proc, "poll", None)
    if callable(poll):
        try:
            if poll() is not None:
                return False
        except Exception:  # noqa: BLE001 — defensive; treat probe failure as dead
            return False
    return True


# ─── HTTP probe ───────────────────────────────────────────────────────


async def is_chrome_reachable(
    cdp_url: str,
    *,
    timeout_ms: int = _DEFAULT_REACHABLE_TIMEOUT_MS,
    ssrf_policy: Any | None = None,  # noqa: ARG001 — wired in W1a
) -> bool:
    """Return True iff GET <cdp_url>/json/version returns 200 with valid JSON."""
    if not cdp_url:
        return False
    if cdp_url.startswith(("ws://", "wss://")):
        cdp_url = ("http://" if cdp_url.startswith("ws://") else "https://") + cdp_url.split("://", 1)[1]
    base = cdp_url.rstrip("/")
    timeout_s = max(0.1, timeout_ms / 1000.0)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(f"{base}/json/version")
    except (httpx.HTTPError, OSError) as exc:
        _log.debug("is_chrome_reachable(%s): %s", cdp_url, exc)
        return False
    if resp.status_code != 200:
        return False
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return False
    return isinstance(body, dict)


async def get_chrome_websocket_url(
    cdp_url: str,
    *,
    timeout_ms: int = _DEFAULT_REACHABLE_TIMEOUT_MS,
    ssrf_policy: Any | None = None,  # noqa: ARG001
) -> str | None:
    """Resolve the browser-level WS URL via /json/version's webSocketDebuggerUrl field."""
    if not cdp_url:
        return None
    base = cdp_url.rstrip("/")
    if base.startswith(("ws://", "wss://")):
        base = ("http://" if base.startswith("ws://") else "https://") + base.split("://", 1)[1]
    timeout_s = max(0.1, timeout_ms / 1000.0)
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.get(f"{base}/json/version")
    except (httpx.HTTPError, OSError):
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        return None
    ws = body.get("webSocketDebuggerUrl") if isinstance(body, dict) else None
    if isinstance(ws, str) and ws.startswith(("ws://", "wss://")):
        return ws
    return None


# ─── stronger WS-level handshake ──────────────────────────────────────


async def is_chrome_cdp_ready(
    cdp_url: str,
    *,
    timeout_ms: int = _DEFAULT_REACHABLE_TIMEOUT_MS,
    handshake_timeout_ms: int = _DEFAULT_CDP_HANDSHAKE_TIMEOUT_MS,
    ssrf_policy: Any | None = None,  # noqa: ARG001
) -> bool:
    """Open the WS, send Browser.getVersion, wait for id-1 reply.

    Returns False if the `websockets` library isn't installed (foundation
    falls back to HTTP-only readiness; W1+ wires it as a hard requirement).
    """
    ws_url = await get_chrome_websocket_url(cdp_url, timeout_ms=timeout_ms)
    if not ws_url:
        return False
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError:
        _log.debug("websockets library not installed; skipping CDP handshake check")
        return False
    try:
        async with asyncio.timeout(max(0.1, handshake_timeout_ms / 1000.0)):
            async with websockets.connect(ws_url, max_size=2**20) as conn:
                await conn.send(json.dumps({"id": 1, "method": "Browser.getVersion"}))
                while True:
                    raw = await conn.recv()
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(msg, dict) and msg.get("id") == 1:
                        return "result" in msg
    except (TimeoutError, OSError) as exc:
        _log.debug("is_chrome_cdp_ready(%s): %s", cdp_url, exc)
        return False
    except Exception as exc:  # noqa: BLE001 — websockets exceptions vary by version
        _log.debug("is_chrome_cdp_ready(%s): %s", cdp_url, exc)
        return False


# ─── graceful stop ────────────────────────────────────────────────────


async def stop_openclaw_chrome(
    running: RunningChrome,
    *,
    timeout_ms: int = _DEFAULT_STOP_TIMEOUT_MS,
) -> None:
    """SIGTERM, poll for unreachable, then SIGKILL. Idempotent."""
    proc = running.proc
    if proc is None:
        return
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    deadline = time.monotonic() + max(0.05, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            return
        if running.cdp_url and not await is_chrome_reachable(
            running.cdp_url,
            timeout_ms=200,
        ):
            try:
                await asyncio.wait_for(proc.wait(), timeout=0.5)
                return
            except TimeoutError:
                break
        await asyncio.sleep(_STOP_POLL_INTERVAL_S)
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except TimeoutError:
        _log.warning("stop_openclaw_chrome: pid=%s did not exit after SIGKILL", running.pid)
