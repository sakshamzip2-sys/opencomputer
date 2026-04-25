"""Cron prompt threat scanner.

Cron jobs run in fresh sessions with full tool access — there's no human
in the loop reading the response, so a prompt-injection in a cron is much
higher risk than in a normal session. This module screens prompts for
known-bad patterns at registration time and again before each tick.

Ported from `sources/hermes-agent-2026.4.23/tools/cronjob_tools.py`
(Hermes Self-Evolution / Hermes Agent project, MIT licensed).

Use sites:
- `opencomputer.cron.jobs.create_job` — block on register
- `opencomputer.cron.scheduler.tick` — re-scan before run (defence in depth)

The scanner is conservative: it only catches obvious patterns and is not
a replacement for the F1 ConsentGate, which gates *capabilities*. The
threat scan + ConsentGate together provide layered defense.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Critical-severity patterns. Match → block.
# ---------------------------------------------------------------------------

_CRON_THREAT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"ignore\s+(?:\w+\s+)*(?:previous|all|above|prior)\s+(?:\w+\s+)*instructions", re.IGNORECASE), "prompt_injection"),
    (re.compile(r"do\s+not\s+tell\s+the\s+user", re.IGNORECASE), "deception_hide"),
    (re.compile(r"system\s+prompt\s+override", re.IGNORECASE), "sys_prompt_override"),
    (re.compile(r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", re.IGNORECASE), "disregard_rules"),
    (re.compile(r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", re.IGNORECASE), "exfil_curl"),
    (re.compile(r"wget\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", re.IGNORECASE), "exfil_wget"),
    (re.compile(r"cat\s+[^\n]*(\.env|credentials|\.netrc|\.pgpass)", re.IGNORECASE), "read_secrets"),
    (re.compile(r"authorized_keys", re.IGNORECASE), "ssh_backdoor"),
    (re.compile(r"/etc/sudoers|visudo", re.IGNORECASE), "sudoers_mod"),
    (re.compile(r"rm\s+-rf\s+/", re.IGNORECASE), "destructive_root_rm"),
)

# ---------------------------------------------------------------------------
# Invisible / control characters used in injection attacks. Bidi overrides
# can flip text rendering so what the user sees ≠ what the model sees.
# ---------------------------------------------------------------------------

_CRON_INVISIBLE_CHARS: frozenset[str] = frozenset({
    "​",  # zero-width space
    "‌",  # zero-width non-joiner
    "‍",  # zero-width joiner
    "⁠",  # word joiner
    "﻿",  # zero-width no-break space (BOM)
    "‪",  # left-to-right embedding
    "‫",  # right-to-left embedding
    "‬",  # pop directional formatting
    "‭",  # left-to-right override
    "‮",  # right-to-left override (the famous "spoof" character)
})


class CronThreatBlocked(ValueError):  # noqa: N818 — naming consistent w/ _TickLockHeld
    """Raised when a cron prompt fails the threat scan.

    The exception message names the matched pattern id so callers can log /
    surface a concrete reason without leaking the rule regex.
    """

    def __init__(self, pattern_id: str, sample: str | None = None) -> None:
        msg = f"Blocked: cron prompt matches threat pattern {pattern_id!r}."
        if sample:
            msg += f" (sample: {sample!r})"
        super().__init__(msg)
        self.pattern_id = pattern_id


def scan_cron_prompt(prompt: str) -> str:
    """Scan a cron prompt for critical threats.

    Returns the empty string when the prompt passes; returns a non-empty
    error message when blocked. Callers can branch on the truthiness of
    the return.

    For the exception-raising variant, see :func:`assert_cron_prompt_safe`.
    """
    for char in _CRON_INVISIBLE_CHARS:
        if char in prompt:
            return (
                f"Blocked: prompt contains invisible unicode "
                f"U+{ord(char):04X} (possible injection)."
            )
    for pattern, pid in _CRON_THREAT_PATTERNS:
        if pattern.search(prompt):
            return (
                f"Blocked: prompt matches threat pattern {pid!r}. "
                "Cron prompts must not contain injection or exfiltration payloads."
            )
    return ""


def assert_cron_prompt_safe(prompt: str) -> None:
    """Like :func:`scan_cron_prompt` but raises :class:`CronThreatBlocked` on match.

    Convenience for code paths that prefer exception flow.
    """
    err = scan_cron_prompt(prompt)
    if err:
        for pattern, pid in _CRON_THREAT_PATTERNS:
            if pattern.search(prompt):
                raise CronThreatBlocked(pid, sample=prompt[:80])
        for char in _CRON_INVISIBLE_CHARS:
            if char in prompt:
                raise CronThreatBlocked(f"invisible_unicode_U+{ord(char):04X}")
        raise CronThreatBlocked("unknown")


__all__ = [
    "CronThreatBlocked",
    "scan_cron_prompt",
    "assert_cron_prompt_safe",
]
