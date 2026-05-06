"""memory-vector plugin entry — registers VectorMemoryAdd/Search/Delete tools.

C.1 MVP (2026-05-05). On registration, ChromaDB is NOT eagerly imported
— the import happens on first tool call so the bundled extension
discovery stays fast and the chromadb dep stays optional.
"""

from __future__ import annotations

import json
import logging

try:
    from backend import VectorMemoryBackend  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.memory_vector.backend import VectorMemoryBackend

from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema


class _RunToExecute:
    """Bridge ``run(**kwargs) -> dict`` to ``execute(call) -> ToolResult``.

    The plugin authors implemented kwargs-style ``run`` methods, but
    BaseTool's contract is ``execute(call: ToolCall) -> ToolResult``.
    This mixin satisfies the abstract method by serializing the dict
    result and trapping exceptions into ``is_error`` ToolResults.

    Mixin must precede BaseTool in the MRO so its concrete ``execute``
    resolves before BaseTool's ``@abstractmethod execute``.
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

logger = logging.getLogger("opencomputer.ext.memory_vector")


# Singleton backend; lazily constructed on first tool invocation.
_BACKEND: VectorMemoryBackend | None = None


def _get_backend() -> VectorMemoryBackend:
    """Resolve the active-profile home via the SDK (no opencomputer.* import).

    ``plugin_sdk.current_profile_home`` is a ContextVar set by the
    gateway dispatcher; falls back to ``OPENCOMPUTER_HOME`` env var,
    then ``~/.opencomputer``.
    """
    global _BACKEND
    if _BACKEND is None:
        import os
        from pathlib import Path

        from plugin_sdk import current_profile_home

        scope = current_profile_home.get()
        if scope is not None:
            base = Path(scope) / "memory-vector"
        else:
            env_home = os.environ.get("OPENCOMPUTER_HOME", "").strip()
            home_root = (
                Path(env_home) if env_home else Path.home() / ".opencomputer"
            )
            base = home_root / "memory-vector"
        _BACKEND = VectorMemoryBackend(persist_dir=base)
    return _BACKEND


class VectorMemoryAdd(_RunToExecute, BaseTool):
    """Store a text chunk in the vector memory."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="VectorMemoryAdd",
            description=(
                "Store a text chunk in the local ChromaDB-backed vector "
                "memory for later semantic search. Use when you want to "
                "remember a freeform note, observation, or transcript "
                "snippet that should later be findable by meaning rather "
                "than keyword. Returns the document id."
            ),
            parameters={
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


class VectorMemorySearch(_RunToExecute, BaseTool):
    """Semantic-search the vector memory."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="VectorMemorySearch",
            description=(
                "Semantic-search the local vector memory by meaning rather "
                "than exact keyword match. Use when you remember the gist "
                "of something but not the wording. Returns up to top_k "
                "matching documents with their text, score, and metadata."
            ),
            parameters={
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


class VectorMemoryDelete(_RunToExecute, BaseTool):
    """Delete a document from the vector memory by id."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="VectorMemoryDelete",
            description=(
                "Delete a single document from the vector memory by its "
                "id (returned at VectorMemoryAdd time). Use when a stored "
                "note becomes wrong or sensitive and should no longer "
                "appear in future semantic searches."
            ),
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        )

    async def run(self, *, id: str) -> dict:
        deleted = _get_backend().delete(id)
        return {"deleted": deleted}


def register(api) -> None:  # PluginAPI duck-typed
    api.register_tool(VectorMemoryAdd())
    api.register_tool(VectorMemorySearch())
    api.register_tool(VectorMemoryDelete())
    logger.info("memory-vector plugin: 3 tools registered")
