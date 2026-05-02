"""Memory tool — the agent's handle for curating declarative memory.

Exposes four actions over two targets:

  action=add / target=memory|user      → append entry to MEMORY.md / USER.md
  action=replace / target=memory|user  → find + replace substring
  action=remove / target=memory|user   → delete a block
  action=read / target=memory|user     → return full file contents

All writes go through MemoryManager's atomic + locked + backed-up write
path, so this tool inherits those safety properties. The agent never
needs to know about locking, temp files, or backups.

Errors are returned as ToolResult(is_error=True) — this tool MUST NOT raise.
"""

from __future__ import annotations

from typing import Any

from opencomputer.agent.memory import MemoryTooLargeError
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_VALID_ACTIONS = {"add", "replace", "remove", "read"}
_VALID_TARGETS = {"memory", "user"}


class MemoryTool(BaseTool):
    """Curate MEMORY.md (agent observations) or USER.md (user profile)."""

    parallel_safe = False  # writes to disk; backup rotation not re-entrant

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Memory",
            description=(
                "Read or mutate the user's declarative memory file (MEMORY.md or USER.md).\n"
                "\n"
                "Use this when:\n"
                "  - The user says 'remember that X' or 'add to my notes that Y'\n"
                "  - You need to record a stable fact (preference, decision, contact info)\n"
                "  - You need to retrieve a specific stable fact from MEMORY.md / USER.md\n"
                "\n"
                "Do NOT use this for:\n"
                "  - Searching past conversations by topic — use Recall (semantic search)\n"
                "  - Reading a known session's transcript — use SessionsHistory\n"
                "  - Listing recent sessions — use SessionsList\n"
                "  - Storing per-conversation state — use the session naturally\n"
                "\n"
                "Targets: target='memory' → MEMORY.md (agent learned facts). "
                "target='user' → USER.md (preferences user stated).\n"
                "\n"
                "Actions:\n"
                "  add    — append an entry to the target file\n"
                "  replace — find+replace a substring in the target\n"
                "  remove — delete a block from the target\n"
                "  read   — return current contents of the target\n"
                "\n"
                "Files are bounded (MEMORY.md: ~4000 chars, USER.md: ~2000). "
                "Over-limit writes return an error; use remove to free space."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": sorted(_VALID_ACTIONS),
                        "description": "What to do.",
                    },
                    "target": {
                        "type": "string",
                        "enum": sorted(_VALID_TARGETS),
                        "description": (
                            "'memory' = MEMORY.md (agent observations). "
                            "'user' = USER.md (user preferences)."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "For action=add: the text to append. "
                            "For action=remove: the block text to delete."
                        ),
                    },
                    "old": {
                        "type": "string",
                        "description": "For action=replace: substring to find.",
                    },
                    "new": {
                        "type": "string",
                        "description": "For action=replace: replacement text.",
                    },
                },
                "required": ["action", "target"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments or {}
        action = str(args.get("action", "")).lower()
        target = str(args.get("target", "")).lower()

        if action not in _VALID_ACTIONS:
            return self._err(call.id, f"unknown action '{action}'")
        if target not in _VALID_TARGETS:
            return self._err(call.id, f"unknown target '{target}'")

        mm = self._ctx.manager
        try:
            if action == "read":
                content = mm.read_declarative() if target == "memory" else mm.read_user()
                return ToolResult(
                    tool_call_id=call.id,
                    content=content or "(empty)",
                    is_error=False,
                )

            if action == "add":
                text = str(args.get("content", "")).strip()
                if not text:
                    return self._err(call.id, "content required for add")
                if target == "memory":
                    mm.append_declarative(text)
                else:
                    mm.append_user(text)
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Added entry to {target.upper()}.md",
                    is_error=False,
                )

            if action == "replace":
                old = str(args.get("old", ""))
                new = str(args.get("new", ""))
                if not old:
                    return self._err(call.id, "'old' required for replace")
                ok = (
                    mm.replace_declarative(old, new)
                    if target == "memory"
                    else mm.replace_user(old, new)
                )
                if not ok:
                    return self._err(
                        call.id,
                        f"substring not found in {target.upper()}.md",
                    )
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Replaced in {target.upper()}.md",
                    is_error=False,
                )

            if action == "remove":
                block = str(args.get("content", ""))
                if not block:
                    return self._err(call.id, "content required for remove")
                ok = mm.remove_declarative(block) if target == "memory" else mm.remove_user(block)
                if not ok:
                    return self._err(
                        call.id,
                        f"block not found in {target.upper()}.md",
                    )
                return ToolResult(
                    tool_call_id=call.id,
                    content=f"Removed from {target.upper()}.md",
                    is_error=False,
                )

            # Unreachable due to earlier validation.
            return self._err(call.id, f"unhandled action '{action}'")

        except MemoryTooLargeError as e:
            return self._err(
                call.id,
                f"write rejected: would exceed char limit "
                f"({e.would_be} > {e.limit}); use remove to free space",
            )
        except Exception as e:  # pragma: no cover — defensive
            return self._err(call.id, f"memory op failed: {e}")

    @staticmethod
    def _err(tool_call_id: str, msg: str) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            content=f"Error: {msg}",
            is_error=True,
        )
