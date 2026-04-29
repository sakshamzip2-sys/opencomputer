"""``/<skill-name>`` slash-dispatch fallback — load any skill by name as a slash command.

Tier 2.A.26 from docs/refs/hermes-agent/2026-04-28-major-gaps.md.

When the user types ``/foo`` and ``foo`` isn't in the slash-command
registry, the dispatcher's ``fallback`` callable (wired by the agent
loop) checks if ``foo`` is the id of a registered skill. If yes, the
SKILL.md body is returned as the slash response — equivalent to the
user typing the skill name directly but reachable via the same slash
dispatch path as built-in commands. If no, the fallback returns None
and the dispatcher reports unknown-command.

The body is what the loop would normally inject as a system-prompt
overlay; surfacing it via slash means the user can introspect a
skill's full prose without leaving the agent loop. Useful for "what
does this skill actually contain?" questions.
"""

from __future__ import annotations

from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.slash_command import SlashCommandResult

# Cap on rendered body — protects against pathologically long skills.
# 16K chars ≈ 4K tokens; a single slash response shouldn't blow the
# context window.
_MAX_BODY_CHARS = 16_000


def make_skill_fallback(memory_manager) -> object:  # noqa: ANN001 — duck-typed
    """Build a fallback callable closing over a MemoryManager.

    The agent loop calls this once at construction time and passes the
    returned callable to ``dispatch()``. Keeps slash_dispatcher.py free
    of any ``opencomputer.agent.memory`` import.

    Returns a synchronous function with signature
    ``(name: str, args: str, runtime: RuntimeContext) -> SlashCommandResult | None``.
    """

    def _fallback(
        name: str, args: str, runtime: RuntimeContext
    ) -> SlashCommandResult | None:
        try:
            skills = memory_manager.list_skills()
        except Exception:  # noqa: BLE001
            return None

        for skill in skills:
            sid = getattr(skill, "id", None)
            sname = getattr(skill, "name", None)
            if name in (sid, sname):
                try:
                    body = memory_manager.load_skill_body(sid or sname or name)
                except Exception as e:  # noqa: BLE001
                    return SlashCommandResult(
                        output=f"failed to load skill '{name}': {type(e).__name__}: {e}",
                        handled=True,
                        source="skill",
                    )
                if not body:
                    return SlashCommandResult(
                        output=f"skill '{name}' has empty body",
                        handled=True,
                        source="skill",
                    )
                if len(body) > _MAX_BODY_CHARS:
                    body = body[:_MAX_BODY_CHARS] + (
                        f"\n\n[truncated — skill body has "
                        f"{len(body) - _MAX_BODY_CHARS} more chars]"
                    )
                title = getattr(skill, "name", None) or name
                return SlashCommandResult(
                    output=f"## {title}\n\n{body}",
                    handled=True,
                    source="skill",
                )
        # Not a known skill — return None so dispatcher can report
        # unknown-slash to the caller's intended path.
        return None

    return _fallback


__all__ = ["make_skill_fallback"]
