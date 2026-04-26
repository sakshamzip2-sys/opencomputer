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
    # ─── Round 4 catalog expansion (15 entries) ────────────────────────
    # Picked from the official modelcontextprotocol/servers repo plus
    # high-volume third-party MCPs commonly requested. Order: official
    # first, then community. Each must have a public install path
    # (npm/pypi) and clear required_env. Slugs match the canonical
    # short name used in MCP-server READMEs to keep `mcp install <name>`
    # discoverable.
    "sqlite": Preset(
        slug="sqlite",
        description=(
            "Run SQL queries against a local SQLite database. "
            "Pass --db-path to point at your file at install time."
        ),
        config=MCPServerConfig(
            name="sqlite",
            transport="stdio",
            command="uvx",
            args=("mcp-server-sqlite", "--db-path", "${SQLITE_DB_PATH}"),
            url="",
            env={"SQLITE_DB_PATH": "${SQLITE_DB_PATH}"},
            headers={},
            enabled=True,
        ),
        required_env=("SQLITE_DB_PATH",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/sqlite",
    ),
    "gitlab": Preset(
        slug="gitlab",
        description=(
            "Browse GitLab repos, issues, MRs. "
            "Requires GITLAB_PERSONAL_ACCESS_TOKEN."
        ),
        config=MCPServerConfig(
            name="gitlab",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-gitlab"),
            url="",
            env={
                "GITLAB_PERSONAL_ACCESS_TOKEN": "${GITLAB_PERSONAL_ACCESS_TOKEN}",
                "GITLAB_API_URL": "${GITLAB_API_URL:-https://gitlab.com/api/v4}",
            },
            headers={},
            enabled=True,
        ),
        required_env=("GITLAB_PERSONAL_ACCESS_TOKEN",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/gitlab",
    ),
    "google-drive": Preset(
        slug="google-drive",
        description=(
            "Read/search files in Google Drive via OAuth. "
            "Run the included auth helper after install to grant access."
        ),
        config=MCPServerConfig(
            name="google-drive",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-gdrive"),
            url="",
            env={},
            headers={},
            enabled=True,
        ),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/gdrive",
    ),
    "slack": Preset(
        slug="slack",
        description=(
            "Read Slack channels + send messages. Requires SLACK_BOT_TOKEN "
            "(starts with xoxb-) and SLACK_TEAM_ID."
        ),
        config=MCPServerConfig(
            name="slack",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-slack"),
            url="",
            env={
                "SLACK_BOT_TOKEN": "${SLACK_BOT_TOKEN}",
                "SLACK_TEAM_ID": "${SLACK_TEAM_ID}",
            },
            headers={},
            enabled=True,
        ),
        required_env=("SLACK_BOT_TOKEN", "SLACK_TEAM_ID"),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/slack",
    ),
    "memory": Preset(
        slug="memory",
        description=(
            "Knowledge-graph memory persisted to a local JSON file. "
            "Lets the agent build long-term notes across sessions."
        ),
        config=MCPServerConfig(
            name="memory",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-memory"),
            url="",
            env={},
            headers={},
            enabled=True,
        ),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
    ),
    "puppeteer": Preset(
        slug="puppeteer",
        description=(
            "Headless-Chrome browser control: navigate, screenshot, click, fill forms. "
            "Pulls Chromium on first run (~150MB)."
        ),
        config=MCPServerConfig(
            name="puppeteer",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-puppeteer"),
            url="",
            env={},
            headers={},
            enabled=True,
        ),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/puppeteer",
    ),
    "sequential-thinking": Preset(
        slug="sequential-thinking",
        description=(
            "Adds a structured 'think step by step' tool the agent can call "
            "to break down hard problems. Stateless; no env vars."
        ),
        config=MCPServerConfig(
            name="sequential-thinking",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-sequential-thinking"),
            url="",
            env={},
            headers={},
            enabled=True,
        ),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
    ),
    "time": Preset(
        slug="time",
        description=(
            "Current time + timezone conversion as a tool. "
            "Useful for cross-zone scheduling chats."
        ),
        config=MCPServerConfig(
            name="time",
            transport="stdio",
            command="uvx",
            args=("mcp-server-time",),
            url="",
            env={},
            headers={},
            enabled=True,
        ),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/time",
    ),
    "everart": Preset(
        slug="everart",
        description=(
            "Image generation via EverArt's API. Requires EVERART_API_KEY."
        ),
        config=MCPServerConfig(
            name="everart",
            transport="stdio",
            command="npx",
            args=("-y", "@modelcontextprotocol/server-everart"),
            url="",
            env={"EVERART_API_KEY": "${EVERART_API_KEY}"},
            headers={},
            enabled=True,
        ),
        required_env=("EVERART_API_KEY",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/everart",
    ),
    # ─── Community / third-party ──────────────────────────────────────
    "notion": Preset(
        slug="notion",
        description=(
            "Read/write Notion pages + databases via the official API. "
            "Requires NOTION_API_TOKEN — create at notion.so/profile/integrations."
        ),
        config=MCPServerConfig(
            name="notion",
            transport="stdio",
            command="npx",
            args=("-y", "@notionhq/notion-mcp-server"),
            url="",
            env={
                "OPENAPI_MCP_HEADERS": '{"Authorization": "Bearer ${NOTION_API_TOKEN}", "Notion-Version": "2022-06-28"}',
            },
            headers={},
            enabled=True,
        ),
        required_env=("NOTION_API_TOKEN",),
        homepage="https://github.com/makenotion/notion-mcp-server",
    ),
    "linear": Preset(
        slug="linear",
        description=(
            "Linear issues + projects. Requires LINEAR_API_KEY from "
            "linear.app/settings/api."
        ),
        config=MCPServerConfig(
            name="linear",
            transport="stdio",
            command="npx",
            args=("-y", "@linear/mcp-server-linear"),
            url="",
            env={"LINEAR_API_KEY": "${LINEAR_API_KEY}"},
            headers={},
            enabled=True,
        ),
        required_env=("LINEAR_API_KEY",),
        homepage="https://github.com/linear/mcp",
    ),
    "sentry": Preset(
        slug="sentry",
        description=(
            "Pull Sentry issue details + stack traces into the agent. "
            "Requires SENTRY_AUTH_TOKEN."
        ),
        config=MCPServerConfig(
            name="sentry",
            transport="stdio",
            command="uvx",
            args=("mcp-server-sentry", "--auth-token", "${SENTRY_AUTH_TOKEN}"),
            url="",
            env={"SENTRY_AUTH_TOKEN": "${SENTRY_AUTH_TOKEN}"},
            headers={},
            enabled=True,
        ),
        required_env=("SENTRY_AUTH_TOKEN",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/sentry",
    ),
    "context7": Preset(
        slug="context7",
        description=(
            "Up-to-date library docs lookup (Upstash Context7). "
            "Free tier; no API key required."
        ),
        config=MCPServerConfig(
            name="context7",
            transport="stdio",
            command="npx",
            args=("-y", "@upstash/context7-mcp"),
            url="",
            env={},
            headers={},
            enabled=True,
        ),
        homepage="https://github.com/upstash/context7-mcp",
    ),
    "perplexity": Preset(
        slug="perplexity",
        description=(
            "Perplexity AI search as a tool. Requires PERPLEXITY_API_KEY."
        ),
        config=MCPServerConfig(
            name="perplexity",
            transport="stdio",
            command="npx",
            args=("-y", "perplexity-mcp"),
            url="",
            env={"PERPLEXITY_API_KEY": "${PERPLEXITY_API_KEY}"},
            headers={},
            enabled=True,
        ),
        required_env=("PERPLEXITY_API_KEY",),
        homepage="https://github.com/jaacob/perplexity-mcp",
    ),
    "docker": Preset(
        slug="docker",
        description=(
            "Manage local Docker containers + images via the daemon. "
            "Daemon must be running; no API key needed."
        ),
        config=MCPServerConfig(
            name="docker",
            transport="stdio",
            command="uvx",
            args=("docker-mcp",),
            url="",
            env={},
            headers={},
            enabled=True,
        ),
        homepage="https://github.com/QuantGeekDev/docker-mcp",
    ),
}


def get_preset(slug: str) -> Preset | None:
    """Return the preset for ``slug`` or ``None`` if unknown."""
    return PRESETS.get(slug)


def list_preset_slugs() -> list[str]:
    """Return the list of preset slugs in declaration order."""
    return list(PRESETS.keys())


__all__ = ["PRESETS", "Preset", "get_preset", "list_preset_slugs"]
