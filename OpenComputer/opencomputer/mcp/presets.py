"""Bundled MCP presets — one-line install for common MCPs.

Eliminates boilerplate: instead of figuring out the right ``--command``,
``--arg``, ``--env`` flags for each MCP server, the user runs:

    opencomputer mcp install <preset>

and gets a working config. Presets are vetted from the official
``modelcontextprotocol/servers`` repo + a few popular community ones.

Adding a new preset:
1. Add a ``Preset`` entry to ``PRESETS`` below.
2. Document any required env var in ``required_env``.
3. Add a row to the docstring's preset table.
4. Add a test case in ``tests/test_mcp_presets.py``.

Each preset returns an ``MCPServerConfig`` ready to drop into
``config.yaml``. Auth / API key resolution is the user's job — we just
flag which env vars need to be set with ``required_env``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from opencomputer.agent.config import MCPServerConfig


@dataclass(frozen=True, slots=True)
class Preset:
    """One MCP server preset.

    Attributes:
        slug: Unique short id (used as ``opencomputer mcp install <slug>``).
        description: Human-readable one-liner.
        config: The ``MCPServerConfig`` produced. Args are filled at install.
        required_env: List of env var names that must be set for the server
            to function. Install warns if any are missing.
        homepage: URL to the server's docs / repo for the install confirmation.
    """

    slug: str
    description: str
    config: MCPServerConfig
    required_env: tuple[str, ...] = field(default_factory=tuple)
    homepage: str = ""


PRESETS: dict[str, Preset] = {
    "filesystem": Preset(
        slug="filesystem",
        description=(
            "Read/write/list files within a configured root directory. "
            "Defaults the root to the current working dir; pass --root to override."
        ),
        config=MCPServerConfig(
            name="filesystem",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-filesystem", "."),
            url="",
            env={},
            headers={},
            enabled=True,
        ),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
    ),
    "github": Preset(
        slug="github",
        description=(
            "Browse repos, read code, list issues + PRs, search code on GitHub. "
            "Requires GITHUB_PERSONAL_ACCESS_TOKEN — create one at github.com/settings/tokens."
        ),
        config=MCPServerConfig(
            name="github",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-github"),
            url="",
            env={"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_PERSONAL_ACCESS_TOKEN}"},
            headers={},
            enabled=True,
        ),
        required_env=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/github",
    ),
    "fetch": Preset(
        slug="fetch",
        description=(
            "Fetch URLs and convert HTML to markdown for the agent. "
            "Useful for reading articles, docs, and web content."
        ),
        config=MCPServerConfig(
            name="fetch",
            transport="stdio",
            command="uvx",
            args=("mcp-server-fetch",),
            url="",
            env={},
            headers={},
            enabled=True,
        ),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
    ),
    "postgres": Preset(
        slug="postgres",
        description=(
            "Read-only Postgres queries via a connection string. "
            "Pass --db-url to set the connection string at install time."
        ),
        config=MCPServerConfig(
            name="postgres",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-postgres", "${POSTGRES_URL}"),
            url="",
            env={"POSTGRES_URL": "${POSTGRES_URL}"},
            headers={},
            enabled=True,
        ),
        required_env=("POSTGRES_URL",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/postgres",
    ),
    "brave-search": Preset(
        slug="brave-search",
        description=(
            "Web search via Brave's API. "
            "Requires BRAVE_API_KEY — get one (free tier) at api.search.brave.com."
        ),
        config=MCPServerConfig(
            name="brave-search",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-brave-search"),
            url="",
            env={"BRAVE_API_KEY": "${BRAVE_API_KEY}"},
            headers={},
            enabled=True,
        ),
        required_env=("BRAVE_API_KEY",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search",
    ),
}


def get_preset(slug: str) -> Preset | None:
    """Return the preset for ``slug`` or ``None`` if unknown."""
    return PRESETS.get(slug)


def list_preset_slugs() -> list[str]:
    """Return the list of preset slugs in declaration order."""
    return list(PRESETS.keys())


__all__ = ["PRESETS", "Preset", "get_preset", "list_preset_slugs"]
