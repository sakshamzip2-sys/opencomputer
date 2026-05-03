"""File-chooser arming — same pattern as dialog (silent last-arm-wins).

The arm fires-and-forgets. When a file chooser opens, the most-recent
listener calls ``chooser.set_files(paths)``. Empty paths → press Escape
to dismiss (Playwright doesn't expose ``FileChooser.cancel()``).

Best-effort dispatches synthetic ``input``/``change`` events on the
underlying input afterward — some sites don't observe ``set_files`` alone.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from .._utils import sanitize_filename

_arm_counter = 0
_page_arm_id: dict[int, int] = {}


def _bump(page: Any) -> int:
    global _arm_counter
    _arm_counter += 1
    aid = _arm_counter
    _page_arm_id[id(page)] = aid
    return aid


def _current(page: Any) -> int | None:
    return _page_arm_id.get(id(page))


def _validate_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        if not isinstance(p, str) or not p.strip():
            continue
        absolute = os.path.abspath(p)
        if not os.path.isfile(absolute):
            raise FileNotFoundError(f"upload path does not exist or is not a file: {absolute!r}")
        out.append(absolute)
    if not out:
        raise ValueError("no valid upload paths")
    # Defensive: sanitize basenames for the error log only — we still
    # send the original absolute paths to Playwright.
    _ = [sanitize_filename(os.path.basename(p)) for p in out]
    return out


async def arm_file_chooser(
    page: Any,
    *,
    paths: list[str] | None = None,
    timeout_ms: int = 120_000,
    dispatch_synthetic_events: bool = True,
) -> dict[str, Any]:
    """Register a one-shot ``filechooser`` handler.

    ``paths``: list of absolute paths to upload. Empty/None → Escape.
    ``dispatch_synthetic_events``: best-effort fire ``input``/``change``
    on the underlying ``<input>`` after ``set_files`` returns.

    Returns ``{"armed": True, "arm_id": N}`` immediately.
    """
    arm_id = _bump(page)
    timeout_s = max(1.0, timeout_ms / 1000.0)
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

    on = getattr(page, "on", None)
    off = getattr(page, "remove_listener", None) or getattr(page, "off", None)

    def listener(chooser: Any) -> None:
        if not fut.done():
            fut.set_result(chooser)

    if callable(on):
        on("filechooser", listener)

    validated = _validate_paths(paths) if paths else []

    async def _handle() -> None:
        try:
            try:
                chooser = await asyncio.wait_for(fut, timeout=timeout_s)
            except asyncio.TimeoutError:
                return
            if _current(page) != arm_id:
                return
            try:
                if not validated:
                    # Press Escape to dismiss.
                    keyboard = getattr(page, "keyboard", None)
                    if keyboard is not None:
                        try:
                            await keyboard.press("Escape")
                        except Exception:
                            pass
                    return
                await chooser.set_files(validated)
                if dispatch_synthetic_events:
                    element = await chooser.element()
                    js = (
                        "(el) => { try { "
                        "el.dispatchEvent(new Event('input', { bubbles: true })); "
                        "el.dispatchEvent(new Event('change', { bubbles: true })); "
                        "} catch (_) {} }"
                    )
                    try:
                        await element.evaluate(js)
                    except Exception:
                        pass
            except Exception:
                pass
        finally:
            if callable(off):
                try:
                    off("filechooser", listener)
                except Exception:
                    pass

    try:
        asyncio.create_task(_handle())
    except RuntimeError:
        pass

    return {"armed": True, "arm_id": arm_id, "paths": validated}


# Synthetic-events JS export, in case callers want to fire them manually.
def synthetic_input_change_script() -> str:
    return (
        "(el) => { "
        "el.dispatchEvent(new Event('input', { bubbles: true })); "
        "el.dispatchEvent(new Event('change', { bubbles: true })); "
        "}"
    )


def _reset_for_tests() -> None:
    global _arm_counter
    _arm_counter = 0
    _page_arm_id.clear()


# JSON helper used by the upload route; keeps a small surface.
def serialize_arm_response(resp: dict[str, Any]) -> str:
    return json.dumps(resp, ensure_ascii=False)
