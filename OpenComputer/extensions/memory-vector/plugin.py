"""memory-vector plugin entry — registers VectorMemoryAdd/Search/Delete tools.

C.1 MVP (2026-05-05). On registration, ChromaDB is NOT eagerly imported
— the import happens on first tool call so the bundled extension
discovery stays fast and the chromadb dep stays optional.
"""

from __future__ import annotations

import logging

try:
    from backend import VectorMemoryBackend  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.memory_vector.backend import VectorMemoryBackend

from plugin_sdk.tool_contract import BaseTool, ToolSchema

logger = logging.getLogger("opencomputer.ext.memory_vector")


# Singleton backend; lazily constructed on first tool invocation.
_BACKEND: VectorMemoryBackend | None = None


def _get_backend() -> VectorMemoryBackend:
    global _BACKEND
    if _BACKEND is None:
        from pathlib import Path

        try:
            from opencomputer.agent.config import _home

            base = _home() / "memory-vector"
        except ImportError:
            base = Path.home() / ".opencomputer" / "memory-vector"
        _BACKEND = VectorMemoryBackend(persist_dir=base)
    return _BACKEND


class VectorMemoryAdd(BaseTool):
    """Store a text chunk in the vector memory."""

    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="VectorMemoryAdd",
            description=(
                "Store a text chunk in the local vector memory for later "
                "semantic search. Returns the document id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to store."},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for filtering.",
                    },
                },
                "required": ["text"],
            },
        )

    async def run(self, *, text: str, tags: list[str] | None = None) -> dict:
        meta = {"tags": list(tags or [])}
        doc_id = _get_backend().add(text, metadata=meta)
        return {"id": doc_id}


class VectorMemorySearch(BaseTool):
    """Semantic-search the vector memory."""

    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="VectorMemorySearch",
            description=(
                "Semantic-search the local vector memory. Returns up to "
                "top_k matching documents with their text + score + metadata."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "top_k": {
                        "type": "integer",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 100,
                    },
                },
                "required": ["query"],
            },
        )

    async def run(self, *, query: str, top_k: int = 5) -> dict:
        hits = _get_backend().search(query, top_k=top_k)
        return {
            "hits": [
                {"id": h.id, "text": h.text, "score": h.score, "metadata": h.metadata}
                for h in hits
            ]
        }


class VectorMemoryDelete(BaseTool):
    """Delete a document from the vector memory by id."""

    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="VectorMemoryDelete",
            description="Delete a document from the vector memory by id.",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        )

    async def run(self, *, id: str) -> dict:
        deleted = _get_backend().delete(id)
        return {"deleted": deleted}


def register(api) -> None:  # PluginAPI duck-typed
    api.register_tool(VectorMemoryAdd)
    api.register_tool(VectorMemorySearch)
    api.register_tool(VectorMemoryDelete)
    logger.info("memory-vector plugin: 3 tools registered")
