"""memory-wiki plugin entry — registers 5 wiki tools.

C.2 MVP (2026-05-05). All-stdlib, no external deps. Lives at
<profile-home>/wiki/.
"""

from __future__ import annotations

import logging

try:
    from backend import WikiMemoryBackend  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.memory_wiki.backend import WikiMemoryBackend

from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger("opencomputer.ext.memory_wiki")


_BACKEND: WikiMemoryBackend | None = None


def _get_backend() -> WikiMemoryBackend:
    """Resolve the active-profile home via the SDK (no opencomputer.* import)."""
    global _BACKEND
    if _BACKEND is None:
        import os
        from pathlib import Path

        from plugin_sdk import current_profile_home

        scope = current_profile_home.get()
        if scope is not None:
            base = Path(scope) / "wiki"
        else:
            env_home = os.environ.get("OPENCOMPUTER_HOME", "").strip()
            home_root = (
                Path(env_home) if env_home else Path.home() / ".opencomputer"
            )
            base = home_root / "wiki"
        _BACKEND = WikiMemoryBackend(root=base)
    return _BACKEND


class WikiMemoryAdd(BaseTool):
    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="WikiMemoryAdd",
            description=(
                "Create a new wiki note. Returns the assigned slug. Use "
                "[[slug]] in the body to link to other notes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "slug": {
                        "type": "string",
                        "description": "Optional explicit slug (lowercase a-z 0-9 _ -).",
                    },
                },
                "required": ["title", "body"],
            },
        )

    async def run(self, *, title, body, tags=None, slug=None) -> dict:
        chosen = _get_backend().add(
            title=title, body=body, tags=tuple(tags or []), slug=slug
        )
        return {"slug": chosen}


class WikiMemoryRead(BaseTool):
    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="WikiMemoryRead",
            description="Read a wiki note by slug.",
            input_schema={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        )

    async def run(self, *, slug) -> dict:
        note = _get_backend().read(slug)
        if note is None:
            return {"found": False}
        return {
            "found": True,
            "slug": note.slug,
            "title": note.title,
            "body": note.body,
            "tags": list(note.tags),
            "created_at": note.created_at,
            "updated_at": note.updated_at,
        }


class WikiMemorySearch(BaseTool):
    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="WikiMemorySearch",
            description=(
                "Substring-search across all wiki notes (title + body). "
                "Returns matching slugs. Uses ripgrep when available."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )

    async def run(self, *, query) -> dict:
        slugs = _get_backend().search(query)
        return {"slugs": slugs}


class WikiMemoryBacklinks(BaseTool):
    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="WikiMemoryBacklinks",
            description=(
                "List slugs that reference the given slug via [[slug]] "
                "wikilink syntax."
            ),
            input_schema={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        )

    async def run(self, *, slug) -> dict:
        return {"backlinks": _get_backend().backlinks(slug)}


class WikiMemoryDelete(BaseTool):
    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="WikiMemoryDelete",
            description="Delete a wiki note by slug.",
            input_schema={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        )

    async def run(self, *, slug) -> dict:
        return {"deleted": _get_backend().delete(slug)}


def register(api) -> None:
    api.register_tool(WikiMemoryAdd)
    api.register_tool(WikiMemoryRead)
    api.register_tool(WikiMemorySearch)
    api.register_tool(WikiMemoryBacklinks)
    api.register_tool(WikiMemoryDelete)
    logger.info("memory-wiki plugin: 5 tools registered")
