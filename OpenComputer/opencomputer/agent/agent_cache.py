"""Agent cache — LRU of warmed `AgentLoop` instances keyed by config signature.

Without this, every call into `Dispatch.handle_message` (gateway) or
`DelegateTool` (subagent) builds a fresh `AgentLoop`, which throws away the
per-session prompt-snapshot cache and the LRU it lives behind. Repeated calls
under the same configuration end up paying full prompt-build cost every time.

The cache key is a `config_signature` tuple over the dimensions that change
the prompt or the tool schemas. If any of those drift (model swap, new
plugin loaded mid-process, config edit), the signature changes and the cache
returns a miss — never a stale loop.

Mirrors `_prompt_snapshots` in `opencomputer/agent/loop.py` — same
`OrderedDict + popitem(last=False)` LRU pattern.

Source: hermes-agent 03-borrowables.md §Agent cache.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

DEFAULT_AGENT_CACHE_MAX = 32


def config_signature(
    *,
    provider_name: str,
    model: str,
    system_prompt_hash: str,
    tool_names: Iterable[str],
    extras: tuple[Any, ...] = (),
) -> tuple[Any, ...]:
    """Build the cache key for an `AgentLoop` instance.

    Tools are sorted before hashing so insertion order in the registry doesn't
    spuriously invalidate the cache. `extras` lets callers inject any
    additional context-affecting state (e.g. plan-mode flag).
    """
    return (
        provider_name,
        model,
        system_prompt_hash,
        tuple(sorted(tool_names)),
        extras,
    )


@dataclass(slots=True)
class AgentCache:
    """LRU keyed by `config_signature`. Stores `AgentLoop`-like instances."""

    max_size: int = DEFAULT_AGENT_CACHE_MAX
    _store: OrderedDict[tuple[Any, ...], Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._store is None:
            self._store = OrderedDict()

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: tuple[Any, ...]) -> bool:
        return key in self._store

    def get(self, key: tuple[Any, ...]) -> Any | None:
        """LRU read — returns the value and marks it most-recently-used."""
        value = self._store.get(key)
        if value is not None:
            self._store.move_to_end(key)
        return value

    def put(self, key: tuple[Any, ...], value: Any) -> None:
        """Insert or refresh. Evicts the least-recently-used entry if full."""
        if key in self._store:
            self._store.move_to_end(key)
            self._store[key] = value
            return
        # Evict before insert so we never momentarily exceed max_size.
        while len(self._store) >= self.max_size:
            self._store.popitem(last=False)
        self._store[key] = value

    def get_or_create(self, key: tuple[Any, ...], factory: Callable[[], Any]) -> Any:
        """Convenience — read; if miss, build via `factory()`, store, return."""
        existing = self.get(key)
        if existing is not None:
            return existing
        built = factory()
        self.put(key, built)
        return built

    def invalidate(self, key: tuple[Any, ...]) -> None:
        """Drop one entry. No-op if absent."""
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


__all__ = ["DEFAULT_AGENT_CACHE_MAX", "AgentCache", "config_signature"]
