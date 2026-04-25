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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from opencomputer.agent.memory import SkillMeta

_TRUNCATION_MARKER = "[earlier entries truncated]\n\n"


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
        memory_char_limit: int = 4000,
        user_char_limit: int = 2000,
        template: str = "base.j2",
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
        )


    async def build_with_memory(
        self,
        *,
        skills: list[SkillMeta] | None = None,
        declarative_memory: str = "",
        user_profile: str = "",
        soul: str = "",
        memory_char_limit: int = 4000,
        user_char_limit: int = 2000,
        template: str = "base.j2",
        memory_bridge: Any = None,
        session_id: str | None = None,
        enable_ambient_blocks: bool = True,
        max_ambient_block_chars: int = 800,
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
            memory_char_limit=memory_char_limit,
            user_char_limit=user_char_limit,
            template=template,
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


__all__ = ["PromptBuilder", "PromptContext"]
