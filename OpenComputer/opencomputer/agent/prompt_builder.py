"""
Prompt builder — Jinja2 templates + slot injection.

Loads `base.j2` and renders it with runtime variables (cwd, user_home,
time, available skills, declarative memory, user profile). Keeps the
prompt out of code and makes customization trivial — users can edit the
.j2 files.

Declarative memory + user profile go into the FROZEN base prompt (not
per-turn injection) so Anthropic prefix cache stays hot across turns.
"""

from __future__ import annotations

import datetime
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opencomputer.user_model.store import UserModelStore

from jinja2 import Environment, FileSystemLoader, select_autoescape

from opencomputer.agent.memory import SkillMeta

_TRUNCATION_MARKER = "[earlier entries truncated]\n\n"

#: V3.A-T8 — per-file size cap for workspace context loader. Keeps the
#: prefix prompt bounded if a project ships a 10MB CLAUDE.md. Truncated
#: files get a marker so the agent knows what happened.
_WORKSPACE_FILE_CAP_BYTES = 100_000
_WORKSPACE_TRUNCATION_NOTE = "\n\n[truncated — file exceeded 100KB cap]\n"


def load_workspace_context(*, start: Path | None = None, max_depth: int = 5) -> str:
    """Find ``OPENCOMPUTER.md`` / ``CLAUDE.md`` / ``AGENTS.md`` from cwd or ancestors.

    Walks up from ``start`` (default cwd) up to ``max_depth`` levels.
    Returns concatenated content with file-tagged markers, or an empty
    string if none found.

    Per-file size is capped at ``_WORKSPACE_FILE_CAP_BYTES`` (100KB) to
    prevent a misconfigured workspace file from blowing the prompt
    budget; over-cap files are truncated with a visible marker so the
    agent can ask for the full file if needed.

    The ``seen_paths`` set deduplicates the same physical file across
    iterations (e.g. via symlink or repeated visit), but distinct files
    in different ancestors are all loaded — closer-to-cwd first, since
    they reflect more-specific project conventions.
    """
    if start is None:
        start = Path.cwd()
    start = start.resolve()

    # Files to check, in priority order. We collect ALL that exist, not
    # just the highest-priority one — multiple files may coexist (e.g.
    # both CLAUDE.md and AGENTS.md in the same repo).
    target_names = ("OPENCOMPUTER.md", "CLAUDE.md", "AGENTS.md")

    found: list[tuple[str, str]] = []
    seen_paths: set[Path] = set()

    current = start
    # ``range(max_depth)`` iterates exactly ``max_depth`` times — each
    # iteration inspects one directory level (start + max_depth-1
    # ancestors). The break-on-root guard handles filesystems shallower
    # than ``max_depth``.
    for _ in range(max_depth):
        for name in target_names:
            candidate = current / name
            if not candidate.is_file():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen_paths:
                continue
            try:
                content = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            seen_paths.add(resolved)
            if len(content) > _WORKSPACE_FILE_CAP_BYTES:
                content = content[:_WORKSPACE_FILE_CAP_BYTES] + _WORKSPACE_TRUNCATION_NOTE
            found.append((name, content))
        if current.parent == current:
            break  # filesystem root reached
        current = current.parent

    if not found:
        return ""

    parts: list[str] = []
    for name, content in found:
        parts.append(f"## {name}\n\n{content.strip()}\n")
    return "\n".join(parts)


def _truncate_from_top(text: str, limit: int) -> str:
    """Drop content from the TOP until under *limit* chars, prepending a marker.

    Recent entries are assumed to be at the bottom — that's where the agent
    appends new observations — so the top is what we discard first. If the
    text already fits, return unchanged.
    """
    if len(text) <= limit:
        return text
    # Make room for the marker itself.
    budget = limit - len(_TRUNCATION_MARKER)
    if budget <= 0:
        return _TRUNCATION_MARKER.rstrip()
    tail = text[-budget:]
    # Prefer cutting at a line boundary to avoid mid-word truncation.
    newline_idx = tail.find("\n")
    if newline_idx != -1:
        tail = tail[newline_idx + 1 :]
    return _TRUNCATION_MARKER + tail


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Variables injected into prompt templates."""

    cwd: str = ""
    user_home: str = ""
    now: str = ""
    skills: list[SkillMeta] | None = None
    memory: str = ""
    user_profile: str = ""
    #: Phase 14.F / C3 — per-profile personality from ``SOUL.md``. Empty
    #: means "no profile identity" and the section is omitted.
    soul: str = ""
    #: Layered Awareness MVP — pre-formatted top-K user-model facts built
    #: via :meth:`PromptBuilder.build_user_facts` from the F4 graph. Empty
    #: string means "no user-model knowledge yet" — base.j2 omits the
    #: section accordingly.
    user_facts: str = ""
    #: V3.A-T3 — operating-system label rendered into the system info block
    #: (e.g. ``"Darwin"`` / ``"Linux"`` / ``"Windows"``). Defaults to the
    #: live :func:`platform.system` value when ``PromptBuilder.build``
    #: constructs the context, but downstream callers may override.
    os_name: str = ""
    #: V3.A-T3 — workspace-context slot reserved for T8 (CLAUDE.md /
    #: OPENCOMPUTER.md / AGENTS.md aggregation). Defaults to ``""`` so
    #: ``base.j2`` omits the section until the loader is wired. Existing
    #: PromptContext consumers do not need to set this; the field has a
    #: safe default.
    workspace_context: str = ""
    #: V2.C-T5 — persona auto-classifier output (system_prompt_overlay
    #: from the matched persona YAML). Empty string means "no persona
    #: detected" — ``base.j2`` omits the "Active persona" section
    #: accordingly. Computed once per session in the same lane as
    #: ``user_facts`` to keep the prefix-cache invariant intact.
    persona_overlay: str = ""
    #: Path A.1 (2026-04-27) — the ID of the persona whose overlay is
    #: above. ``base.j2`` uses this for persona-specific Jinja
    #: conditionals (e.g. omitting "no filler / no hedging" rules under
    #: the companion persona). Empty string means "no active persona" —
    #: equivalent to the legacy default.
    active_persona_id: str = ""
    #: V3.A-T3 — runtime mode flags that drive Jinja conditionals in
    #: ``base.j2``. ``plan_mode`` mirrors ``runtime.plan_mode`` and tells
    #: the agent that destructive tools are blocked. ``yolo_mode`` mirrors
    #: ``runtime.yolo_mode`` and warns the agent that the safety gate is
    #: lowered. Both default to ``False`` so unmodified callers render the
    #: standard prompt (no plan/yolo bumper sections).
    plan_mode: bool = False
    yolo_mode: bool = False


class PromptBuilder:
    """Renders system prompts from Jinja2 templates."""

    def __init__(self, templates_dir: Path | None = None) -> None:
        if templates_dir is None:
            templates_dir = Path(__file__).parent / "prompts"
        self.env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(disabled_extensions=("j2",)),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def build(
        self,
        *,
        skills: list[SkillMeta] | None = None,
        declarative_memory: str = "",
        user_profile: str = "",
        soul: str = "",
        user_facts: str = "",
        memory_char_limit: int = 4000,
        user_char_limit: int = 2000,
        template: str = "base.j2",
        workspace_context: str = "",
        plan_mode: bool = False,
        yolo_mode: bool = False,
        persona_overlay: str = "",
        active_persona_id: str = "",
    ) -> str:
        memory = _truncate_from_top(declarative_memory, memory_char_limit)
        profile = _truncate_from_top(user_profile, user_char_limit)
        ctx = PromptContext(
            cwd=os.getcwd(),
            user_home=str(Path.home()),
            now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            skills=skills or [],
            memory=memory,
            user_profile=profile,
            soul=soul,
            user_facts=user_facts,
            os_name=platform.system() or "",
            workspace_context=workspace_context,
            plan_mode=plan_mode,
            yolo_mode=yolo_mode,
            persona_overlay=persona_overlay,
            active_persona_id=active_persona_id,
        )
        tpl = self.env.get_template(template)
        return tpl.render(
            cwd=ctx.cwd,
            user_home=ctx.user_home,
            now=ctx.now,
            skills=ctx.skills,
            memory=ctx.memory,
            user_profile=ctx.user_profile,
            soul=ctx.soul,
            user_facts=ctx.user_facts,
            os_name=ctx.os_name,
            workspace_context=ctx.workspace_context,
            plan_mode=ctx.plan_mode,
            yolo_mode=ctx.yolo_mode,
            persona_overlay=ctx.persona_overlay,
            active_persona_id=ctx.active_persona_id,
        )

    def build_user_facts(
        self,
        *,
        store: UserModelStore | None = None,
        top_k: int = 20,
    ) -> str:
        """Return a pre-formatted top-K user-facts block, or empty string.

        Pulls Identity + Goal + Preference + Attribute nodes from the
        F4 user-model graph, sorted by kind priority then descending
        confidence. Truncates to ~80 chars per fact for prompt token
        economy. Returns ``""`` when the graph is empty so that
        ``base.j2`` can omit the section via ``{% if user_facts %}``.
        """
        from opencomputer.user_model.store import UserModelStore

        s = store if store is not None else UserModelStore()
        # Bumped from default 100 to 500 so a fresh bootstrap (which
        # may write 50-200 nodes) leaves headroom for ranking before
        # the top-K cut.
        nodes = s.list_nodes(
            kinds=("identity", "goal", "preference", "attribute"),
            limit=500,
        )
        # Rank: identity > goal > preference > attribute, then by confidence
        kind_order = {"identity": 0, "goal": 1, "preference": 2, "attribute": 3}
        nodes_ranked = sorted(
            nodes,
            key=lambda n: (kind_order.get(n.kind, 99), -n.confidence),
        )[:top_k]
        if not nodes_ranked:
            return ""
        lines = [f"- ({n.kind}) {n.value[:80]}" for n in nodes_ranked]
        return "\n".join(lines)

    async def build_with_memory(
        self,
        *,
        skills: list[SkillMeta] | None = None,
        declarative_memory: str = "",
        user_profile: str = "",
        soul: str = "",
        user_facts: str = "",
        memory_char_limit: int = 4000,
        user_char_limit: int = 2000,
        template: str = "base.j2",
        memory_bridge: Any = None,
        session_id: str | None = None,
        enable_ambient_blocks: bool = True,
        max_ambient_block_chars: int = 800,
        workspace_context: str = "",
        plan_mode: bool = False,
        yolo_mode: bool = False,
        persona_overlay: str = "",
        active_persona_id: str = "",
    ) -> str:
        """Async variant of build() that appends ambient memory blocks.

        PR-6 T2.1 — if ``enable_ambient_blocks`` is True and a
        ``memory_bridge`` is provided, calls
        ``memory_bridge.collect_system_prompt_blocks`` and appends the result
        under a ``## Memory context`` header. The sync ``build()`` signature
        is unchanged to preserve prefix-cache behaviour for callers that
        haven't opted in to T2.1 yet.

        The AgentLoop calls this variant when memory is wired in and
        ``config.memory.enable_ambient_blocks`` is True; callers that pass
        ``system_prompt_override`` bypass both ``build`` and this method.
        """
        base = self.build(
            skills=skills,
            declarative_memory=declarative_memory,
            user_profile=user_profile,
            soul=soul,
            user_facts=user_facts,
            memory_char_limit=memory_char_limit,
            user_char_limit=user_char_limit,
            template=template,
            workspace_context=workspace_context,
            plan_mode=plan_mode,
            yolo_mode=yolo_mode,
            persona_overlay=persona_overlay,
            active_persona_id=active_persona_id,
        )
        if not enable_ambient_blocks or memory_bridge is None:
            return base
        try:
            blocks = await memory_bridge.collect_system_prompt_blocks(
                session_id=session_id,
                max_per_block=max_ambient_block_chars,
            )
        except Exception:
            # Never break prompt construction over a memory error.
            return base
        if blocks:
            return base + "\n\n## Memory context\n\n" + blocks
        return base


__all__ = ["PromptBuilder", "PromptContext", "load_workspace_context"]
