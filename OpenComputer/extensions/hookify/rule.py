"""Rule + Condition dataclasses for the Hookify rule engine.

Mirrors Anthropic's hookify ``Rule`` / ``Condition`` shape so existing
hookify rule files (``.claude/hookify.<name>.local.md``) can be moved
to OC unchanged. The frontmatter format is identical; the only
difference is *where* the rules live (``~/.opencomputer/<profile>/
hookify/`` instead of ``.claude/``).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Condition:
    """One field/operator/pattern triple — the unit of rule matching."""

    field: str  # "command", "new_text", "file_path", "reason", "transcript", …
    operator: str  # "regex_match", "contains", "equals", "not_contains", …
    pattern: str

    @classmethod
    def from_dict(cls, data: dict) -> Condition:
        return cls(
            field=str(data.get("field", "")),
            operator=str(data.get("operator", "regex_match")),
            pattern=str(data.get("pattern", "")),
        )


@dataclass(frozen=True, slots=True)
class Rule:
    """One auto-loaded hookify rule.

    ``event`` maps to a Hookify-style event family (the OC plugin maps
    it to OC's :class:`plugin_sdk.hooks.HookEvent` at registration):

    - ``bash``    → fires for ``Bash`` PreToolUse
    - ``file``    → fires for ``Edit | Write | MultiEdit`` PreToolUse
    - ``stop``    → fires on ``Stop``
    - ``prompt``  → fires on ``UserPromptSubmit``
    - ``post``    → fires on ``PostToolUse`` (any tool)
    - ``all``     → fires on PreToolUse for any tool
    """

    name: str
    enabled: bool
    event: str
    conditions: tuple[Condition, ...] = field(default_factory=tuple)
    action: str = "warn"  # "warn" | "block"
    tool_matcher: str | None = None
    message: str = ""

    @classmethod
    def from_frontmatter(cls, fm: dict, body: str) -> Rule:
        # Build conditions from explicit list or legacy `pattern` shorthand.
        conds: list[Condition] = []
        if isinstance(fm.get("conditions"), list):
            for c in fm["conditions"]:
                if isinstance(c, dict):
                    conds.append(Condition.from_dict(c))

        simple_pattern = fm.get("pattern")
        if simple_pattern and not conds:
            event = str(fm.get("event", "all"))
            field_name = {
                "bash": "command",
                "file": "new_text",
                "post": "content",
                "stop": "reason",
                "prompt": "user_prompt",
            }.get(event, "content")
            conds.append(
                Condition(
                    field=field_name,
                    operator="regex_match",
                    pattern=str(simple_pattern),
                )
            )

        return cls(
            name=str(fm.get("name", "unnamed")),
            enabled=bool(fm.get("enabled", True)),
            event=str(fm.get("event", "all")),
            conditions=tuple(conds),
            action=str(fm.get("action", "warn")),
            tool_matcher=(
                str(fm["tool_matcher"]) if fm.get("tool_matcher") else None
            ),
            message=str(body or "").strip(),
        )
