"""Dialog arming — last-arm-wins, **silent** no-op for stale handlers.

Dialogs are fire-and-forget: there's no caller to surface "superseded"
to. The arm-id check causes the stale handler to silently return when
its event fires.

A second arm bumps the arm-id; the first listener stays attached but
becomes a no-op. Whichever dialog actually fires next is handled by the
listener whose arm-id still matches.
"""

from __future__ import annotations

import asyncio
from typing import Any

# Module-level monotonic counter — global to the process per OpenClaw's
# pattern. Per-page state stores the latest arm-id; stale handlers compare
# against page state and silently return.
_dialog_arm_counter = 0
_page_arm_id: dict[int, int] = {}


def _bump_arm_id(page: Any) -> int:
    global _dialog_arm_counter
    _dialog_arm_counter += 1
    arm_id = _dialog_arm_counter
    _page_arm_id[id(page)] = arm_id
    return arm_id


def _current_arm_id(page: Any) -> int | None:
    return _page_arm_id.get(id(page))


async def arm_dialog(
    page: Any,
    *,
    accept: bool,
    prompt_text: str | None = None,
    timeout_ms: int = 120_000,
) -> dict[str, Any]:
    """Register a one-shot dialog handler that resolves the next dialog.

    Returns ``{"armed": True, "arm_id": N}`` immediately; the actual
    accept/dismiss happens when the dialog fires (or never, if it
    times out).

    Last-arm-wins: a second call before the first fires bumps the arm-id;
    the first handler silently no-ops on event.
    """
    arm_id = _bump_arm_id(page)
    timeout_s = max(1.0, timeout_ms / 1000.0)
    fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()

    on = getattr(page, "on", None)
    off = getattr(page, "remove_listener", None) or getattr(page, "off", None)

    def listener(dialog: Any) -> None:
        if not fut.done():
            fut.set_result(dialog)

    if callable(on):
        on("dialog", listener)

    async def _handle() -> None:
        try:
            try:
                dialog = await asyncio.wait_for(fut, timeout=timeout_s)
            except TimeoutError:
                return
            current = _current_arm_id(page)
            if current != arm_id:
                # Stale arm — silent no-op.
                return
            try:
                if accept:
                    if prompt_text is not None:
                        await dialog.accept(prompt_text=prompt_text)
                    else:
                        await dialog.accept()
                else:
                    await dialog.dismiss()
            except Exception:
                pass
        finally:
            if callable(off):
                try:
                    off("dialog", listener)
                except Exception:
                    pass

    try:
        asyncio.create_task(_handle())
    except RuntimeError:
        # No running loop — best-effort, ignore.
        pass

    return {"armed": True, "arm_id": arm_id}


def _reset_for_tests() -> None:
    global _dialog_arm_counter
    _dialog_arm_counter = 0
    _page_arm_id.clear()
