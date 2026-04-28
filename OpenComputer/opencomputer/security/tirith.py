"""Tirith pre-exec command scanner — Hermes Tier 3 port (MVP).

Wraps the external Rust binary at ``sheeki03/tirith`` for shell-command
security scanning before execution. Catches:

- Homograph URL attacks (Cyrillic look-alikes in domains)
- ``curl … | bash``-style pipe-to-shell patterns
- Suspicious sudo escalations
- Known-bad binary patterns

This is the **MVP** wrapper. Auto-install + cosign verification (which
the Hermes upstream ships) are intentionally NOT in this PR — users
install ``tirith`` themselves (``brew install tirith`` / ``cargo install
tirith``) and we just call it. Lazy-install is a clean follow-up.

Config (``~/.opencomputer/<profile>/config.yaml``)::

    security:
      tirith:
        enabled: true              # default false; opt-in
        path: tirith               # bin name on PATH (or absolute)
        timeout_seconds: 5         # subprocess timeout
        fail_open: true            # on spawn error / timeout: allow

Verdict semantics (per Tirith's exit codes — JSON enriches the
description but never overrides the verdict):

- exit 0 → ``allow``
- exit 1 → ``block``
- exit 2 → ``warn``
- anything else / spawn error / timeout → ``allow`` (if ``fail_open``)
  or ``block`` (if not)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("opencomputer.security.tirith")

Verdict = Literal["allow", "warn", "block"]

_DEFAULT_TIMEOUT = 5
_MAX_FINDINGS = 50
_MAX_SUMMARY_LEN = 500


@dataclass(frozen=True, slots=True)
class TirithResult:
    """Outcome of a single scan."""

    action: Verdict
    findings: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    raw_exit_code: int | None = None
    error: str | None = None

    def is_blocked(self) -> bool:
        return self.action == "block"

    def is_warning(self) -> bool:
        return self.action == "warn"


def _resolve_binary(path: str) -> str | None:
    """Locate the tirith binary by name (PATH lookup) or absolute path."""
    if os.path.isabs(path):
        return path if os.path.isfile(path) else None
    return shutil.which(path)


def is_available(*, path: str = "tirith") -> bool:
    """Whether the tirith binary is reachable. Useful for doctor checks."""
    return _resolve_binary(path) is not None


def check_command(
    command: str,
    *,
    path: str = "tirith",
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    fail_open: bool = True,
    shell: str = "posix",
) -> TirithResult:
    """Scan ``command`` and return a :class:`TirithResult`.

    The exit code is the **verdict source of truth** — JSON output is
    parsed for findings/summary but never overrides the action.

    Failures (binary missing, timeout, JSON parse error) honor
    ``fail_open``: by default the call returns ``allow`` so a misconfigured
    Tirith never bricks the agent loop. Set ``fail_open=False`` only when
    you want strict-deny on uncertainty (e.g., regulated environments).
    """
    bin_path = _resolve_binary(path)
    if bin_path is None:
        action: Verdict = "allow" if fail_open else "block"
        return TirithResult(
            action=action,
            error=f"tirith binary not found ({path!r})",
            summary="tirith not installed; install via your package manager",
        )

    try:
        out = subprocess.run(
            [
                bin_path,
                "check",
                "--json",
                "--non-interactive",
                "--shell",
                shell,
                "--",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        action = "allow" if fail_open else "block"
        logger.warning("tirith timed out after %ds", timeout_seconds)
        return TirithResult(
            action=action,
            error=f"tirith timeout ({timeout_seconds}s)",
            summary=f"tirith scan timed out (fail_{'open' if fail_open else 'closed'})",
        )
    except OSError as exc:
        action = "allow" if fail_open else "block"
        logger.error("tirith spawn error: %s", exc)
        return TirithResult(
            action=action,
            error=f"spawn error: {exc}",
            summary=f"tirith spawn failed (fail_{'open' if fail_open else 'closed'})",
        )

    # Verdict from exit code (always trusted)
    if out.returncode == 0:
        verdict: Verdict = "allow"
    elif out.returncode == 1:
        verdict = "block"
    elif out.returncode == 2:
        verdict = "warn"
    else:
        verdict = "allow" if fail_open else "block"

    # Parse JSON for description (best-effort)
    findings: list[dict[str, Any]] = []
    summary = ""
    if out.stdout.strip():
        try:
            parsed = json.loads(out.stdout)
            if isinstance(parsed, dict):
                findings = list(parsed.get("findings", []))[:_MAX_FINDINGS]
                summary = str(parsed.get("summary", ""))[:_MAX_SUMMARY_LEN]
        except json.JSONDecodeError as exc:
            logger.warning("tirith JSON parse failed: %s", exc)

    return TirithResult(
        action=verdict,
        findings=findings,
        summary=summary,
        raw_exit_code=out.returncode,
    )


def format_findings_for_user(result: TirithResult) -> str:
    """Render findings as a short multi-line string for chat / approval display."""
    if not result.findings and not result.summary:
        return ""
    lines: list[str] = []
    if result.summary:
        lines.append(result.summary)
    for f in result.findings:
        sev = f.get("severity", "info")
        title = f.get("title") or f.get("rule") or "finding"
        desc = f.get("description") or ""
        lines.append(f"[{sev}] {title}: {desc}".rstrip(": "))
    return "\n".join(lines)


__all__ = [
    "TirithResult",
    "Verdict",
    "check_command",
    "format_findings_for_user",
    "is_available",
]
