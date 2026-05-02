"""SystemNotify — cross-platform desktop notification.

Backends:
- macOS: ``osascript`` (always present; no extra deps)
- Linux: ``notify-send`` (libnotify; available on most desktops)
- Windows: PowerShell ``BurntToast`` module → fallback ``BalloonTip``

Capability tier EXPLICIT (one tier lower than click/keystroke since
notifications are non-invasive — they pop up but don't take over the
desktop).
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

from opencomputer.tools._gui_backends import detect_platform, has_command
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_VALID_URGENCY = ("low", "normal", "critical")
_MAX_TITLE = 200
_MAX_BODY = 1000


class SystemNotifyTool(BaseTool):
    # Item 3 (2026-05-02): schema enumerated; closed.
    strict_mode = True
    """Show a desktop notification. Cross-platform."""

    parallel_safe: bool = True  # notifications are independent
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="gui.system_notify",
            tier_required=ConsentTier.EXPLICIT,
            human_description="Show a non-modal desktop notification (macOS / Linux / Windows).",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="SystemNotify",
            description=(
                "Show a desktop notification with a title and optional body. "
                "Cross-platform (macOS / Linux / Windows). Backend chain: "
                "osascript (mac), notify-send (Linux), PowerShell BurntToast "
                "(Windows). EXPLICIT consent (less restrictive than "
                "click/keystroke — notifications are non-invasive)."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {
                        "type": "string",
                        "description": f"Notification title (≤{_MAX_TITLE} chars).",
                        "maxLength": _MAX_TITLE,
                    },
                    "body": {
                        "type": "string",
                        "description": f"Notification body (≤{_MAX_BODY} chars).",
                        "maxLength": _MAX_BODY,
                    },
                    "urgency": {
                        "type": "string",
                        "enum": list(_VALID_URGENCY),
                        "description": "Linux only; ignored on others.",
                    },
                },
                "required": ["title"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        title = args.get("title", "")
        body = args.get("body", "")
        urgency = args.get("urgency", "normal")

        if not title:
            return ToolResult(tool_call_id=call.id, content="title is required", is_error=True)
        if len(title) > _MAX_TITLE:
            return ToolResult(tool_call_id=call.id, content=f"title exceeds {_MAX_TITLE}-char cap", is_error=True)
        if len(body) > _MAX_BODY:
            return ToolResult(tool_call_id=call.id, content=f"body exceeds {_MAX_BODY}-char cap", is_error=True)
        if urgency not in _VALID_URGENCY:
            return ToolResult(tool_call_id=call.id, content=f"invalid urgency {urgency!r}", is_error=True)

        platform = detect_platform()
        try:
            ok = await asyncio.to_thread(_notify_dispatch, platform, title, body, urgency)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(tool_call_id=call.id, content=f"notify failed: {type(exc).__name__}: {exc}", is_error=True)

        if not ok:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "no backend available. macOS: osascript should be present; "
                    "Linux: install libnotify-bin (`apt install libnotify-bin`); "
                    "Windows: install BurntToast PowerShell module."
                ),
                is_error=True,
            )
        return ToolResult(tool_call_id=call.id, content=f"notified: {title}")


def _notify_dispatch(platform: str, title: str, body: str, urgency: str) -> bool:
    if platform == "macos":
        return _notify_osascript(title, body)
    if platform == "linux":
        return _notify_send(title, body, urgency)
    if platform == "windows":
        return _notify_powershell(title, body)
    return False


def _notify_osascript(title: str, body: str) -> bool:
    import subprocess

    if not has_command("osascript"):
        return False
    et = title.replace("\\", "\\\\").replace('"', '\\"')
    eb = body.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{eb}" with title "{et}"'
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _notify_send(title: str, body: str, urgency: str) -> bool:
    import subprocess

    if not has_command("notify-send"):
        return False
    cmd = ["notify-send", "--urgency", urgency, title]
    if body:
        cmd.append(body)
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _notify_powershell(title: str, body: str) -> bool:
    """Windows BurntToast PowerShell module. Falls back to a Wscript.Shell
    popup if BurntToast isn't installed."""
    import subprocess

    if not has_command("powershell") and not has_command("pwsh"):
        return False
    ps = has_command("pwsh") and "pwsh" or "powershell"
    et = title.replace("'", "''")
    eb = body.replace("'", "''")
    script = (
        f"if (Get-Module -ListAvailable -Name BurntToast) {{ "
        f"  Import-Module BurntToast; "
        f"  New-BurntToastNotification -Text '{et}', '{eb}' "
        f"}} else {{ "
        f"  Add-Type -AssemblyName System.Windows.Forms; "
        f"  $b = New-Object System.Windows.Forms.NotifyIcon; "
        f"  $b.Icon = [System.Drawing.SystemIcons]::Information; "
        f"  $b.BalloonTipTitle = '{et}'; "
        f"  $b.BalloonTipText = '{eb}'; "
        f"  $b.Visible = $true; $b.ShowBalloonTip(5000) "
        f"}}"
    )
    try:
        out = subprocess.run(
            [ps, "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


__all__ = ["SystemNotifyTool"]
