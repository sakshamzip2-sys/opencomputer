"""security-guidance — register PreToolUse hook for security warnings.

The hook fires before Edit / Write / MultiEdit. If the new content
matches a known risky pattern (eval, innerHTML, pickle, os.system, …)
the tool call is blocked with the corresponding reminder. Each
``(file_path, rule)`` pair fires at most once per session — recorded in
a per-session state file under the active profile home.

Disable globally with ``ENABLE_SECURITY_REMINDER=0``.

Hook contract: this is a synchronous PreToolUse hook returning a
:class:`plugin_sdk.hooks.HookDecision` with ``decision="block"`` or the
default ``"pass"``. The agent loop logs the reason verbatim so the
model sees the security guidance.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from security_patterns import find_match  # type: ignore[import-not-found]

from plugin_sdk.hooks import HookContext, HookDecision, HookEvent, HookSpec

logger = logging.getLogger(__name__)

# Tool names this hook acts on. Other tools pass through unchanged.
_FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})


def _profile_home() -> Path:
    """Locate the active profile dir.

    The OC hook engine sets ``OPENCOMPUTER_PROFILE_HOME``; the legacy
    Claude-Code-compat alias is ``CLAUDE_PLUGIN_ROOT``. Fall back to
    ``~/.opencomputer/default/`` so the hook is usable even before
    plugin activation has set the env var (e.g. in tests).
    """
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME") or os.environ.get(
        "CLAUDE_PLUGIN_ROOT"
    )
    if env:
        return Path(env)
    return Path.home() / ".opencomputer" / "default"


def _state_path(session_id: str) -> Path:
    return (
        _profile_home()
        / "security_warnings"
        / f"shown_{session_id}.json"
    )


def _load_shown(session_id: str) -> set[str]:
    path = _state_path(session_id)
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_shown(session_id: str, shown: set[str]) -> None:
    path = _state_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(sorted(shown)), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("security-guidance: could not save state: %s", exc)


def _extract_content(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """Pull (file_path, content_to_check) from the tool input dict."""
    file_path = str(tool_input.get("file_path", "") or "")
    if tool_name == "Write":
        content = str(tool_input.get("content", "") or "")
    elif tool_name == "Edit":
        content = str(tool_input.get("new_string", "") or "")
    elif tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        if isinstance(edits, list):
            content = " ".join(
                str((e or {}).get("new_string", "") or "") for e in edits
            )
        else:
            content = ""
    else:
        content = ""
    return file_path, content


async def on_pre_tool_use(ctx: HookContext) -> HookDecision:
    """Check the proposed file edit against the security pattern catalogue."""
    if os.environ.get("ENABLE_SECURITY_REMINDER", "1") == "0":
        return HookDecision(decision="pass")
    if ctx.tool_call is None:
        return HookDecision(decision="pass")
    tool_name = ctx.tool_call.name
    if tool_name not in _FILE_TOOLS:
        return HookDecision(decision="pass")

    file_path, content = _extract_content(tool_name, ctx.tool_call.arguments)
    if not file_path:
        return HookDecision(decision="pass")

    match = find_match(file_path, content)
    if match is None:
        return HookDecision(decision="pass")

    warning_key = f"{file_path}::{match.rule_name}"
    shown = _load_shown(ctx.session_id)
    if warning_key in shown:
        # Already warned about this file+rule this session — let it through.
        return HookDecision(decision="pass")
    shown.add(warning_key)
    _save_shown(ctx.session_id, shown)

    return HookDecision(
        decision="block",
        reason=match.reminder,
    )


def register(api) -> None:  # noqa: D401 — duck-typed PluginAPI
    """Register the PreToolUse hook with a tool-name matcher."""
    api.register_hook(
        HookSpec(
            event=HookEvent.PRE_TOOL_USE,
            handler=on_pre_tool_use,
            matcher=r"Edit|Write|MultiEdit",
            fire_and_forget=False,
            timeout_ms=2000,
        )
    )
