"""Process-wide active-skill tool allowlist (M4.4 hard enforcement).

v1.1 plan-2 M4.4 follow-up (2026-05-09). When a SKILL.md with
``context: inline`` declares ``tools: [Read, Grep]``, those tools
become a HARD allowlist for the duration of that skill's execution
in the parent agent loop — not just an advisory directive.

Mechanism:

* :class:`SkillTool` calls :func:`set_active_filter` after returning
  the body. The skill's `tools:` list is the allowlist; the filter
  also stores the skill name for audit-log clarity.
* The core ``PreToolUse`` hook ``skill_tools_enforcer`` checks the
  filter on every tool call. If the call's tool name isn't in the
  allowlist, the hook returns ``HookDecision(decision="block")``
  with a clear reason — same shape as ConsentGate's deny.
* The agent loop clears the filter on ``END_TURN`` (the model
  decided it's done with this skill) via :func:`clear_active_filter`.

Single in-process slot is sufficient: an agent loop only ever has
one active skill at a time. Lock is defensive for future
multi-loop processes.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActiveSkillFilter:
    """Currently-active skill's tool allowlist."""

    skill_name: str
    allowed_tools: frozenset[str]


_FILTER_LOCK = threading.Lock()
_ACTIVE_FILTER: ActiveSkillFilter | None = None


def get_active_filter() -> ActiveSkillFilter | None:
    """Return the active filter without clearing it."""
    with _FILTER_LOCK:
        return _ACTIVE_FILTER


def set_active_filter(skill_name: str, tools: list[str] | tuple[str, ...]) -> None:
    """Activate a tool allowlist for the next turn(s) of this loop.

    Replaces any prior filter — only one active skill at a time.
    Empty ``tools`` means "no tools allowed" (rare; the skill
    declared an explicit empty allowlist).
    """
    global _ACTIVE_FILTER  # noqa: PLW0603
    with _FILTER_LOCK:
        _ACTIVE_FILTER = ActiveSkillFilter(
            skill_name=skill_name,
            allowed_tools=frozenset(str(t) for t in tools),
        )


def clear_active_filter() -> ActiveSkillFilter | None:
    """Drop the active filter and return what was there.

    Called by the agent loop on ``END_TURN`` (the model finished
    with this skill) so subsequent turns aren't constrained.
    """
    global _ACTIVE_FILTER  # noqa: PLW0603
    with _FILTER_LOCK:
        out = _ACTIVE_FILTER
        _ACTIVE_FILTER = None
        return out


def is_tool_allowed(tool_name: str) -> tuple[bool, str | None]:
    """Check whether ``tool_name`` is allowed by the active filter.

    Returns ``(allowed, reason)``. ``reason`` is ``None`` when no
    filter is active OR when the tool is explicitly allowed; carries
    a short human-readable string when the call is blocked.
    """
    flt = get_active_filter()
    if flt is None:
        return (True, None)
    if tool_name in flt.allowed_tools:
        return (True, None)
    allowed_str = ", ".join(sorted(flt.allowed_tools)) or "(none)"
    return (
        False,
        f"skill {flt.skill_name!r} restricts tools to: {allowed_str} "
        f"(tool {tool_name!r} is not in the allowlist)",
    )


__all__ = [
    "ActiveSkillFilter",
    "clear_active_filter",
    "get_active_filter",
    "is_tool_allowed",
    "set_active_filter",
]
