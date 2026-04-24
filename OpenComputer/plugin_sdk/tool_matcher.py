"""
III.2 — Pattern-syntax tool allowlist entries.

Plugins (and subagent callers) declare an ``allowed_tools`` list. Entries
can be in one of three shapes:

* **Bare name** — ``"Read"``. Exact match on the tool name. This is the
  III.1 shape, kept for backwards compat.
* **Prefix glob** — ``"mcp__*"``, ``"DevTools*"``. Allow any tool whose
  name starts with the prefix. Only a trailing ``*`` is accepted.
* **Tool + arg glob** — ``"Bash(gh issue view:*)"``. Allow that specific
  tool ONLY when its first meaningful string argument matches the glob
  (``fnmatch.fnmatchcase``).

This module parses those entries into ``ToolPattern`` values and decides
whether a given ``(tool_name, tool_args)`` pair is allowed.

Mirrors Claude Code's ``allowed-tools:`` frontmatter syntax (see
``sources/claude-code/plugins/code-review/commands/code-review.md``)
ported to OpenComputer's actual dispatching surface (subagent spawn via
``DelegateTool``).

### Per-tool "first meaningful arg" map

Arg-pattern matching needs to know which field of ``tool_args`` to match
against. We hardcode this per tool rather than trying to match against a
JSON-serialized arg blob (which would give surprising results — e.g.
escaped quotes, key-order dependence, mixed fields all scrambled
together).

Tools NOT in this map fail closed when an arg pattern is specified: the
caller asked us to restrict on an arg shape we don't know how to
interpret, so the safest answer is to refuse. Plugin authors who need
to allow a new tool with arg filtering should submit a PR extending the
map.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

# Per-tool "which field is the first meaningful string arg". Matching is
# done via ``fnmatch.fnmatchcase(args[field], pattern)``. Tools not in this
# map that get an arg-pattern entry fail closed (see ``matches``).
_FIRST_ARG_FIELD: dict[str, str] = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}


@dataclass(frozen=True, slots=True)
class ToolPattern:
    """Parsed ``allowed_tools`` entry.

    Fields:
        raw: the original string (e.g. ``"Bash(gh issue view:*)"``).
        tool_name: the tool name part. For bare names and arg-patterned
            entries, this is the exact tool name. For prefix globs, this
            is the prefix WITH the trailing ``*`` stripped.
        arg_pattern: an fnmatch glob pattern matched against the tool's
            first meaningful string arg (see ``_FIRST_ARG_FIELD``).
            ``None`` for bare names and prefix globs.
        is_prefix: True when the original entry ended in ``*`` (prefix
            match across tool names). Mutually exclusive with a
            non-``None`` ``arg_pattern``.
    """

    raw: str
    tool_name: str
    arg_pattern: str | None
    is_prefix: bool


def parse(entry: str) -> ToolPattern:
    """Parse a single ``allowed_tools`` string into a ``ToolPattern``.

    Raises:
        ValueError: the entry is empty, has mismatched parens, or has a
            ``*`` anywhere other than as a trailing prefix-glob marker.
    """
    stripped = entry.strip()
    if not stripped:
        raise ValueError("allowed_tools entry is empty")

    # Form 3: ToolName(arg_pattern)
    if "(" in stripped:
        if not stripped.endswith(")"):
            raise ValueError(
                f"allowed_tools entry {entry!r}: unmatched '(' "
                "(expected 'ToolName(arg_pattern)')"
            )
        name, _, rest = stripped.partition("(")
        tool_name = name.strip()
        if not tool_name:
            raise ValueError(f"allowed_tools entry {entry!r}: empty tool name")
        arg_pattern = rest[:-1]  # strip the trailing ')'
        # Disallow prefix-globbing + arg-pattern combined. Claude Code
        # doesn't use that shape and it would be ambiguous.
        if tool_name.endswith("*"):
            raise ValueError(
                f"allowed_tools entry {entry!r}: prefix '*' cannot be "
                "combined with arg pattern"
            )
        return ToolPattern(
            raw=entry,
            tool_name=tool_name,
            arg_pattern=arg_pattern,
            is_prefix=False,
        )

    # Form 2: prefix glob ('foo*'). Only trailing '*' allowed.
    if "*" in stripped:
        if not stripped.endswith("*"):
            raise ValueError(
                f"allowed_tools entry {entry!r}: '*' only allowed as a "
                "trailing prefix marker (e.g. 'mcp__*')"
            )
        prefix = stripped[:-1]
        # No inner '*' (trailing already stripped — if there's still one,
        # it was in the middle).
        if "*" in prefix:
            raise ValueError(
                f"allowed_tools entry {entry!r}: '*' only allowed once, "
                "as a trailing prefix marker"
            )
        return ToolPattern(
            raw=entry,
            tool_name=prefix,
            arg_pattern=None,
            is_prefix=True,
        )

    # Form 1: bare name.
    return ToolPattern(
        raw=entry,
        tool_name=stripped,
        arg_pattern=None,
        is_prefix=False,
    )


def matches(pattern: ToolPattern, tool_name: str, tool_args: dict) -> bool:
    """True if a tool-call ``(tool_name, tool_args)`` is allowed by ``pattern``.

    Rules:

    * **Prefix pattern** (``pattern.is_prefix``): match if
      ``tool_name.startswith(pattern.tool_name)``. (``fnmatch`` with
      pattern ``"mcp__*"`` would also work but ``startswith`` is cheaper
      and semantically identical for a single trailing star.)
    * **Arg pattern** (``pattern.arg_pattern is not None``): match only
      if the tool name is exactly ``pattern.tool_name`` AND the tool's
      first-arg field (per ``_FIRST_ARG_FIELD``) exists as a string AND
      ``fnmatch.fnmatchcase`` accepts it. Tools not in the first-arg map
      fail closed.
    * **Bare** (no arg pattern, not a prefix): exact tool-name match.
    """
    if pattern.is_prefix:
        return tool_name.startswith(pattern.tool_name)

    if pattern.arg_pattern is not None:
        if tool_name != pattern.tool_name:
            return False
        field = _FIRST_ARG_FIELD.get(tool_name)
        if field is None:
            # Fail closed: we don't know which arg to match against.
            return False
        value = tool_args.get(field)
        if not isinstance(value, str):
            return False
        # Claude Code's convention for Bash args uses ``prefix:*`` to mean
        # "the command starts with ``prefix``" — e.g. ``Bash(gh issue view:*)``
        # matches ``gh issue view 123``. ``fnmatch`` on its own would parse
        # the literal ``:`` — so we special-case this shape: if the arg
        # pattern ends in ``:*``, compare the command's prefix against the
        # text before ``:*``. Otherwise fall through to plain fnmatch for
        # arbitrary globs (e.g. ``Read(/Users/*)``).
        if pattern.arg_pattern.endswith(":*"):
            prefix = pattern.arg_pattern[:-2]
            return value.startswith(prefix)
        return fnmatch.fnmatchcase(value, pattern.arg_pattern)

    # Bare name.
    return tool_name == pattern.tool_name


__all__ = ["ToolPattern", "parse", "matches"]
