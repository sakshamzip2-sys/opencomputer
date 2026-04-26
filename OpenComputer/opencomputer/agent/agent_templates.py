"""
III.5 — Filesystem-based subagent template discovery.

Mirrors Claude Code's subagent-definition layout:
``sources/claude-code/plugins/<plugin>/agents/*.md`` — a Markdown file with
YAML frontmatter whose body is the subagent's system prompt. Example:

    ---
    name: code-reviewer
    description: Reviews code for bugs, logic errors, ...
    tools: Glob, Grep, LS, Read, Bash
    model: sonnet
    ---

    You are an expert code reviewer ...

Discovery runs in three tiers with profile > plugin > bundled precedence
(same ordering as skills: later tiers override earlier names). A malformed
``.md`` file is logged and skipped rather than raised, so one broken
template never takes down the whole registry.

At CLI startup, ``DelegateTool.set_templates(discover_agents(...))``
populates the class-level template map; the tool's ``agent`` parameter
looks up a name and applies its ``system_prompt`` + ``tools`` allowlist +
``model`` override to the spawned subagent.

Public surface:
  * :class:`AgentTemplate` — frozen dataclass carrying a parsed template.
  * :func:`parse_agent_template` — parse one ``.md`` file.
  * :func:`discover_agents` — scan three tiers, return ``{name: template}``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import frontmatter

_log = logging.getLogger(__name__)


#: Valid values for :attr:`AgentTemplate.source`. Mirrors I.7's activation-
#: source convention so CLI surfaces (``opencomputer agents list``) can
#: group entries by provenance.
_VALID_SOURCES: frozenset[str] = frozenset({"bundled", "profile", "plugin", "user"})


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    """A parsed subagent template loaded from a ``.md`` file.

    Matches the shape of Claude Code's agent frontmatter (see
    ``sources/claude-code/plugins/feature-dev/agents/code-reviewer.md``)
    adapted to OpenComputer's tool naming convention.

    Attributes
    ----------
    name:
        Template identifier — the key used in ``DelegateTool`` lookups.
        Required. Sourced from frontmatter ``name``.
    description:
        Short human-readable summary of what the subagent does. Required.
        Sourced from frontmatter ``description``. Surfaced by
        ``opencomputer agents list``.
    tools:
        Tool-name allowlist applied to the spawned subagent. Empty tuple
        means "unrestricted inherit" (no allowlist filtering — the child
        loop sees the parent's full tool set, existing behavior). A non-
        empty tuple is converted to a frozenset at DelegateTool dispatch
        time and passed through the III.1 ``allowed_tools`` pipeline.
    model:
        Optional model override. ``None`` means "use the parent's configured
        model". Forwarded to the child loop at spawn time (reserved — not
        yet consumed; threaded through for forward compatibility with the
        parent's future per-delegation model-override work).
    system_prompt:
        The body of the ``.md`` file after the frontmatter block. Required.
        Used verbatim as the child loop's system prompt via
        ``run_conversation(system_override=...)`` — the template author is
        responsible for the full prompt (no declarative / skills / memory /
        SOUL injection on top).
    source_path:
        Absolute path the template was discovered from. Kept for
        diagnostics and ``opencomputer agents list`` display.
    source:
        One of ``"bundled"``, ``"profile"``, ``"plugin"``, ``"user"``.
        Tracks where the template came from, analogous to I.7's
        activation-source bookkeeping.
    """

    name: str
    description: str
    tools: tuple[str, ...]
    model: str | None
    system_prompt: str
    source_path: Path
    source: str

    def __post_init__(self) -> None:
        # Defensive validation — ``parse_agent_template`` already enforces
        # these, but construction via ``replace()`` / direct init should
        # still catch misuse. Use object.__setattr__ because the dataclass
        # is frozen (this is the canonical pattern for frozen post-init
        # normalization; we're only asserting here, not mutating).
        if not self.name:
            raise ValueError("AgentTemplate.name must be non-empty")
        if not self.description:
            raise ValueError("AgentTemplate.description must be non-empty")
        if self.source not in _VALID_SOURCES:
            raise ValueError(
                f"AgentTemplate.source must be one of {sorted(_VALID_SOURCES)}, "
                f"got {self.source!r}"
            )


def parse_agent_template(path: Path, source: str) -> AgentTemplate:
    """Parse one ``.md`` file into an :class:`AgentTemplate`.

    Parameters
    ----------
    path:
        Absolute path to the ``.md`` file.
    source:
        Provenance tier — one of ``"bundled"``, ``"profile"``, ``"plugin"``,
        ``"user"``. Raises ``ValueError`` for any other value.

    Returns
    -------
    AgentTemplate
        A frozen dataclass with frontmatter fields populated.

    Raises
    ------
    ValueError
        If the file does not start with ``---`` (no frontmatter), if the
        ``name`` / ``description`` fields are missing or empty, or if
        ``source`` is not a recognized tier.
    """
    if source not in _VALID_SOURCES:
        raise ValueError(
            f"source must be one of {sorted(_VALID_SOURCES)}, got {source!r}"
        )

    raw = path.read_text(encoding="utf-8")
    # The Claude Code frontmatter contract is "must open with ``---``".
    # ``python-frontmatter`` returns an empty metadata dict for files that
    # don't, which would silently mask a bug — explicitly reject here.
    if not raw.lstrip().startswith("---"):
        raise ValueError(f"missing frontmatter in {path}")

    post = frontmatter.loads(raw)
    meta = post.metadata

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"frontmatter 'name' is required in {path}")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"frontmatter 'description' is required in {path}")

    # ``tools:`` is documented in Claude Code as a comma-separated string
    # (``Glob, Grep, LS, Read, ...``). Be lenient: if the YAML parser
    # already returned a list (valid YAML), accept that too.
    tools_raw = meta.get("tools")
    tools: tuple[str, ...]
    if tools_raw is None or tools_raw == "":
        tools = ()
    elif isinstance(tools_raw, str):
        tools = tuple(s.strip() for s in tools_raw.split(",") if s.strip())
    elif isinstance(tools_raw, list | tuple):
        tools = tuple(str(s).strip() for s in tools_raw if str(s).strip())
    else:
        raise ValueError(
            f"frontmatter 'tools' must be a string or list in {path}, "
            f"got {type(tools_raw).__name__}"
        )

    model_raw = meta.get("model")
    model: str | None
    if model_raw is None or model_raw == "":
        model = None
    elif isinstance(model_raw, str):
        model = model_raw.strip() or None
    else:
        raise ValueError(
            f"frontmatter 'model' must be a string in {path}, "
            f"got {type(model_raw).__name__}"
        )

    system_prompt = post.content.strip()

    return AgentTemplate(
        name=name.strip(),
        description=description.strip(),
        tools=tools,
        model=model,
        system_prompt=system_prompt,
        source_path=path,
        source=source,
    )


def _scan_dir(dir_path: Path, source: str) -> list[AgentTemplate]:
    """Scan ``dir_path/*.md`` non-recursively. Malformed files are logged + skipped.

    Returns the templates in directory-iteration order; callers merge
    these into a precedence-respecting dict.
    """
    if not dir_path.exists() or not dir_path.is_dir():
        return []
    out: list[AgentTemplate] = []
    # Sort for deterministic ordering — stabilizes the "last wins" rule
    # when two sibling files somehow resolve to the same ``name`` (rare,
    # but sorted order makes the winner predictable).
    for md in sorted(dir_path.glob("*.md")):
        if not md.is_file():
            continue
        try:
            out.append(parse_agent_template(md, source=source))
        except (ValueError, OSError) as e:
            # Malformed frontmatter, missing required fields, unreadable
            # file, etc. A single bad template must not take down the
            # whole registry — log + skip and keep going.
            _log.warning(
                "agent templates: skipping malformed file %s (%s)", md, e
            )
    return out


def _default_bundled_root() -> Path:
    """Bundled agent-template dir that ships with the package."""
    # ``opencomputer/agent/agent_templates.py`` → parent is ``agent/``,
    # grandparent is the package root (``opencomputer/``), and bundled
    # templates live under ``opencomputer/agents/``.
    return Path(__file__).resolve().parent.parent / "agents"


def _active_profile_root() -> Path | None:
    """Best-effort resolution of the active profile's root dir.

    Returns ``None`` if the config helper isn't importable in the current
    context (e.g. during package-import-time tests). The discovery path
    tolerates ``None`` by skipping that tier — bundled + plugin templates
    still work.
    """
    try:
        from opencomputer.agent.config import _home
    except Exception:  # noqa: BLE001
        return None
    try:
        return _home()
    except Exception:  # noqa: BLE001
        return None


def discover_agents(
    *,
    profile_root: Path | None = None,
    plugin_roots: Sequence[Path] = (),
    bundled_root: Path | None = None,
) -> dict[str, AgentTemplate]:
    """Discover subagent templates from the three tiers.

    Precedence (later tiers override earlier — same as the skills
    hierarchy in :meth:`MemoryManager.list_skills`):

    1. **Bundled** — ``{bundled_root}/*.md`` (defaults to
       ``opencomputer/agents/``, the package dir).
    2. **Plugin** — each ``{plugin_root}/agents/*.md`` in ``plugin_roots``.
    3. **Profile / user** — ``{profile_root}/home/agents/*.md`` (defaults
       to the active profile's home dir — ``_home()``).

    A malformed ``.md`` file anywhere in the chain is logged at WARNING
    and skipped; the returned dict contains every template that parsed
    cleanly.

    Parameters
    ----------
    profile_root:
        Profile directory. The scanner reads ``{profile_root}/home/agents/``.
        ``None`` (the default) resolves to the active profile's home dir
        or skips the tier if that can't be determined.
    plugin_roots:
        Sequence of plugin root directories. Each is scanned at
        ``{plugin_root}/agents/*.md``. Empty by default.
    bundled_root:
        Override for the bundled-templates dir. ``None`` (the default)
        uses the package's ``opencomputer/agents/`` dir.

    Returns
    -------
    dict[str, AgentTemplate]
        Map of ``name`` → template. Earlier tiers are inserted first,
        later tiers overwrite, so profile > plugin > bundled is the
        effective precedence.
    """
    out: dict[str, AgentTemplate] = {}

    # Tier 1: bundled.
    b_root = bundled_root if bundled_root is not None else _default_bundled_root()
    for tpl in _scan_dir(b_root, source="bundled"):
        out[tpl.name] = tpl

    # Tier 2: plugin (each plugin root's ``agents/`` subdir).
    for plugin_root in plugin_roots:
        for tpl in _scan_dir(Path(plugin_root) / "agents", source="plugin"):
            out[tpl.name] = tpl

    # Tier 3: profile / user — highest precedence.
    p_root = profile_root if profile_root is not None else _active_profile_root()
    if p_root is not None:
        for tpl in _scan_dir(Path(p_root) / "home" / "agents", source="profile"):
            out[tpl.name] = tpl

    return out


__all__ = [
    "AgentTemplate",
    "discover_agents",
    "parse_agent_template",
]
