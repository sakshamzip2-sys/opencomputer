"""User-authored markdown slash commands (best-of-three port, Recipe 1).

Claude Code's highest-leverage plugin feature: drop a ``.md`` file in a
commands directory and it becomes a ``/slash`` command — no Python, no
restart. The file's body is the prompt template; optional YAML
frontmatter supplies ``description`` / ``args_hint`` / ``category`` /
``model_override`` / ``tools``.

Three directories are scanned, lowest precedence first:

1. ``<global_root>/commands/*.md``        — global user commands
2. ``<profile_home>/commands/*.md``       — per-profile user commands
3. ``<project_cwd>/.opencomputer/commands/*.md`` — project commands
   (opt-in: only scanned when ``project_cwd`` is passed)

Conflict policy (port plan R1.6): project > per-profile > global, and a
markdown command may shadow a built-in. Every override is logged at
WARNING so the user always sees a shadow.

This module is deliberately free of any ``cli_ui`` import — it is pure
discovery + rendering. Wiring discovered commands into the slash
registry lives in :mod:`opencomputer.cli_ui.slash_handlers`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from opencomputer.profiles import get_default_root

log = logging.getLogger(__name__)

#: A markdown command body larger than this is skipped — a prompt
#: template that big is almost certainly a mistake, and it would blow
#: the context budget if injected.
MAX_BODY_BYTES = 16 * 1024

#: Slash command names are lowercase, start alphanumeric, and may
#: contain ``-`` / ``_``. A file whose stem fails this is skipped so a
#: stray ``My Notes.md`` never becomes an un-typeable command.
_VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: The substitution token replaced with the user's ``/cmd <args>`` args.
_ARGS_TOKEN = "{{args}}"


@dataclass(frozen=True)
class MarkdownCommand:
    """One discovered markdown command.

    ``body`` is the raw prompt template (frontmatter stripped). It is
    turned into the actual user message by :func:`render_command_body`.
    """

    name: str
    body: str
    source_path: Path
    description: str = ""
    args_hint: str = ""
    category: str = "custom"
    model_override: str | None = None
    tools: tuple[str, ...] = field(default_factory=tuple)


def _coerce_tools(raw: object) -> tuple[str, ...]:
    """Frontmatter ``tools`` may be a YAML list or a comma string."""
    if isinstance(raw, (list, tuple)):
        return tuple(str(t).strip() for t in raw if str(t).strip())
    if isinstance(raw, str):
        return tuple(t.strip() for t in raw.split(",") if t.strip())
    return ()


def _parse_md_file(path: Path) -> MarkdownCommand | None:
    """Parse one ``.md`` file into a :class:`MarkdownCommand`.

    Returns ``None`` (and logs WARNING) for any reason the file can't
    become a command — unreadable, oversized, invalid name, or malformed
    frontmatter. A bad file must never abort discovery of its siblings.
    """
    name = path.stem.lower()
    if not _VALID_NAME.match(name):
        log.warning(
            "markdown command %s skipped: invalid command name %r "
            "(allowed: lowercase a-z0-9 plus - and _)",
            path, path.stem,
        )
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        log.warning("markdown command %s skipped: unreadable (%s)", path, exc)
        return None
    if len(raw) > MAX_BODY_BYTES:
        log.warning(
            "markdown command %s skipped: %d bytes exceeds %d-byte cap",
            path, len(raw), MAX_BODY_BYTES,
        )
        return None
    try:
        import frontmatter

        post = frontmatter.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        # frontmatter raises a variety of YAML errors (plus UnicodeError
        # on non-utf8); treat any parse failure as "skip this file" —
        # never wedge boot on one bad file.
        log.warning(
            "markdown command %s skipped: malformed frontmatter (%s)",
            path, exc,
        )
        return None
    meta = post.metadata or {}
    return MarkdownCommand(
        name=name,
        body=post.content,
        source_path=path,
        description=str(meta.get("description", "")).strip(),
        args_hint=str(meta.get("args_hint", "")).strip(),
        category=str(meta.get("category", "custom")).strip() or "custom",
        model_override=(
            str(meta["model_override"]).strip()
            if meta.get("model_override")
            else None
        ),
        tools=_coerce_tools(meta.get("tools")),
    )


def _scan_dir(directory: Path) -> list[MarkdownCommand]:
    """Parse every ``*.md`` in one directory (non-recursive)."""
    if not directory.is_dir():
        return []
    found: list[MarkdownCommand] = []
    for md in sorted(directory.glob("*.md")):
        if not md.is_file():
            continue
        cmd = _parse_md_file(md)
        if cmd is not None:
            found.append(cmd)
    log.debug("markdown commands: %d found in %s", len(found), directory)
    return found


def discover_markdown_commands(
    profile_home: Path,
    *,
    global_root: Path | None = None,
    project_cwd: Path | None = None,
) -> list[MarkdownCommand]:
    """Discover all markdown commands, conflict policy applied.

    ``global_root`` defaults to :func:`get_default_root` (``~/.opencomputer``);
    it is a parameter so tests can isolate it. ``project_cwd`` is opt-in —
    the caller only passes it when project commands are enabled.

    Later tiers override earlier ones by command name; each override is
    logged at WARNING. When the per-profile directory resolves to the
    same path as the global directory (the ``default`` profile, whose
    home *is* the root) it is scanned only once.
    """
    if global_root is None:
        global_root = get_default_root()

    # Directories lowest precedence first — a later tier's command name
    # overrides an earlier one.
    candidates: list[Path] = [
        global_root / "commands",
        profile_home / "commands",
    ]
    if project_cwd is not None:
        candidates.append(project_cwd / ".opencomputer" / "commands")

    # Dedup directories by resolved path so the default profile (whose
    # home == root) does not scan one dir under two tiers.
    seen_dirs: set[Path] = set()
    merged: dict[str, MarkdownCommand] = {}
    for directory in candidates:
        resolved = directory.expanduser()
        try:
            key = resolved.resolve()
        except OSError:
            key = resolved
        if key in seen_dirs:
            continue
        seen_dirs.add(key)
        for cmd in _scan_dir(resolved):
            if cmd.name in merged:
                log.warning(
                    "markdown command /%s from %s shadows %s",
                    cmd.name, cmd.source_path, merged[cmd.name].source_path,
                )
            merged[cmd.name] = cmd
    return sorted(merged.values(), key=lambda c: c.name)


def render_command_body(cmd: MarkdownCommand, args: str) -> str:
    """Turn a command + its invocation args into the user message.

    If the body contains ``{{args}}`` the token is substituted (with the
    empty string when no args were given). If there is no placeholder
    and the user supplied args, the args are appended so they are never
    silently dropped.
    """
    args = args.strip()
    if _ARGS_TOKEN in cmd.body:
        return cmd.body.replace(_ARGS_TOKEN, args)
    if args:
        return f"{cmd.body}\n\n{args}"
    return cmd.body
