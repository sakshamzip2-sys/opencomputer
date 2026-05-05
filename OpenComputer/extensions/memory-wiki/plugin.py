"""memory-wiki plugin entry — registers 5 wiki tools.

C.2 MVP (2026-05-05). All-stdlib, no external deps. Lives at
<profile-home>/wiki/.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

# Load this plugin's own backend.py via spec_from_file_location under a
# unique synthetic module name. We can't rely on ``from backend
# import`` because the plugin loader doesn't always evict sibling
# ``backend`` entries from ``sys.modules`` between loads, so memory-
# vector's backend can shadow ours. We also can't rely on
# ``extensions.memory_wiki.backend`` — ``extensions/`` is not a Python
# package.
#
# ``sys.modules`` registration MUST happen before ``exec_module`` —
# Python 3.13 ``@dataclass`` does ``sys.modules.get(cls.__module__).__dict__``
# during class construction and explodes on a missing entry.
_BACKEND_NAME = "_memory_wiki_backend_isolated"
if _BACKEND_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _BACKEND_NAME,
        Path(__file__).resolve().parent / "backend.py",
    )
    if _spec is None or _spec.loader is None:
        raise ImportError(
            "Cannot locate memory-wiki backend.py for spec_from_file_location"
        )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[_BACKEND_NAME] = _mod
    _spec.loader.exec_module(_mod)
WikiMemoryBackend = sys.modules[_BACKEND_NAME].WikiMemoryBackend


class _RunToExecute:
    """Bridge ``run(**kwargs) -> dict`` to ``execute(call) -> ToolResult``.

    Mirrors the helper in extensions/memory-vector/plugin.py — see that
    module for the rationale. Mixin must precede BaseTool in the MRO.
    """

    async def execute(self, call: ToolCall) -> ToolResult:
        try:
            result = await self.run(**call.arguments)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — must not raise from execute
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {exc}",
                is_error=True,
            )
        return ToolResult(
            tool_call_id=call.id,
            content=json.dumps(result, default=str),
        )

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


class WikiMemoryAdd(_RunToExecute, BaseTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="WikiMemoryAdd",
            description=(
                "Create a new wiki note. Returns the assigned slug. Use "
                "[[slug]] in the body to link to other notes."
            ),
            parameters={
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


class WikiMemoryRead(_RunToExecute, BaseTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="WikiMemoryRead",
            description="Read a wiki note by slug.",
            parameters={
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


class WikiMemorySearch(_RunToExecute, BaseTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="WikiMemorySearch",
            description=(
                "Substring-search across all wiki notes (title + body). "
                "Returns matching slugs. Uses ripgrep when available."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        )

    async def run(self, *, query) -> dict:
        slugs = _get_backend().search(query)
        return {"slugs": slugs}


class WikiMemoryBacklinks(_RunToExecute, BaseTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="WikiMemoryBacklinks",
            description=(
                "List slugs that reference the given slug via [[slug]] "
                "wikilink syntax."
            ),
            parameters={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        )

    async def run(self, *, slug) -> dict:
        return {"backlinks": _get_backend().backlinks(slug)}


class WikiMemoryDelete(_RunToExecute, BaseTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="WikiMemoryDelete",
            description="Delete a wiki note by slug.",
            parameters={
                "type": "object",
                "properties": {"slug": {"type": "string"}},
                "required": ["slug"],
            },
        )

    async def run(self, *, slug) -> dict:
        return {"deleted": _get_backend().delete(slug)}


def register(api) -> None:
    api.register_tool(WikiMemoryAdd())
    api.register_tool(WikiMemoryRead())
    api.register_tool(WikiMemorySearch())
    api.register_tool(WikiMemoryBacklinks())
    api.register_tool(WikiMemoryDelete())
    logger.info("memory-wiki plugin: 5 tools registered")
