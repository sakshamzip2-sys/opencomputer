"""Bridge ACP tool calls → OpenComputer's PluginAPI.

Currently a stub — for v1, tool calls happen INSIDE the AgentLoop via the
existing PluginAPI surface. ACP doesn't expose tools for the IDE to
invoke directly (per openclaw's spec — IDE-provided MCP servers are
'not yet supported' on the bridge).

Future expansion: when openclaw spec adds per-session MCP, this module
will translate ACP tool descriptors into OC's BaseTool registrations.

PR-D of /Users/saksham/.claude/plans/replicated-purring-dewdrop.md.
"""

from __future__ import annotations

__all__: list[str] = []
