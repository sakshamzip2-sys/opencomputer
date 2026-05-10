"""Approvals config — Hermes-parity ``security.approvals.{mode,timeout}``
plus OpenClaw-parity per-command pattern rules.

Maps the Hermes ``approvals.mode: manual|smart|off`` knob into
OpenComputer's idioms:

| Hermes mode | OC behaviour |
|---|---|
| ``manual`` (default) | standard consent gate flow (PER_ACTION tier prompts) |
| ``off`` | equivalent to ``--auto``: auto-allow consent prompts at the session level |
| ``smart`` | invokes :func:`opencomputer.security.smart_mode.assess_risk` for an LLM-based verdict; low-risk auto-allows, high-risk auto-denies, medium / uncertain fall through to the manual prompt |

``timeout`` overrides the consent gate's default 300s wait.

OpenClaw-parity (2026-05-10) — per-command pattern rules. Operators
declare allow/ask/deny verdicts for specific command shapes, e.g.::

    security:
      approvals:
        mode: manual
        command_rules:
          - pattern: "git commit"
            verdict: allow
          - pattern: "git push"
            verdict: ask
          - pattern: "rm -rf"
            verdict: deny

Rules are evaluated first-match-wins, in the order declared. A rule
verdict of ``allow`` short-circuits the consent gate; ``deny`` refuses
the command outright (without going through the LLM-driven Tirith scan
either, so denials are deterministic). ``ask`` is the default and
falls through to whatever ``mode`` would otherwise do.

Config-schema independence: like
:mod:`opencomputer.security.website_blocklist`, this module reads YAML
directly rather than going through the central ``SecurityConfig``
dataclass. Independence from concurrent ``security.*`` schema PRs.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger("opencomputer.security.approvals")


VALID_MODES: frozenset[str] = frozenset({"manual", "smart", "off"})
DEFAULT_MODE: str = "manual"
DEFAULT_TIMEOUT_S: float = 300.0  # 5 minutes — matches consent gate default

CommandVerdict = Literal["allow", "ask", "deny"]
VALID_VERDICTS: frozenset[str] = frozenset({"allow", "ask", "deny"})


@dataclass(frozen=True, slots=True)
class CommandRule:
    """One per-command approval rule.

    Patterns are matched against the *raw* command string passed to
    Bash — typically the full shell string the agent would run. Two
    matchers are supported:

    * ``substring`` (default) — match if ``pattern`` appears anywhere
      in the command. Cheap, predictable; recommended for most users.
    * ``glob`` — :mod:`fnmatch` shell-style globs. Use when you need
      ``git push *origin*`` or ``rm -rf /tmp/*``-style expressions.
    * ``regex`` — full Python regex. Power-tool; an invalid regex
      degrades gracefully (rule is skipped + warning logged).

    Verdict ``deny`` short-circuits Tirith and the consent gate.
    Verdict ``allow`` short-circuits the consent gate but Tirith
    still runs (Tirith is a static-analysis backstop the operator
    cannot override away — only hardline + this deny can).
    Verdict ``ask`` is the default fallthrough — equivalent to no
    rule matching.
    """

    pattern: str
    verdict: CommandVerdict = "ask"
    matcher: Literal["substring", "glob", "regex"] = "substring"


@dataclass(frozen=True, slots=True)
class ApprovalsConfig:
    """Resolved approvals settings — what callers consult.

    Attributes:
        mode: one of ``manual``, ``smart``, ``off``. ``smart`` invokes
            the auxiliary-LLM risk assessor (see
            :mod:`opencomputer.security.smart_mode`); ``off`` auto-allows
            all consent prompts (operator opt-in); ``manual`` is the
            default user-prompted flow.
        timeout_s: seconds the consent gate waits for a user response
            before auto-denying. Mirrors the Hermes
            ``approvals.timeout`` knob.
        command_rules: ordered list of :class:`CommandRule` evaluated
            first-match-wins. OpenClaw-parity per-command verdicts.
    """

    mode: str = DEFAULT_MODE
    timeout_s: float = DEFAULT_TIMEOUT_S
    command_rules: tuple[CommandRule, ...] = field(default_factory=tuple)

    @property
    def auto_allow(self) -> bool:
        """True iff the caller should treat consent prompts as pre-approved.

        Mirrors ``--auto`` / OC's existing yolo_mode semantics. Intended
        for callers that want a single-question "should I prompt or not"
        without each rebuilding the mode-vs-flag logic.
        """
        return self.mode == "off"

    def evaluate_command(self, command: str) -> CommandVerdict | None:
        """Return the verdict for *command* or ``None`` if no rule matched.

        First-match-wins. ``None`` means "fall through to the
        ``mode``-driven path" (i.e. the existing Hermes-parity
        behaviour). Defensive: a malformed regex in a rule is skipped
        with a warning rather than crashing the whole evaluation.
        """
        for rule in self.command_rules:
            try:
                if _command_matches(command, rule):
                    return rule.verdict
            except re.error as e:
                logger.warning(
                    "approvals: invalid regex %r in rule (skipping): %s",
                    rule.pattern, e,
                )
        return None


def _command_matches(command: str, rule: CommandRule) -> bool:
    """Pure matcher — used by :meth:`ApprovalsConfig.evaluate_command`."""
    if rule.matcher == "substring":
        return rule.pattern in command
    if rule.matcher == "glob":
        return fnmatch.fnmatchcase(command, rule.pattern)
    if rule.matcher == "regex":
        return re.search(rule.pattern, command) is not None
    return False


def parse_mode(raw: object) -> str:
    """Normalise a raw config value into a known mode name.

    Unknown / missing → :data:`DEFAULT_MODE`. ``smart`` is accepted but
    logged once as "not yet wired". PyYAML quirk: unquoted ``off`` /
    ``no`` parse as boolean False, so both False and the string ``off``
    are honoured equally.
    """
    # YAML quirk: unquoted ``off`` parses as boolean False.
    if raw is False:
        return "off"
    if raw is True:
        # ``on`` / ``yes`` don't map to any mode in Hermes — user almost
        # certainly meant ``manual``. Don't silently miscoerce.
        logger.warning(
            "security.approvals.mode parsed as boolean True (likely "
            "unquoted 'on'/'yes'); falling back to %r. Quote the value "
            "in config.yaml if you meant something specific.",
            DEFAULT_MODE,
        )
        return DEFAULT_MODE
    if not isinstance(raw, str):
        return DEFAULT_MODE
    candidate = raw.strip().lower()
    if candidate not in VALID_MODES:
        logger.warning(
            "security.approvals.mode=%r is unknown; falling back to %r. "
            "Valid: %s",
            raw, DEFAULT_MODE, ", ".join(sorted(VALID_MODES)),
        )
        return DEFAULT_MODE
    return candidate


def parse_timeout(raw: object) -> float:
    """Normalise a raw config value into a float seconds value.

    Non-numeric / missing → :data:`DEFAULT_TIMEOUT_S`. Negative values
    are clamped to 1.0 (a 0-or-negative timeout would auto-deny every
    prompt instantly which is not what any user means).
    """
    try:
        v = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if v <= 0:
        return 1.0
    return v


def parse_command_rules(raw: object) -> tuple[CommandRule, ...]:
    """Parse the ``command_rules`` YAML list into a tuple of rules.

    Tolerant of malformed entries — a single bad rule is skipped with
    a warning, the rest are honoured. Accepts either::

        command_rules:
          - pattern: "git push"
            verdict: ask
          - pattern: "^rm -rf /"
            verdict: deny
            matcher: regex

    or the OpenClaw-style mapping shorthand (less expressive, no
    matcher choice — substring only)::

        command_rules:
          "git commit": allow
          "git push": ask
          "rm -rf": deny
    """
    if isinstance(raw, dict):
        out: list[CommandRule] = []
        for pattern, verdict_raw in raw.items():
            verdict = _normalise_verdict(verdict_raw)
            if verdict is None:
                continue
            out.append(CommandRule(pattern=str(pattern), verdict=verdict))
        return tuple(out)
    if not isinstance(raw, list):
        return ()
    out_list: list[CommandRule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("pattern", "")).strip()
        if not pattern:
            continue
        verdict = _normalise_verdict(entry.get("verdict"))
        if verdict is None:
            continue
        matcher_raw = str(entry.get("matcher", "substring")).strip().lower()
        matcher = matcher_raw if matcher_raw in {"substring", "glob", "regex"} else "substring"
        out_list.append(
            CommandRule(pattern=pattern, verdict=verdict, matcher=matcher)  # type: ignore[arg-type]
        )
    return tuple(out_list)


def _normalise_verdict(raw: object) -> CommandVerdict | None:
    if not isinstance(raw, str):
        logger.warning(
            "approvals: command rule verdict %r is not a string — skipping rule",
            raw,
        )
        return None
    candidate = raw.strip().lower()
    if candidate not in VALID_VERDICTS:
        logger.warning(
            "approvals: unknown verdict %r — skipping rule. Valid: %s",
            raw, ", ".join(sorted(VALID_VERDICTS)),
        )
        return None
    return candidate  # type: ignore[return-value]


def load_approvals_from_active_config() -> ApprovalsConfig:
    """Read ``security.approvals.{mode,timeout,command_rules}`` from the
    active profile's ``config.yaml``.

    On any error returns the safe default (``manual``, 300s, no rules).
    This is the public hot-path callers should use.
    """
    try:
        import yaml

        from opencomputer.profiles import (
            profile_home_dir,
            read_active_profile,
        )

        prof = read_active_profile()
        if prof is None:
            return ApprovalsConfig()
        config_path = profile_home_dir(prof) / "config.yaml"
        if not config_path.exists():
            return ApprovalsConfig()
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        appr = (data.get("security") or {}).get("approvals") or {}
        return ApprovalsConfig(
            mode=parse_mode(appr.get("mode")),
            timeout_s=parse_timeout(appr.get("timeout")),
            command_rules=parse_command_rules(appr.get("command_rules")),
        )
    except Exception:  # noqa: BLE001
        return ApprovalsConfig()


__all__ = [
    "DEFAULT_MODE",
    "DEFAULT_TIMEOUT_S",
    "VALID_MODES",
    "VALID_VERDICTS",
    "ApprovalsConfig",
    "CommandRule",
    "CommandVerdict",
    "load_approvals_from_active_config",
    "parse_command_rules",
    "parse_mode",
    "parse_timeout",
]
