"""Agent Client Protocol (ACP) server — OpenComputer as IDE backend.

Allows IDEs (Zed, VS Code with ACP extension, Cursor, Claude Desktop) to
drive OpenComputer via JSON-RPC over stdio. Implements the ACP spec from
openclaw + adapts hermes-agent's tool routing.

Public API:
    from opencomputer.acp import ACPServer
    server = ACPServer()
    asyncio.run(server.serve_stdio())

PR-D of /Users/saksham/.claude/plans/replicated-purring-dewdrop.md.
"""

from opencomputer.acp.server import ACPServer
from opencomputer.acp.session import ACPSession

__all__ = ["ACPServer", "ACPSession"]
