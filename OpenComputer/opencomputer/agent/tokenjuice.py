"""Tokenjuice — deterministic tool-result compaction (OpenClaw parity).

Some tools dump huge, mostly-noise output that burns context for a
small signal: ``find /``, ``npm install``, ``git status`` after a
fresh clone. Tokenjuice trims those *after* the command runs but
*before* the result re-enters the conversation, so the model sees the
signal without paying tokens for the noise.

Three strategies:

* ``none``      — no transformation (the safe default).
* ``truncate``  — keep the first *head_lines* lines and the last
                  *tail_lines*; collapse the middle into a sentinel.
* ``summary``   — keep head/tail AND any line that looks like an
                  error/warning/traceback/path, so failure signals
                  survive even when most of the output is noise.

The rule of thumb borrowed from OpenClaw: never compact ``Read``,
``ReadFile`` or other "I want the bytes verbatim" tools. The
default config ships with those on the do-not-compact list.

Architecture:

* :class:`ToolCompactionRule`  — frozen per-tool config.
* :class:`TokenjuiceConfig`    — per-loop config, with a default
                                 rule and an explicit per-tool map.
* :func:`compact_tool_result`  — pure function: ``str → str``. Pure
                                 so it's trivially testable, and
                                 returns the input unchanged when
                                 the rule is ``none`` or the
                                 content is below the threshold.

The function never raises on bad input — corrupt UTF-8, gargantuan
strings, etc. — so a degenerate tool result can never break the
agent loop. Errors degrade to "return original" with a debug log.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "DEFAULT_DO_NOT_COMPACT",
    "TokenjuiceConfig",
    "ToolCompactionRule",
    "compact_tool_result",
]


_log = logging.getLogger("opencomputer.agent.tokenjuice")


Strategy = Literal["none", "truncate", "summary"]


# Tools that almost always want raw output. ``Read`` content is what
# the model edits next; trimming it would silently truncate code.
#
# This is a tuple (not a frozenset) so the containing
# :class:`TokenjuiceConfig` round-trips through YAML cleanly —
# PyYAML's SafeDumper has no representer for frozenset, and the
# default LoopConfig must be safely serialisable for routing /
# config-roundtrip tests.
DEFAULT_DO_NOT_COMPACT: tuple[str, ...] = (
    "Read",
    "ReadFile",
    "NotebookRead",
    "Skill",
    "PushNotification",
    "AskUserQuestion",
    "ExitPlanMode",
)


@dataclass(frozen=True, slots=True)
class ToolCompactionRule:
    """Per-tool compaction settings.

    Attributes:
        strategy:    which compaction strategy to apply.
        head_lines:  how many leading lines to preserve verbatim.
        tail_lines:  how many trailing lines to preserve verbatim.
        max_lines:   compaction is a no-op when the result has fewer
                     lines than this (cheap content stays cheap).
        max_chars:   hard upper bound — if the result exceeds this,
                     truncate even when the rule is ``none``. Defends
                     against pathological 50 MB outputs entering the
                     model at any cost.
        signal_patterns: regexes whose hits are preserved by the
                     ``summary`` strategy on top of head/tail.
    """

    strategy: Strategy = "none"
    head_lines: int = 80
    tail_lines: int = 80
    max_lines: int = 200
    max_chars: int = 200_000  # 200 KB hard ceiling — model context dignity
    signal_patterns: tuple[str, ...] = (
        r"\berror\b",
        r"\bwarning\b",
        r"\bfailed\b",
        r"traceback",
        r"exception",
        r"^\s*at\s",          # JS / Java stack frames
        r"FAIL[ED]?\b",
        r"\bfatal\b",
    )


@dataclass(frozen=True, slots=True)
class TokenjuiceConfig:
    """Loop-level compaction config.

    Attributes:
        enabled:        master kill-switch. ``False`` skips all work.
        default_rule:   applied to any tool not in *per_tool* and
                        not in *do_not_compact*.
        per_tool:       explicit per-tool overrides.
        do_not_compact: tool names that always pass through verbatim,
                        regardless of *default_rule*.
    """

    enabled: bool = False
    default_rule: ToolCompactionRule = field(default_factory=ToolCompactionRule)
    per_tool: dict[str, ToolCompactionRule] = field(default_factory=dict)
    do_not_compact: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_DO_NOT_COMPACT,
    )

    def rule_for(self, tool_name: str) -> ToolCompactionRule | None:
        """Return the rule that applies to *tool_name*, or ``None`` to skip."""
        if not self.enabled:
            return None
        if tool_name in self.do_not_compact:
            return None
        return self.per_tool.get(tool_name, self.default_rule)


def compact_tool_result(
    *,
    tool_name: str,
    content: str,
    config: TokenjuiceConfig,
) -> str:
    """Apply tokenjuice rules to *content*, returning the rewritten text.

    Returns the original ``content`` when:

    * tokenjuice is disabled,
    * the tool is on the do-not-compact list,
    * the rule's strategy is ``none``,
    * the result is shorter than the rule's threshold,
    * any error happens during compaction (defensive: a buggy compactor
      must never break the agent loop).
    """
    rule = config.rule_for(tool_name)
    if rule is None or rule.strategy == "none":
        return _apply_hard_ceiling(content, rule.max_chars if rule else None)
    if not isinstance(content, str):
        # Caller should never give us non-strings, but we degrade
        # gracefully rather than crash if they do.
        return content
    try:
        return _compact(content, rule)
    except Exception:  # noqa: BLE001 — defensive: never break the loop
        _log.warning(
            "tokenjuice: compaction crashed for %s — returning original",
            tool_name,
            exc_info=True,
        )
        return content


# ─── internals ────────────────────────────────────────────────────────


def _apply_hard_ceiling(content: str, max_chars: int | None) -> str:
    """Enforce the bytes-ceiling even when no compaction strategy applies.

    Pathological tool outputs (binary blobs, 100 MB log dumps) must not
    enter the model regardless of strategy. ``None`` ceiling = no cap
    (callers that explicitly pass ``rule=None`` are saying "leave it").
    """
    if max_chars is None or len(content) <= max_chars:
        return content
    head = content[: max_chars // 2]
    tail = content[-max_chars // 2 :]
    return (
        head
        + f"\n\n[... omitted {len(content) - max_chars} chars (max_chars cap) ...]\n\n"
        + tail
    )


def _compact(content: str, rule: ToolCompactionRule) -> str:
    """Run the rule's strategy. Pure; no I/O."""
    # Hard ceiling first — if the input is enormous we trim before
    # any line-based work to keep the cost bounded.
    content = _apply_hard_ceiling(content, rule.max_chars)
    lines = content.splitlines()
    if len(lines) <= rule.max_lines:
        # Below threshold — return original (preserves trailing newline).
        return content

    if rule.strategy == "truncate":
        return _truncate(lines, rule)
    if rule.strategy == "summary":
        return _summary(lines, rule)
    return content  # pragma: no cover — Literal narrows; guard for forward-compat


def _truncate(lines: list[str], rule: ToolCompactionRule) -> str:
    head = lines[: rule.head_lines]
    tail = lines[-rule.tail_lines :] if rule.tail_lines else []
    omitted = len(lines) - len(head) - len(tail)
    if omitted <= 0:
        return "\n".join(lines)
    return "\n".join(
        head
        + [f"\n[... omitted {omitted} lines (truncate) ...]\n"]
        + tail
    )


def _summary(lines: list[str], rule: ToolCompactionRule) -> str:
    head = lines[: rule.head_lines]
    tail = lines[-rule.tail_lines :] if rule.tail_lines else []

    # Indices already covered by head/tail; we don't want to re-include
    # the same line twice in "signal".
    head_idx = set(range(len(head)))
    tail_start = len(lines) - len(tail)
    tail_idx = set(range(tail_start, len(lines))) if tail else set()
    covered = head_idx | tail_idx

    pattern = re.compile("|".join(f"({p})" for p in rule.signal_patterns), re.IGNORECASE)
    signal: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if i in covered:
            continue
        if pattern.search(line):
            signal.append((i, line))
    # Cap signal so a build with thousands of warnings doesn't bloat
    # past the threshold — keep it proportional to head+tail size.
    signal_cap = max(rule.head_lines, rule.tail_lines)
    if len(signal) > signal_cap:
        # Prefer the first N — earlier errors are usually root cause.
        kept = signal[:signal_cap]
        dropped = len(signal) - signal_cap
        signal_block = (
            [f"[... showing first {signal_cap} of {len(signal)} signal lines; "
             f"dropped {dropped} ...]"]
            + [f"L{i+1}: {line}" for i, line in kept]
        )
    elif signal:
        signal_block = [f"L{i+1}: {line}" for i, line in signal]
    else:
        signal_block = []

    pieces: list[str] = []
    if head:
        pieces.append("\n".join(head))
    if signal_block:
        pieces.append("\n[... signal lines from omitted region ...]")
        pieces.append("\n".join(signal_block))
    omitted = len(lines) - len(head) - len(tail)
    if omitted > 0:
        pieces.append(f"\n[... omitted {omitted} lines (summary) ...]")
    if tail:
        pieces.append("\n".join(tail))
    return "\n".join(pieces)
