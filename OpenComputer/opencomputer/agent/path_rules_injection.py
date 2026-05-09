"""PathGlobRulesProvider — inject ``[Active Rules]`` per-turn after path-touching tool calls.

v1.1 plan-2 M7.1 wiring (2026-05-09). Translates the recently-touched
file paths from the message history into matched rules from
``.opencomputer/rules/*.md`` and contributes them as a
:class:`DynamicInjectionProvider` text block on the next turn.

Why injection instead of mutating the system prompt directly?

- The base system prompt is FROZEN per session for prefix-cache hits
  (see ``opencomputer/agent/loop.py`` ``_prompt_snapshots``). Mutating
  it mid-session would invalidate the cache and pay full re-encoding
  every turn.
- The InjectionEngine adds text AFTER the cached system prompt every
  turn; it's the canonical surface for cross-cutting context that
  changes per-turn (plan mode, yolo mode, thinking tags, etc.).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from opencomputer.agent.rules_loader import (
    Rule,
    active_rules_for,
    extract_paths_from_tool_call,
    format_rules_block,
)
from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext

logger = logging.getLogger("opencomputer.agent.path_rules_injection")


@dataclass
class PathGlobRulesProvider(DynamicInjectionProvider):
    """Inject ``[Active Rules]`` based on paths touched by recent tool calls.

    Construction:
        ``PathGlobRulesProvider(rules=load_rules_for_active_profile())``

    The ``rules`` list is captured at construction time. Re-construct
    + re-register on the InjectionEngine when rules change on disk
    (the CLI helper :func:`reload_path_rules` provides this).

    Paths are extracted from the message history's recent assistant
    tool calls; specifically, the LAST batch of tool calls (the
    immediately-preceding assistant message). This avoids re-injecting
    the same rule block on every turn even when the agent isn't
    touching new files — the rule fires once per turn that contains
    matching path-touching tool calls, then quiesces.
    """

    rules: list[Rule]

    #: Lower runs first per InjectionEngine convention. 60 sits between
    #: built-in modes (plan=10, yolo=20, custom 50+) and user-added
    #: providers (≥100). Operators rarely override.
    priority: int = 60

    @property
    def provider_id(self) -> str:
        return "path_glob_rules"

    async def collect(self, ctx: InjectionContext) -> str | None:
        """Return the rules block for paths touched in the last assistant turn."""
        if not self.rules:
            return None
        paths = self._paths_in_last_assistant_turn(ctx)
        if not paths:
            return None
        matched = active_rules_for(self.rules, paths)
        if not matched:
            return None
        block = format_rules_block(matched)
        return block or None

    @staticmethod
    def _paths_in_last_assistant_turn(ctx: InjectionContext) -> list[str]:
        """Extract path arguments from the most-recent assistant tool calls.

        Walks backwards through ``ctx.messages`` to find the last
        assistant message; pulls every tool call from it and runs each
        through :func:`extract_paths_from_tool_call`. Returns the
        de-duplicated list (preserving insertion order) so the matcher
        sees each path once.
        """
        seen: dict[str, None] = {}
        for msg in reversed(ctx.messages):
            if msg.role != "assistant":
                continue
            tool_calls = getattr(msg, "tool_calls", None) or []
            for call in tool_calls:
                name = getattr(call, "name", "") or getattr(call, "tool_name", "")
                args = getattr(call, "arguments", None)
                if args is None:
                    args = getattr(call, "args", None)
                if not isinstance(args, dict):
                    continue
                for path in extract_paths_from_tool_call(name, args):
                    seen.setdefault(path, None)
            if seen:
                # Stop at the first assistant turn that produced paths —
                # we only want the *most recent* batch, not the whole
                # session's history of file touches.
                break
        return list(seen.keys())


def load_rules_for_active_profile() -> list[Rule]:
    """Read workspace + active-profile rules dirs and merge them.

    Resolution:

    * Workspace dir: ``./.opencomputer/rules/`` relative to ``cwd``.
    * Profile dir: ``<active_profile_home>/rules/`` resolved via
      ``opencomputer.agent.config._home``.

    Workspace shadows profile by name (see :func:`merged_rules`).
    Errors at load time (malformed YAML, OSError) are logged + skipped
    by the loader; a totally absent rules dir returns ``[]``.
    """
    from pathlib import Path

    from opencomputer.agent.config import _home as _profile_home
    from opencomputer.agent.rules_loader import merged_rules

    workspace_rules = Path.cwd() / ".opencomputer" / "rules"
    profile_rules = _profile_home() / "rules"
    return merged_rules(workspace_rules, profile_rules)


__all__ = ["PathGlobRulesProvider", "load_rules_for_active_profile"]
