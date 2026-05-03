"""Download lifecycle: arm → trigger → capture → store.

Last-arm-wins, with explicit ``"superseded"`` raised to the previous
caller (vs dialog/file-chooser which silently no-op). The agent UX
depends on the difference: dialogs are fire-and-forget; downloads
have a caller awaiting the bytes.

Two surfaces:

  - ``arm_download(page) -> DownloadHandle`` — stage 1; returns a handle
    the caller awaits.
  - ``capture_download(handle, *, out_dir, suggested_name=None, max_bytes=None)``
    → ``DownloadResult`` — stage 2; resolves the handle's future, saves
    file, returns a ``DownloadResult``. Raises ``DownloadSupersededError``
    if a newer arm bumped the id.
  - ``await_and_save_download(page, *, out_dir, ...)`` — combined helper
    that arms + waits + saves in one call (the typical agent path).
"""

from __future__ import annotations

import asyncio
import os
import secrets
from dataclasses import dataclass
from typing import Any

from .._utils import sanitize_filename
from .._utils.atomic_write import (
    atomic_write_bytes,  # for path-traversal-safe rename pattern  # noqa: F401
)

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


class DownloadSupersededError(RuntimeError):
    """A newer arm replaced this one before its event fired."""


@dataclass(slots=True)
class DownloadHandle:
    """Returned from ``arm_download``. Caller awaits the future, then
    passes the handle to ``capture_download`` to actually save the file."""

    arm_id: int
    page: Any
    future: asyncio.Future[Any]
    listener: Any
    timeout_ms: int


@dataclass(slots=True)
class DownloadResult:
    url: str
    suggested_filename: str
    path: str


def _build_temp_path(out_dir: str, suggested: str | None) -> str:
    base = sanitize_filename(suggested or "download.bin")
    token = secrets.token_hex(6)
    os.makedirs(out_dir, exist_ok=True)
    return os.path.join(out_dir, f"{token}-{base}")


async def arm_download(
    page: Any, *, timeout_ms: int = 120_000
) -> DownloadHandle:
    """Stage 1 — register a one-shot ``download`` listener."""
    arm_id = _bump(page)
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

    def listener(download: Any) -> None:
        if not fut.done():
            fut.set_result(download)

    on = getattr(page, "on", None)
    if callable(on):
        on("download", listener)

    return DownloadHandle(
        arm_id=arm_id, page=page, future=fut, listener=listener, timeout_ms=timeout_ms
    )


async def capture_download(
    handle: DownloadHandle,
    *,
    out_dir: str | None = None,
    out_path: str | None = None,
    suggested_name: str | None = None,
) -> DownloadResult:
    """Stage 2 — await the handle, save the file, return the result.

    Raises ``DownloadSupersededError`` if a newer arm has bumped the id
    since this handle was issued.
    """
    timeout_s = max(1.0, handle.timeout_ms / 1000.0)
    try:
        download = await asyncio.wait_for(handle.future, timeout=timeout_s)
    except TimeoutError as exc:
        _detach(handle)
        raise TimeoutError(
            f"timed out waiting for download (arm_id={handle.arm_id})"
        ) from exc

    _detach(handle)

    current = _current(handle.page)
    if current != handle.arm_id:
        raise DownloadSupersededError(
            f"Download was superseded by another waiter (this arm_id={handle.arm_id}, "
            f"current={current})"
        )

    suggested = (
        suggested_name
        or (await _safe_suggested_name(download))
        or "download.bin"
    )
    if out_path:
        target = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    else:
        target = _build_temp_path(out_dir or os.getcwd(), suggested)

    await download.save_as(target)
    url = getattr(download, "url", "") or ""
    return DownloadResult(url=url, suggested_filename=suggested, path=target)


async def await_and_save_download(
    page: Any,
    *,
    out_dir: str | None = None,
    out_path: str | None = None,
    timeout_ms: int = 120_000,
) -> DownloadResult:
    """Convenience: arm + capture in one call.

    Use this when there's a single caller who will trigger the download
    via a click or navigation between the two stages and doesn't need
    the ``DownloadHandle`` for anything else.
    """
    handle = await arm_download(page, timeout_ms=timeout_ms)
    return await capture_download(handle, out_dir=out_dir, out_path=out_path)


def _detach(handle: DownloadHandle) -> None:
    page = handle.page
    off = getattr(page, "remove_listener", None) or getattr(page, "off", None)
    if callable(off):
        try:
            off("download", handle.listener)
        except Exception:
            pass


async def _safe_suggested_name(download: Any) -> str | None:
    try:
        candidate = download.suggested_filename
    except Exception:
        try:
            candidate = await download.suggested_filename()
        except Exception:
            return None
    if not isinstance(candidate, str):
        return None
    return candidate or None


def _reset_for_tests() -> None:
    global _arm_counter
    _arm_counter = 0
    _page_arm_id.clear()
