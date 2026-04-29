"""DBusCall tool — invoke a D-Bus method on Linux via ``dbus-send``.

D-Bus is the universal Linux desktop IPC layer. GNOME Shell, KDE
Plasma, NetworkManager, BlueZ, systemd, and most desktop apps publish
methods through it. ``dbus-send`` ships with every systemd Linux distro
(part of ``dbus`` package, always installed).

The tool intentionally does not use ``dbus-python`` (a heavier dep) —
``dbus-send`` plus parsing the textual reply is enough for the agent's
"call this method, get back text" use case. If a skill needs richer
introspection later, a follow-up tool ``DBusIntrospect`` can wrap
``gdbus introspect``.

Mirrors ``AppleScriptRun`` in spirit: PER_ACTION consent, Linux-only,
returns stdout as text.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from typing import ClassVar

from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_TIMEOUT_SECONDS = 10
_VALID_BUSES = {"session", "system"}


class DBusCallTool(BaseTool):
    """Invoke a D-Bus method via ``dbus-send``. Linux only."""

    # parallel_safe = False — D-Bus methods can mutate desktop state
    # (window focus, network config, etc.); racing parallel calls is bad.
    parallel_safe: bool = False
    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="gui.dbus_call",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Invoke an arbitrary D-Bus method on the Linux session or "
                "system bus. Can control GNOME/KDE apps, NetworkManager, "
                "BlueZ, systemd, etc. Same surface as ``dbus-send``."
            ),
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="DBusCall",
            description=(
                "Call a D-Bus method via dbus-send (Linux only). "
                "``bus`` is 'session' or 'system'. ``destination`` is the "
                "well-known bus name (e.g. 'org.gnome.Shell'). "
                "``object_path`` is the object (e.g. '/org/gnome/Shell'). "
                "``interface`` + ``method`` identify the method. "
                "``args`` is an optional list of dbus-send-formatted args "
                "like ['string:hello', 'int32:42'] — see ``man dbus-send``. "
                "PER_ACTION consent. Returns the textual reply from "
                "dbus-send --print-reply."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "bus": {"type": "string", "enum": ["session", "system"]},
                    "destination": {"type": "string"},
                    "object_path": {"type": "string"},
                    "interface": {"type": "string"},
                    "method": {"type": "string"},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["bus", "destination", "object_path", "interface", "method"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        a = call.arguments
        bus = a.get("bus", "")
        if bus not in _VALID_BUSES:
            return ToolResult(
                tool_call_id=call.id,
                content=f"bus must be one of {sorted(_VALID_BUSES)}; got {bus!r}",
                is_error=True,
            )
        for required in ("destination", "object_path", "interface", "method"):
            if not isinstance(a.get(required), str) or not a[required]:
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"missing or empty required arg {required!r}",
                    is_error=True,
                )

        if not sys.platform.startswith("linux"):
            return ToolResult(
                tool_call_id=call.id,
                content="DBusCall requires Linux (sys.platform.startswith('linux'))",
                is_error=True,
            )

        exe = shutil.which("dbus-send")
        if exe is None:
            return ToolResult(
                tool_call_id=call.id,
                content="dbus-send not found on PATH (install the 'dbus' package)",
                is_error=True,
            )

        argv = [
            exe,
            f"--{bus}",
            "--print-reply",
            "--type=method_call",
            f"--dest={a['destination']}",
            a["object_path"],
            f"{a['interface']}.{a['method']}",
        ]
        for raw in a.get("args", []) or []:
            if isinstance(raw, str):
                argv.append(raw)

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                argv, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool_call_id=call.id,
                content=f"dbus-send timed out after {_TIMEOUT_SECONDS}s",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(
                tool_call_id=call.id,
                content=f"dbus-send launch failed: {exc}",
                is_error=True,
            )

        body = proc.stdout
        if proc.stderr:
            body += f"\n[stderr]\n{proc.stderr}"
        return ToolResult(
            tool_call_id=call.id,
            content=body or "(no output)",
            is_error=proc.returncode != 0,
        )


__all__ = ["DBusCallTool"]
