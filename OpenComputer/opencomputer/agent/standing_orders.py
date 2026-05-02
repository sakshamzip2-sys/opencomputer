"""Standing Orders — parsed `## Program: <name>` blocks from AGENTS.md.

Each block grants the agent declarative authority for an autonomous
program. Block fields (all optional except Triggers):

  Scope:           paths/components the program owns
  Triggers:        cron expression, event name, or trigger description
  Approval Gates:  what requires human confirmation before action
  Escalation:      who to contact / how to bail out

A block runs from `## Program: <name>` to the next H2 heading (or EOF).
Field continuation: any line following a field that is indented or has
no `Key:` shape becomes part of that field's value.

Why a line-state-machine and not regex: Python's `re` does not support
the kind of multi-line negative lookahead (`(?!^## )` cross-line) that
the rev-1 plan assumed. A regex-based parser would have eaten adjacent
`## Program:` blocks as the first block's body — silent authority leak
where program "alpha" gets the scope of program "beta".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StandingOrder:
    name: str
    scope: str = ""
    triggers: str = ""
    approval_gates: str = ""
    escalation: str = ""
    #: All field key/value pairs, including unknown keys (forward compat).
    raw_fields: dict[str, str] = field(default_factory=dict)


# `## Program: <name>` — name allows letters, digits, hyphen, underscore.
_HEADER_RE = re.compile(r"^##\s+Program:\s+(?P<name>[\w\-]+)\s*$")
# Any other `##` heading terminates a block.
_OTHER_H2_RE = re.compile(r"^##\s+(?!Program:)")
# Field key: value — key must start with uppercase letter, then word/space chars.
_FIELD_RE = re.compile(r"^(?P<key>[A-Z][\w\s]*?):\s*(?P<val>.*)$")

# Fields that map onto explicit StandingOrder attributes (everything else
# stays in raw_fields only).
_FIELD_TO_ATTR = {
    "scope": "scope",
    "triggers": "triggers",
    "approval_gates": "approval_gates",
    "escalation": "escalation",
}


def parse_agents_md(text: str) -> list[StandingOrder]:
    """Parse AGENTS.md text and return all well-formed StandingOrder blocks.

    Blocks missing the `Triggers:` field are logged at WARNING and skipped
    (NOT raised) — malformed input must not crash the gateway.
    """
    if not text:
        return []

    out: list[StandingOrder] = []
    in_block = False
    current: StandingOrder | None = None
    cur_key: str | None = None
    cur_lines: list[str] = []

    def commit_field() -> None:
        nonlocal cur_key, cur_lines
        if current is None or cur_key is None:
            cur_key = None
            cur_lines = []
            return
        val = "\n".join(cur_lines).strip()
        norm = cur_key.strip().lower().replace(" ", "_")
        current.raw_fields[norm] = val
        if attr := _FIELD_TO_ATTR.get(norm):
            setattr(current, attr, val)
        cur_key = None
        cur_lines = []

    def commit_block() -> None:
        nonlocal current, in_block
        if current is None:
            return
        commit_field()
        if current.triggers:
            out.append(current)
        else:
            logger.warning(
                "standing-order block %r missing 'Triggers' — skipped",
                current.name,
            )
        current = None
        in_block = False

    for line in text.splitlines():
        # Header opens a new block (and closes any previous one)
        if m := _HEADER_RE.match(line):
            commit_block()
            current = StandingOrder(name=m.group("name"))
            in_block = True
            continue

        # Any other H2 closes the current block
        if _OTHER_H2_RE.match(line):
            commit_block()
            continue

        if not in_block:
            continue

        # Field key: value (only when line starts in column 0 — indented
        # lines are continuations of the previous field)
        if not line.startswith((" ", "\t")):
            if fm := _FIELD_RE.match(line):
                commit_field()
                cur_key = fm.group("key")
                cur_lines = [fm.group("val")]
                continue

        # Continuation of current field
        if cur_key is not None:
            cur_lines.append(line)

    commit_block()
    return out


def parse_agents_md_file(path: str | Path) -> list[StandingOrder]:
    """Convenience: read a file and parse it. Missing file → empty list."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        return parse_agents_md(p.read_text(encoding="utf-8"))
    except OSError as e:
        logger.warning("could not read AGENTS.md at %s: %s", p, e)
        return []


def render_orders_for_system_context(orders: list[StandingOrder]) -> str:
    """Format parsed orders as a system-context block for the agent loop.

    Returns an empty string when there are no orders so the caller can
    cheaply concatenate without conditional logic.
    """
    if not orders:
        return ""
    parts = ["<standing-orders>"]
    for o in orders:
        parts.append(f"  Program: {o.name}")
        if o.scope:
            parts.append(f"    Scope: {o.scope}")
        if o.triggers:
            parts.append(f"    Triggers: {o.triggers}")
        if o.approval_gates:
            parts.append(f"    Approval Gates: {o.approval_gates}")
        if o.escalation:
            parts.append(f"    Escalation: {o.escalation}")
    parts.append("</standing-orders>")
    return "\n".join(parts)


__all__ = [
    "StandingOrder",
    "parse_agents_md",
    "parse_agents_md_file",
    "render_orders_for_system_context",
]
