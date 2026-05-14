"""Evaluate Hookify rules against an OC :class:`HookContext`.

Returns a HookDecision suitable for blocking or pass-through. Multiple
rules can match a single event; blocking rules win over warning rules,
and matching rules' messages are concatenated under their name headers.
"""

from __future__ import annotations

import re
from functools import lru_cache

from rule import Condition, Rule  # type: ignore[import-not-found]

from plugin_sdk.hooks import HookDecision


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern:
    """Cache compiled regexes to avoid recompiling on every tool call."""
    return re.compile(pattern, re.IGNORECASE)


class RuleEngine:
    """Evaluates rules against (tool_name, tool_input) pairs.

    Single-purpose object; no per-instance state. Kept as a class only
    so callers can mock or extend it from a plugin if they ever need
    custom evaluation semantics.
    """

    def evaluate(
        self,
        rules: list[Rule],
        *,
        tool_name: str,
        tool_input: dict | None,
        extra: dict | None = None,
    ) -> HookDecision:
        """Run every rule; return a single HookDecision.

        ``extra`` carries fields like ``user_prompt``, ``reason``,
        ``transcript_path`` for non-tool events. ``tool_input`` may be
        ``None`` (e.g. UserPromptSubmit, Stop) — handled gracefully.
        """
        ti = tool_input or {}
        ex = extra or {}
        blocking: list[Rule] = []
        warnings: list[Rule] = []
        for rule in rules:
            if not self._rule_matches(rule, tool_name, ti, ex):
                continue
            (blocking if rule.action == "block" else warnings).append(rule)

        if blocking:
            msg = "\n\n".join(
                f"**[{r.name}]**\n{r.message}" for r in blocking
            )
            return HookDecision(decision="block", reason=msg)
        if warnings:
            msg = "\n\n".join(
                f"**[{r.name}]**\n{r.message}" for r in warnings
            )
            # Anthropic's "warn" semantics: surface the message but allow
            # the operation. OC's HookDecision treats this as a pass with
            # a context-injected reason — the agent loop logs `reason`
            # for the user even on pass.
            return HookDecision(decision="pass", reason=msg)
        return HookDecision(decision="pass")

    def _rule_matches(
        self,
        rule: Rule,
        tool_name: str,
        tool_input: dict,
        extra: dict,
    ) -> bool:
        if rule.tool_matcher and not self._matches_tool(
            rule.tool_matcher, tool_name
        ):
            return False
        if not rule.conditions:
            return False
        return all(
            self._check_condition(c, tool_name, tool_input, extra)
            for c in rule.conditions
        )

    def _matches_tool(self, matcher: str, tool_name: str) -> bool:
        if matcher in ("*", ".*"):
            return True
        return tool_name in matcher.split("|")

    def _check_condition(
        self,
        cond: Condition,
        tool_name: str,
        tool_input: dict,
        extra: dict,
    ) -> bool:
        value = self._extract(cond.field, tool_name, tool_input, extra)
        if value is None:
            return False
        op = cond.operator
        pat = cond.pattern
        if op == "regex_match":
            try:
                return _compile(pat).search(value) is not None
            except re.error:
                return False
        if op == "contains":
            return pat in value
        if op == "not_contains":
            return pat not in value
        if op == "equals":
            return pat == value
        if op == "starts_with":
            return value.startswith(pat)
        if op == "ends_with":
            return value.endswith(pat)
        return False

    def _extract(
        self,
        field: str,
        tool_name: str,
        tool_input: dict,
        extra: dict,
    ) -> str | None:
        # Direct tool_input field
        if field in tool_input:
            v = tool_input[field]
            return v if isinstance(v, str) else str(v)
        # Tool-specific aliases
        if tool_name == "Bash" and field == "command":
            return str(tool_input.get("command", ""))
        if tool_name in ("Write", "Edit") and field in ("content", "new_text"):
            return str(
                tool_input.get("content")
                or tool_input.get("new_string", "")
                or ""
            )
        if tool_name == "MultiEdit" and field in ("content", "new_text"):
            edits = tool_input.get("edits") or []
            if isinstance(edits, list):
                return " ".join(
                    str((e or {}).get("new_string", "") or "")
                    for e in edits
                )
        # Non-tool event fields
        if field == "user_prompt":
            return str(extra.get("user_prompt", "") or "")
        if field == "reason":
            return str(extra.get("reason", "") or "")
        if field == "transcript":
            path = extra.get("transcript_path")
            if path:
                try:
                    from pathlib import Path

                    return Path(str(path)).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    return ""
        return None
