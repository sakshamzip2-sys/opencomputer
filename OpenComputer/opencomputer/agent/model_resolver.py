"""Resolve user-facing model aliases to canonical model ids.

Supports chained aliases (a -> b -> c) up to ``max_depth``. Cycles are
detected via a seen-set and raise explicitly so a misconfigured
``model_aliases`` block doesn't infinite-loop.

Resolution order (highest → lowest priority):

1. **User-defined aliases** from ``ModelConfig.model_aliases`` — user
   config always wins so a power user can remap ``opus`` to a custom
   endpoint.
2. **Built-in short-name fallbacks** for the canonical Anthropic
   families — so ``/model opus`` works out of the box without
   requiring every user to set up ``model_aliases`` in config.yaml.
   Added 2026-05-11 after a fresh user typed ``/model opus`` and
   got a 404 (``'message': 'model: opus'``) — the literal short
   name was forwarded to the Anthropic API.
3. **Pass-through** — names that look like a real id (contain a ``-``)
   are returned unchanged. Names that look like an unrecognised short
   alias raise ``ValueError`` so swap_model can refuse the swap rather
   than silently persist a bogus id.

Example::

    aliases = {"fast": "claude-haiku-4-5-20251001",
               "smart": "claude-opus-4-7"}
    resolve_model("fast", aliases)        # → "claude-haiku-4-5-20251001"
    resolve_model("opus", aliases)        # → "claude-opus-4-7"  (builtin)
    resolve_model("opus", {})             # → "claude-opus-4-7"  (builtin)
    resolve_model("claude-opus-4-7", {})  # → pass-through
    resolve_model("totally-unknown", {})  # → pass-through (looks like an id)
    resolve_model("opuse", {})            # → ValueError (short, unrecognised)
"""
from __future__ import annotations

DEFAULT_MAX_DEPTH = 5

#: Built-in short-name aliases for the canonical Anthropic model families.
#: User-defined aliases from ``model_aliases`` ALWAYS win over these — the
#: builtins only kick in when the user hasn't already mapped the short name.
#:
#: Maintenance: keep these pinned to the LATEST available family member so
#: ``/model opus`` always means "the best opus you currently have". When a
#: new release lands, bump these values in lockstep with whatever the
#: ``setup_wizard.py`` / ``model_capabilities.py`` defaults track. Keep this
#: list minimal — short names exist for muscle memory, not as a model
#: catalog. The full picker (``oc model``) is the discovery surface.
_BUILTIN_SHORT_ALIASES: dict[str, str] = {
    "opus": "claude-opus-4-7",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}


def _looks_like_model_id(name: str) -> bool:
    """Heuristic: does this string look like a real model id?

    Real model ids contain at least one ``-`` separator (claude-opus-4-7,
    gpt-4o, mistral-large-2407) or a ``/`` (openrouter/anthropic/...).
    Bare lowercase strings without separators are almost always either a
    short alias OR a typo. We use this signal to decide whether to pass
    an unrecognised name through (legacy compat) or reject it loudly
    (no silent 404).
    """
    return "-" in name or "/" in name or ":" in name


def resolve_model(
    name: str,
    aliases: dict[str, str] | None,
    *,
    strict: bool = False,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> str:
    """Resolve ``name`` through ``aliases`` (then builtin short names).

    Args:
        name: a model id or alias.
        aliases: user-defined ``alias → target`` map. Always tried first.
            Falsy values (None, {}) skip straight to the builtin fallback.
            Target may itself be an alias (chained up to ``max_depth``).
        strict: if True, raise ``ValueError`` when ``name`` is unknown
            even after builtin fallback. Default False — but a name
            that is a bare lowercase short string without a ``-`` /
            ``/`` / ``:`` separator AND not in any alias map is rejected
            regardless of ``strict`` to prevent the silent-404 trap
            (``/model opus`` storing literal ``"opus"`` and tanking on
            the next API call).
        max_depth: chain-following depth cap.

    Returns:
        The fully-resolved canonical model id.

    Raises:
        ValueError: on cyclic alias chain, depth overflow, unrecognised
            bare-short name, or (when strict) any unknown name.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"model name must be a non-empty string (got {name!r})")
    # Defensive coerce — silently ignore non-str values rather than
    # blowing up if YAML produces ints/None for a value (per AMENDMENTS H6).
    user_aliases = (
        {str(k): str(v) for k, v in aliases.items() if v is not None}
        if aliases
        else {}
    )
    seen: set[str] = set()
    current = name
    for _ in range(max_depth):
        if current in seen:
            raise ValueError(f"circular alias chain involving {name!r}")
        seen.add(current)
        if current in user_aliases:
            current = user_aliases[current]
            continue
        if current in _BUILTIN_SHORT_ALIASES:
            current = _BUILTIN_SHORT_ALIASES[current]
            continue
        # No more alias to resolve. Decide whether to accept the name as
        # a canonical id or reject it.
        if _looks_like_model_id(current):
            return current
        if strict:
            raise ValueError(f"unknown model alias {name!r}")
        # Bare short name not in any alias map — the swap would silently
        # persist garbage and produce a 404 on the next API call. Reject.
        known_short = sorted(_BUILTIN_SHORT_ALIASES.keys())
        raise ValueError(
            f"unknown model alias {name!r}; built-in short names are "
            f"{known_short}, or pass a full id like 'claude-opus-4-7'"
        )
    raise ValueError(f"alias chain for {name!r} exceeded depth {max_depth}")
