"""Path-glob rules — read ``.opencomputer/rules/*.md`` and match by path.

v1.1 plan-2 M7.1 (2026-05-09). Operators drop short markdown files
into a ``rules/`` directory and the agent picks them up automatically
when it edits files matching the rule's glob set. Mirrors Cursor's
``.cursorrules`` and Continue's ``.continuerules`` patterns but uses
per-file frontmatter for path scoping instead of a single root file.

Two discovery roots, in priority order:

1. **Workspace** — ``./.opencomputer/rules/*.md`` next to the user's
   project files. Rules here win when a name collision occurs because
   project-local guidance is more specific than profile-level defaults.
2. **Profile** — ``~/.opencomputer/<profile>/rules/*.md``. Common rules
   the user wants applied across every project.

The loader is pure: it parses files into :class:`Rule` instances. The
matcher is pure: it filters loaded rules by ``fnmatch`` against tool-
call paths. The system-prompt block formatter is pure: it renders the
matched rules as a ``[Active Rules]`` markdown chunk for injection.

Wiring into the loop happens via :class:`PathGlobRulesProvider`
(implements :class:`DynamicInjectionProvider`) — when registered on
the InjectionEngine, the agent loop receives the rule block on every
turn that follows a path-touching tool call. See
``opencomputer/agent/path_rules_injection.py``.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger("opencomputer.agent.rules_loader")

#: Hard cap on a single rule body's contribution to the system prompt.
#: 4 KB is enough for ~1 page of guidance; anything larger gets
#: truncated with a marker. Stops a 50KB README from eating the prompt.
MAX_RULE_BODY_BYTES = 4 * 1024

#: Tools that take a ``path`` or ``paths`` argument. Used by
#: :func:`extract_paths_from_tool_call` to know which calls are
#: path-touching. Adding a new path-touching tool? Add its name here.
PATH_TOUCHING_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "Glob",
        "Grep",
        "NotebookEdit",
    }
)


def _glob_match(file_path: str, pattern: str) -> bool:
    """Glob match with proper ``**`` (zero-or-more-segments) semantics.

    Python 3.13 added :meth:`PurePosixPath.full_match` which handles
    ``**`` correctly. On 3.12 we translate the pattern to a regex
    that emulates the same semantics.

    Patterns without ``**`` go straight to ``fnmatch`` (cheaper than
    regex compilation for the common single-segment case).
    """
    if "**" not in pattern:
        return fnmatch.fnmatch(file_path, pattern)
    # 3.13+ has the ergonomic API
    full_match = getattr(PurePosixPath(file_path), "full_match", None)
    if callable(full_match):
        try:
            return full_match(pattern)
        except (TypeError, ValueError):
            pass  # fall through to manual regex
    return _glob_regex_match(file_path, pattern)


def _glob_regex_match(file_path: str, pattern: str) -> bool:
    """Translate a ``**``-aware glob to regex and match.

    Used as the 3.12 fallback. The translator handles:

    * ``**/`` at start, middle, or end → zero-or-more path segments.
    * ``**`` between separators → ``.*`` (matches everything incl ``/``).
    * ``*`` and ``?`` and ``[seq]`` per fnmatch.
    """
    import re

    parts: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # `**` consumes any number of path segments
                parts.append(".*")
                i += 2
                # Optional trailing `/` is part of the `**/` segment shape
                if i < n and pattern[i] == "/":
                    i += 1
            else:
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i)
            if j == -1:
                parts.append(re.escape(c))
                i += 1
            else:
                parts.append(pattern[i : j + 1])
                i = j + 1
        else:
            parts.append(re.escape(c))
            i += 1
    regex = "^" + "".join(parts) + "$"
    return bool(re.match(regex, file_path))


@dataclass(frozen=True)
class Rule:
    """One ``rules/*.md`` file parsed into structured form.

    ``paths`` is a tuple of fnmatch globs (e.g. ``("**/*.py", "src/**/*.tsx")``).
    Empty tuple → the rule never matches; logged at WARNING when loaded.

    ``priority`` controls ordering when multiple rules match the same
    file. Lower priority renders FIRST in the injected block — same
    convention as :class:`DynamicInjectionProvider`. Default 100
    matches the InjectionEngine default so unannotated rules sort
    among the unannotated providers naturally.
    """

    name: str
    paths: tuple[str, ...]
    priority: int
    body: str
    source: Path = field(default_factory=lambda: Path())

    def matches(self, file_path: str) -> bool:
        """Return True if any of ``self.paths`` matches ``file_path``.

        Glob semantics (from most specific to most permissive):

        * ``**`` matches **zero or more** path segments (so
          ``src/**/*.py`` matches both ``src/foo.py`` and
          ``src/sub/foo.py``). On Python 3.13+ this uses
          ``PurePosixPath.full_match``; on 3.12 it falls back to a
          regex translator that emulates the same semantics.
        * ``*`` matches any chars within a single segment (no slash).
        * ``?`` matches one char.
        * ``[seq]`` is a character class.

        Path separators are always ``/`` regardless of platform — rule
        files are version-controlled and should match POSIX-style.
        Plain ``fnmatch.fnmatch`` is used as the very last fallback
        for patterns without ``**``.
        """
        return any(_glob_match(file_path, pattern) for pattern in self.paths)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter dict, body string).

    Uses the same ``python-frontmatter`` library the SkillTool uses so
    we don't introduce a second YAML dialect. Returns
    (``{}, full_text``) when the file has no frontmatter — the body is
    still usable as a generic rule.
    """
    try:
        import frontmatter
    except ImportError:  # pragma: no cover — defensive; frontmatter is a runtime dep
        return ({}, text)
    post = frontmatter.loads(text)
    return (dict(post.metadata), post.content)


def _parse_one(path: Path) -> Rule | None:
    """Read + parse one rule file. Returns ``None`` on any error.

    Errors at the file-system or YAML-parse layer are logged and
    swallowed so one malformed rule can't disable the whole rules
    surface for the agent.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("rules: cannot read %s: %s", path, exc)
        return None
    try:
        meta, body = _parse_frontmatter(text)
    except Exception as exc:  # noqa: BLE001 — frontmatter raises plain Exception
        logger.warning("rules: malformed frontmatter in %s: %s", path, exc)
        return None
    raw_paths = meta.get("paths", [])
    if isinstance(raw_paths, str):
        raw_paths = [raw_paths]
    if not isinstance(raw_paths, list):
        logger.warning(
            "rules: %s: 'paths' must be a string or list, got %s",
            path,
            type(raw_paths).__name__,
        )
        return None
    paths = tuple(str(p) for p in raw_paths if isinstance(p, (str, int, float)))
    if not paths:
        logger.warning("rules: %s has no 'paths' globs; rule will never match", path)
    raw_priority = meta.get("priority", 100)
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        logger.warning(
            "rules: %s: 'priority' must be int, got %r; defaulting to 100",
            path,
            raw_priority,
        )
        priority = 100
    body = body.strip()
    if len(body.encode("utf-8")) > MAX_RULE_BODY_BYTES:
        body = (
            body.encode("utf-8")[:MAX_RULE_BODY_BYTES].decode("utf-8", "ignore")
            + "\n\n[...rule body truncated; max "
            f"{MAX_RULE_BODY_BYTES} bytes]"
        )
    return Rule(
        name=path.stem,
        paths=paths,
        priority=priority,
        body=body,
        source=path,
    )


def load_rules(rules_dir: Path) -> list[Rule]:
    """Load every ``*.md`` from ``rules_dir`` into :class:`Rule` instances.

    Sort order is ``(priority asc, name asc)`` so callers can rely on
    a deterministic ordering when two rules match the same file.

    Missing dir → returns ``[]`` (no rules; not an error).
    """
    if not rules_dir.exists() or not rules_dir.is_dir():
        return []
    out: list[Rule] = []
    for path in sorted(rules_dir.glob("*.md")):
        rule = _parse_one(path)
        if rule is not None:
            out.append(rule)
    out.sort(key=lambda r: (r.priority, r.name))
    return out


def merged_rules(workspace_dir: Path, profile_dir: Path) -> list[Rule]:
    """Load rules from workspace + profile; workspace shadows profile by name.

    The shadow rule is workspace-takes-precedence — if both
    ``./.opencomputer/rules/python.md`` and
    ``~/.opencomputer/<profile>/rules/python.md`` exist, only the
    workspace one ends up in the merged list. This lets a user
    override a global rule per-project without editing the global.
    """
    workspace = load_rules(workspace_dir)
    profile = load_rules(profile_dir)
    by_name: dict[str, Rule] = {r.name: r for r in profile}
    for rule in workspace:
        by_name[rule.name] = rule  # workspace overrides
    out = list(by_name.values())
    out.sort(key=lambda r: (r.priority, r.name))
    return out


def active_rules_for(rules: list[Rule], paths: list[str]) -> list[Rule]:
    """Filter ``rules`` to those matching at least one entry in ``paths``.

    Preserves the input ordering (already sorted by load_rules /
    merged_rules) so the rendered block is deterministic.
    """
    if not paths or not rules:
        return []
    out: list[Rule] = []
    for rule in rules:
        if any(rule.matches(p) for p in paths):
            out.append(rule)
    return out


def format_rules_block(rules: list[Rule]) -> str:
    """Render matched rules as a system-prompt addendum.

    Empty input → empty string (the InjectionEngine drops empty
    contributions). Single rule → a small ``[Active Rules]`` block.
    Multiple rules → one block with all rules separated by ``---``.

    The block is intentionally short and labelled so a model that
    treats system-prompt addenda differently from the base prompt
    (Anthropic's prefix-cache, OpenAI's developer messages) can find
    + isolate the active-rules section easily.
    """
    if not rules:
        return ""
    parts = ["[Active Rules]"]
    for rule in rules:
        header = f"### {rule.name}"
        if rule.paths:
            header += f"  ({', '.join(rule.paths)})"
        parts.append(header)
        if rule.body:
            parts.append(rule.body)
    return "\n\n".join(parts)


def extract_paths_from_tool_call(name: str, arguments: dict[str, Any]) -> list[str]:
    """Pull file paths out of a tool call's arguments dict.

    Looks for ``path`` / ``paths`` / ``file_path`` / ``file_paths``
    keys (covers Read / Edit / Write / MultiEdit / Glob / Grep
    conventions). Returns an empty list for non-path-touching tools
    so the caller can branch cheaply.
    """
    if name not in PATH_TOUCHING_TOOLS:
        return []
    out: list[str] = []
    # Single-path keys
    for key in ("path", "file_path", "filepath", "filename"):
        v = arguments.get(key)
        if isinstance(v, str) and v:
            out.append(v)
    # Multi-path keys (MultiEdit + Glob style)
    for key in ("paths", "file_paths", "files"):
        v = arguments.get(key)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item:
                    out.append(item)
        elif isinstance(v, dict):
            # Edit/MultiEdit spec: {"edits": [{"file_path": ...}]} — handled
            # below by the nested edits scan.
            pass
    # MultiEdit-shape: arguments["edits"] = [{"file_path": "...", ...}, ...]
    edits = arguments.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                fp = entry.get("file_path") or entry.get("path")
                if isinstance(fp, str) and fp:
                    out.append(fp)
    # Grep/Glob "pattern" + "path" — pattern doesn't count as a target file
    return out


__all__ = [
    "MAX_RULE_BODY_BYTES",
    "PATH_TOUCHING_TOOLS",
    "Rule",
    "active_rules_for",
    "extract_paths_from_tool_call",
    "format_rules_block",
    "load_rules",
    "merged_rules",
]
