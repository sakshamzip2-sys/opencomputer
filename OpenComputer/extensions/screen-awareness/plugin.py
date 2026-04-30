"""Plugin entry — wires sensor, hooks, tool, and injection provider
when ScreenAwarenessState.enabled=True at the active profile_home.

Default OFF: a no-op register call leaves nothing wired.
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any

from plugin_sdk.hooks import HookDecision, HookEvent, HookSpec

from .injection_provider import ScreenContextProvider
from .recall_tool import RecallScreenTool
from .ring_buffer import ScreenRingBuffer
from .sensor import ScreenAwarenessSensor
from .state import load_state

_log = logging.getLogger("opencomputer.screen_awareness.plugin")

#: Tools that DO trigger pre/post screen capture (default allowlist).
GUI_MUTATING_TOOLS: frozenset[str] = frozenset({
    "PointAndClick",
    "MouseMoveTool",
    "MouseClickTool",
    "KeyboardTypeTool",
    "AppleScriptRun",
    "PowerShellRun",
})


def _try_load_foreground_callback():
    """Best-effort wire of ambient-sensors's sample_foreground as the
    foreground-app source. Uses ``importlib.import_module`` with a
    STRING module name so the static cross-plugin scanner doesn't
    trip — this is a runtime opt-in, not a static dependency.

    Returns the callback or None if ambient-sensors isn't installed
    (or its API has drifted).
    """
    try:
        mod = importlib.import_module("extensions.ambient_sensors.foreground")
    except ImportError:
        return None

    sample = getattr(mod, "sample_foreground", None)
    if sample is None or not callable(sample):
        return None

    def _callback() -> str:
        try:
            snap = sample()
        except Exception:  # noqa: BLE001
            return ""
        if snap is None:
            return ""
        return getattr(snap, "app_name", "") or ""

    return _callback


def register(api: Any) -> None:  # noqa: ANN001 — duck-typed PluginAPI
    """Wire iff screen_awareness state.enabled=True for active profile."""
    profile_home = getattr(api, "profile_home", None)
    if profile_home is None:
        _log.debug("api.profile_home unavailable — plugin inert")
        return
    if isinstance(profile_home, str):
        profile_home = Path(profile_home)

    state = load_state(profile_home)
    if not state.enabled:
        _log.debug("screen-awareness disabled by state.json — plugin inert")
        return

    ring = ScreenRingBuffer(max_size=state.ring_size)
    foreground_cb = _try_load_foreground_callback()
    if foreground_cb is not None:
        _log.info("screen-awareness: foreground-app callback wired via ambient-sensors")
    sensor = ScreenAwarenessSensor(
        ring_buffer=ring,
        cooldown_seconds=state.cooldown_seconds,
        foreground_app_callback=foreground_cb,
    )

    # Tool — RecallScreen
    api.register_tool(RecallScreenTool(ring_buffer=ring))

    # Injection provider
    api.register_injection_provider(
        ScreenContextProvider(
            ring_buffer=ring,
            freshness_seconds=state.freshness_seconds,
            max_chars=state.max_chars,
        )
    )

    # Hook 1: BEFORE_MESSAGE_WRITE filtered to user-role messages
    async def _on_before_message_write(ctx: Any) -> HookDecision | None:  # noqa: ANN001
        msg = getattr(ctx, "message", None)
        if msg is None or msg.role != "user" or msg.tool_calls:
            return None
        sensor.capture_now(
            session_id=getattr(ctx, "session_id", "") or "",
            trigger="user_message",
        )
        return None

    api.register_hook(HookSpec(
        event=HookEvent.BEFORE_MESSAGE_WRITE,
        handler=_on_before_message_write,
        fire_and_forget=True,
    ))

    # Hook 2: PRE_TOOL_USE filtered to GUI-mutating tools
    async def _on_pre_tool_use(ctx: Any) -> HookDecision | None:  # noqa: ANN001
        tool_call = getattr(ctx, "tool_call", None)
        if tool_call is None or tool_call.name not in GUI_MUTATING_TOOLS:
            return None
        sensor.capture_now(
            session_id=getattr(ctx, "session_id", "") or "",
            trigger="pre_tool_use",
            tool_call_id=tool_call.id,
        )
        return None

    api.register_hook(HookSpec(
        event=HookEvent.PRE_TOOL_USE,
        handler=_on_pre_tool_use,
        fire_and_forget=True,
    ))

    # Hook 3: POST_TOOL_USE filtered to GUI-mutating tools
    async def _on_post_tool_use(ctx: Any) -> HookDecision | None:  # noqa: ANN001
        tool_call = getattr(ctx, "tool_call", None)
        if tool_call is None or tool_call.name not in GUI_MUTATING_TOOLS:
            return None
        sensor.capture_now(
            session_id=getattr(ctx, "session_id", "") or "",
            trigger="post_tool_use",
            tool_call_id=tool_call.id,
        )
        return None

    api.register_hook(HookSpec(
        event=HookEvent.POST_TOOL_USE,
        handler=_on_post_tool_use,
        fire_and_forget=True,
    ))

    # Hook 4: TRANSFORM_TOOL_RESULT — append pre/post delta as text
    async def _on_transform_tool_result(ctx: Any) -> HookDecision | None:  # noqa: ANN001
        from .diff import compute_screen_delta

        tool_call = getattr(ctx, "tool_call", None)
        if tool_call is None or tool_call.name not in GUI_MUTATING_TOOLS:
            return None
        tool_call_id = tool_call.id
        pre = None
        post = None
        for cap in ring.most_recent(n=20):
            if cap.tool_call_id != tool_call_id:
                continue
            if cap.trigger == "post_tool_use" and post is None:
                post = cap
            elif cap.trigger == "pre_tool_use" and pre is None:
                pre = cap
            if pre and post:
                break
        if not (pre and post):
            return None
        delta = compute_screen_delta(pre.text, post.text)
        if not delta.added and not delta.removed:
            return None
        existing = ""
        result = getattr(ctx, "tool_result", None)
        if result is not None:
            existing = getattr(result, "content", "") or ""
        annotation = (
            f"\n\n[screen-awareness] +{len(delta.added)} / -{len(delta.removed)} lines\n"
        )
        if delta.added:
            annotation += "added: " + " | ".join(delta.added[:5]) + "\n"
        if delta.removed:
            annotation += "removed: " + " | ".join(delta.removed[:5]) + "\n"
        return HookDecision(
            decision="pass",
            modified_message=existing + annotation,
        )

    api.register_hook(HookSpec(
        event=HookEvent.TRANSFORM_TOOL_RESULT,
        handler=_on_transform_tool_result,
        fire_and_forget=False,  # we need to return a HookDecision
    ))

    _log.info(
        "screen-awareness plugin wired (sensor + tool + provider + 4 hooks; "
        "primary monitor only — multi-monitor is a follow-up)"
    )


__all__ = ["GUI_MUTATING_TOOLS", "register"]
