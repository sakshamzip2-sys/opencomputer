"""SkillActivationInjectionProvider — auto-load matching SKILL.md into prompt.

Scans the user's last message for keyword overlap with each bundled skill's
frontmatter description. If a skill matches strongly (>=2 shared tokens), its
full body is injected into this turn's system prompt so the agent picks up
the skill's instructions without the user having to invoke it explicitly.
"""

from __future__ import annotations

from pathlib import Path

from plugin_sdk.injection import DynamicInjectionProvider, InjectionContext
from skills.registry import discover, match_skill  # type: ignore[import-not-found]

_SKILLS_DIR = Path(__file__).resolve().parent


class SkillActivationInjectionProvider(DynamicInjectionProvider):
    priority = 80  # after modes, before free-form user content

    def __init__(self, skills_dir: Path | None = None):
        self._skills_dir = skills_dir or _SKILLS_DIR

    @property
    def provider_id(self) -> str:
        return "coding-harness:skill-activation"

    async def collect(self, ctx: InjectionContext) -> str | None:
        # Find the last user message's text.
        last_user_text = ""
        for msg in reversed(ctx.messages or ()):
            role = getattr(msg, "role", None)
            if role is None:
                continue
            if getattr(role, "value", role) == "user":
                last_user_text = getattr(msg, "content", "") or ""
                break
        if not last_user_text:
            return None

        entries = discover(self._skills_dir)
        match = match_skill(last_user_text, entries)
        if match is None:
            return None

        try:
            body = match.path.read_text(encoding="utf-8")
        except OSError:
            return None

        return (
            f"## Activated skill: {match.name}\n\n"
            f"(Auto-activated because the user message matched "
            f"the skill's description.)\n\n"
            f"{body}\n"
        )


__all__ = ["SkillActivationInjectionProvider"]
