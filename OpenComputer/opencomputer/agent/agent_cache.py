"""Agent cache — generic LRU of warmed objects keyed by structural signature.

Two production use cases share the same engine:

1. **AgentLoop instance cache** (original Phase 12a intent). Without this,
   every call into ``Dispatch.handle_message`` or ``DelegateTool`` would
   build a fresh ``AgentLoop`` and throw away the per-session
   prompt-snapshot LRU. ``AgentRouter`` (gateway/agent_router.py) is the
   active production caller for this shape, keyed on ``profile_id``.

2. **Aux-LLM response cache** (v1.1 plan-1 M1.3, 2026-05-09). The
   ``opencomputer.agent.aux_llm`` helpers expose an opt-in
   ``use_cache=True`` parameter that memoizes deterministic
   ``provider.complete()`` calls (e.g. smart-mode security assessments
   at temperature=0.0, where the same command+scope always yields the
   same risk verdict). Cache key is :func:`aux_response_signature`.

The class itself stores ``Any`` and is intentionally generic — the
key shape is what carries the contract. Both signatures funnel through
the same ``OrderedDict + popitem(last=False)`` LRU.

Source: hermes-agent 03-borrowables.md §Agent cache.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

DEFAULT_AGENT_CACHE_MAX = 32

#: Hard cap on aux-LLM response cache memory. Each entry is a string ≤
#: ``aux_llm.complete_text(max_tokens=...)`` which is bounded at the call
#: site. 256 entries × ~8KB each ≈ 2MB worst case.
DEFAULT_AUX_RESPONSE_CACHE_MAX = 256


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


def aux_response_signature(
    *,
    provider_name: str,
    model: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
    temperature: float,
) -> tuple[Any, ...]:
    """Build the cache key for an aux-LLM response.

    M1.3 (2026-05-09). Mirrors the ``aux_llm.complete_text`` argument
    shape so opt-in callers don't have to pre-hash anything — the
    signature function does it. Uses sha256 hashing on the
    ``(system, messages)`` text so:

    * The key is fixed-size regardless of prompt length.
    * Two semantically-equivalent prompts with whitespace differences
      hash differently — that is intentional. Aux-LLM responses can be
      sensitive to whitespace at temperature=0; treating them as
      identical would be a silent miss.

    ``temperature`` IS part of the key (different temperatures sample
    different distributions). At temperature > 0 the cache is still
    "correct" in that it returns one valid sample, but callers SHOULD
    NOT opt in to caching at temperature > 0 unless they explicitly
    want sample re-use.
    """
    payload = json.dumps({"system": system, "messages": messages}, sort_keys=True)
    text_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return (provider_name, model, text_hash, max_tokens, temperature)


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


__all__ = [
    "DEFAULT_AGENT_CACHE_MAX",
    "DEFAULT_AUX_RESPONSE_CACHE_MAX",
    "AgentCache",
    "aux_response_signature",
    "config_signature",
]
