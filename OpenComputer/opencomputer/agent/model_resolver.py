"""Resolve user-facing model aliases to canonical model ids.

Supports chained aliases (a -> b -> c) up to ``max_depth``. Cycles are
detected via a seen-set and raise explicitly so a misconfigured
``model_aliases`` block doesn't infinite-loop.

Example::

    aliases = {"fast": "claude-haiku-4-5-20251001",
               "smart": "claude-opus-4-7"}
    resolve_model("fast", aliases)   # -> "claude-haiku-4-5-20251001"
    resolve_model("claude-opus-4-7", aliases)  # -> pass-through
"""
from __future__ import annotations

DEFAULT_MAX_DEPTH = 5


def resolve_model(
    name: str,
    aliases: dict[str, str] | None,
    *,
    strict: bool = False,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> str:
    """Resolve ``name`` through ``aliases``; return the canonical id.

    Args:
        name: a model id or alias.
        aliases: mapping of alias → target (target may itself be an alias).
            Falsy values (None, {}) short-circuit to ``name`` unchanged.
        strict: if True, raise ValueError when ``name`` is unknown.
            Default False — pass through unknown names so legacy callers
            who don't define aliases keep working.
        max_depth: chain-following depth cap. Cycles are detected via
            seen-set and raise; runaway non-cyclic chains are capped.

    Returns:
        The fully-resolved canonical model id.

    Raises:
        ValueError: on cyclic chain or (when strict) unknown name.
    """
    if not aliases:
        return name
    # Defensive coerce — silently ignore non-str values rather than
    # blowing up if YAML produces ints/None for a value (per AMENDMENTS H6).
    aliases = {str(k): str(v) for k, v in aliases.items() if v is not None}
    seen: set[str] = set()
    current = name
    for _ in range(max_depth):
        if current in seen:
            raise ValueError(f"circular alias chain involving {name!r}")
        seen.add(current)
        if current in aliases:
            current = aliases[current]
            continue
        if strict and name == current and name not in aliases.values():
            raise ValueError(f"unknown model alias {name!r}")
        return current
    raise ValueError(f"alias chain for {name!r} exceeded depth {max_depth}")
