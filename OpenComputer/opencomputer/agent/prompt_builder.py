"""
Prompt builder — Jinja2 templates + slot injection.

Loads `base.j2` and renders it with runtime variables (cwd, user_home,
time, available skills, etc). Keeps the prompt out of code and makes
customization trivial — users can edit the .j2 files.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from opencomputer.agent.memory import SkillMeta


@dataclass(frozen=True, slots=True)
class PromptContext:
    """Variables injected into prompt templates."""

    cwd: str = ""
    user_home: str = ""
    now: str = ""
    skills: list[SkillMeta] | None = None


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
        template: str = "base.j2",
    ) -> str:
        ctx = PromptContext(
            cwd=os.getcwd(),
            user_home=str(Path.home()),
            now=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            skills=skills or [],
        )
        tpl = self.env.get_template(template)
        return tpl.render(
            cwd=ctx.cwd,
            user_home=ctx.user_home,
            now=ctx.now,
            skills=ctx.skills,
        )


__all__ = ["PromptBuilder", "PromptContext"]
