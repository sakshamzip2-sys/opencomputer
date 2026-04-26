"""Detect repeated tool-use patterns within a session.

Phase 5.1 of catch-up plan. The detector is pure-Python, no IO. The hook
that drives it lives in the coding-harness plugin (PostToolUse). Drain
proposals at end-of-turn; the synthesizer (Phase 5.2) takes it from there.

Pattern keying
--------------

A *pattern* is the discriminator we count repetitions of. The default
key is fairly coarse — too fine-grained and we never reach the
threshold; too coarse and unrelated calls collide.

Current rules (tunable per call):

- ``Bash`` calls: key on the first whitespace-separated token of the
  command (i.e. the binary name) + outcome (ok/fail).
- ``Edit`` / ``MultiEdit``: key on the file extension + outcome.
- All other tools: key on tool name + outcome.

Drain semantics
---------------

``drain_proposals()`` returns each pattern's proposal *once* —
re-observing the same key after draining does not re-fire until at
least ``threshold`` more occurrences land. This prevents the same
pattern from spamming the synthesizer in long-running sessions.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class SkillDraftProposal:
    """One candidate for a new skill, surfaced by repeated tool-use observation.

    Attributes
    ----------
    pattern_key:
        The discriminator (e.g. ``"bash:pytest:fail"``). Stable across
        sessions for the same pattern shape.
    pattern_summary:
        Human-readable one-liner ("pytest failed 3 times").
    sample_arguments:
        Up to 3 tool-call argument dicts that produced this pattern.
        Used by the synthesizer to write a concrete SKILL.md.
    count:
        Total observations behind this proposal at drain time.
    """

    pattern_key: str
    pattern_summary: str
    sample_arguments: tuple[dict[str, Any], ...]
    count: int


def _bash_first_token(arguments: dict[str, Any]) -> str:
    cmd = (arguments.get("command") or "").strip()
    if not cmd:
        return ""
    # Strip env-var prefixes like FOO=bar baz; binary is the first
    # token that doesn't contain '='.
    for tok in cmd.split():
        if "=" not in tok:
            return tok
    return cmd.split()[0]


def _edit_extension(arguments: dict[str, Any]) -> str:
    fp = arguments.get("file_path") or arguments.get("path") or ""
    if not fp:
        return ""
    return Path(fp).suffix or ""


@dataclass
class PatternDetector:
    """Stateful counter that surfaces ≥``threshold`` repeated patterns.

    Single-session use. Re-instantiate per session; the bridge wires it
    via SessionStart hook (Phase 5.4 follow-up).
    """

    threshold: int = 3
    _counts: Counter = field(default_factory=Counter)
    _samples: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    _proposed: set[str] = field(default_factory=set)

    @staticmethod
    def _pattern_key(tool_name: str, arguments: dict[str, Any], error: bool) -> str:
        outcome = "fail" if error else "ok"
        if tool_name == "Bash":
            tok = _bash_first_token(arguments)
            return f"bash:{tok}:{outcome}"
        if tool_name in ("Edit", "MultiEdit", "Write"):
            ext = _edit_extension(arguments)
            return f"{tool_name.lower()}:{ext}:{outcome}"
        return f"{tool_name.lower()}:{outcome}"

    def observe(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        error: bool = False,
    ) -> None:
        """Record one tool invocation. Call from PostToolUse hook."""
        args = arguments or {}
        key = self._pattern_key(tool_name, args, error)
        self._counts[key] += 1
        # Keep up to 5 samples per pattern; we only emit 3 in proposals
        # but having spares means draining + later debugging works.
        bucket = self._samples.setdefault(key, [])
        if len(bucket) < 5:
            bucket.append(args)

    def drain_proposals(self) -> list[SkillDraftProposal]:
        """Return one proposal per *new* pattern that crossed the threshold.

        Idempotent against re-calls without new observations: a pattern
        that's already produced a proposal is in ``_proposed`` and is
        skipped until reset.
        """
        out: list[SkillDraftProposal] = []
        for key, count in self._counts.items():
            if count >= self.threshold and key not in self._proposed:
                samples = tuple(self._samples.get(key, [])[:3])
                summary = self._summary_for(key, count)
                out.append(
                    SkillDraftProposal(
                        pattern_key=key,
                        pattern_summary=summary,
                        sample_arguments=samples,
                        count=count,
                    )
                )
                self._proposed.add(key)
        return out

    def reset_proposed(self) -> None:
        """Clear the 'already proposed' set. Use only for tests / explicit reset."""
        self._proposed.clear()

    @staticmethod
    def _summary_for(key: str, count: int) -> str:
        # key shapes: 'bash:pytest:fail' / 'edit:.py:ok' / 'recall:ok'
        parts = key.split(":")
        if parts[0] == "bash" and len(parts) >= 3:
            verb = "failed" if parts[2] == "fail" else "succeeded"
            return f"`{parts[1]}` shell command {verb} {count} times"
        if parts[0] in ("edit", "multiedit", "write") and len(parts) >= 3:
            ext = parts[1] or "<no-ext>"
            verb = "failed" if parts[2] == "fail" else "succeeded"
            return f"`{parts[0]}` on {ext} files {verb} {count} times"
        return f"Pattern `{key}` observed {count} times"
