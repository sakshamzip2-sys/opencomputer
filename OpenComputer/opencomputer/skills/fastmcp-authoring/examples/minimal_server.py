"""Minimal FastMCP server example: a single arithmetic tool.

Run with::

    python minimal_server.py

Then register with OpenComputer (separate terminal)::

    opencomputer mcp add adder \\
        --transport stdio \\
        --command 'python /absolute/path/to/minimal_server.py'

This file is dependency-light on purpose — copy it into a new
project, install ``mcp``, and edit. Tests use the :func:`add`
function directly without spawning a server, so the example doubles
as a sanity check that the imports + decorator wiring work.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP(name="adder-example")


@server.tool()
def add(a: int, b: int) -> int:
    """Return ``a + b``.

    Demo tool. Replace with your real functionality. The type hints
    are what the LLM sees as the parameter schema; the docstring is
    the human-readable description.
    """
    return a + b


def main() -> None:
    """Run the server on stdio."""
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
