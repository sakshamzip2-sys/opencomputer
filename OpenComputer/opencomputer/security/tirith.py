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
import re
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


# ─── local pre-flight ──────────────────────────────────────────────────
#
# Hermes-followup 2026-05-07. Two cheap pattern classes that fire BEFORE
# the external binary spawn so we don't pay subprocess latency on
# obvious cases AND we still catch them when ``tirith`` is uninstalled
# or unreachable. Each returns a list of finding dicts (same shape as
# ``check_command`` returns) — empty list = pass.


#: Sudo / privilege escalation patterns. Catches ``sudo``, ``doas``,
#: ``su -``, and the common ``su root`` form. Conservative — does NOT
#: flag ``sudo -V`` (version) or ``sudo -h`` (help) on the assumption
#: that an attacker who needs to escalate isn't asking for help text.
_SUDO_RE = re.compile(
    r"(?<![A-Za-z_])(?:sudo(?:\s|$)|doas(?:\s|$)|su\s+(?:-\s|root))",
    re.IGNORECASE,
)

#: Known-bad standalone binaries — running these from a chat-driven
#: command is almost always a mistake or an attack. ``mkfs.*`` formats
#: filesystems, ``dd`` is the classic disk-wiper, ``shred`` zeros
#: free space, ``fdisk``/``parted`` repartitions. The list is small +
#: high-confidence; bigger lists go in the external Tirith binary
#: where catalogued patterns live.
_BAD_BINARY_RE = re.compile(
    r"(?<![A-Za-z_])(mkfs(?:\.[a-z0-9]+)?|dd\s+if=|shred\s+|fdisk\s+|parted\s+)",
    re.IGNORECASE,
)

#: Network-exfiltration patterns. Catches ``curl --upload-file``,
#: ``curl -F`` (multipart with filename source), ``curl … --data-binary
#: @file``, and ``wget --post-file`` — all of which read a local file
#: and POST it to a remote endpoint. A chat-driven shell command that
#: uploads disk content to a URL is almost never legitimate; if it is,
#: the user can confirm via the tool-result-middleware route.
_EXFIL_RE = re.compile(
    r"(?:"
    r"\bcurl\b[^;|]*?(?:--upload-file|-F\s+\w+=@|--data-binary\s*@)"
    r"|\bwget\b[^;|]*?--post-file"
    r"|\bnc\b[^;|]*?<\s*/(?:etc|home|var)/"
    r")",
    re.IGNORECASE,
)

#: Crypto-mining indicators. The keys are well-known mining-binary
#: names + canonical pool URLs. A user who actually wants xmrig can
#: run it outside the agent; the agent should never spawn it.
_CRYPTO_MINER_RE = re.compile(
    r"(?<![A-Za-z_])(?:xmrig|minerd|cgminer|sgminer|t-rex|ethminer|nbminer)\b"
    r"|stratum\+(?:tcp|ssl)://"
    r"|(?:pool\.minexmr\.com|nanopool\.org|ethermine\.org|f2pool\.com)",
    re.IGNORECASE,
)

#: Shell-history clearing — common cover-tracks pattern. ``history -c``,
#: ``rm`` against ``~/.bash_history`` / ``~/.zsh_history``, ``unset
#: HISTFILE`` to disable the next session's logging.
_HIST_TAMPER_RE = re.compile(
    r"(?:"
    r"\bhistory\s+-c\b"
    r"|\brm\b[^;|]*?\.(?:bash|zsh|fish)_history"
    r"|\bunset\s+HISTFILE\b"
    r"|>\s*~/\.(?:bash|zsh|fish)_history\b"
    r")",
    re.IGNORECASE,
)


def local_preflight(command: str) -> list[dict[str, Any]]:
    """Cheap local scan run before the external binary spawn.

    Two pattern classes:
    - ``preflight.sudo_escalation`` — sudo/doas/su patterns.
    - ``preflight.dangerous_binary`` — mkfs/dd/shred/fdisk/parted.

    Returns a list of finding dicts with ``rule`` + ``severity`` +
    ``message`` keys (same shape ``check_command`` produces). Empty
    list = pass. The caller decides verdict; this function is pure.
    """
    findings: list[dict[str, Any]] = []
    if _SUDO_RE.search(command):
        findings.append(
            {
                "rule": "preflight.sudo_escalation",
                "severity": "block",
                "message": (
                    "command requests privilege escalation (sudo/doas/su) — "
                    "agent commands should run with the user's normal "
                    "privileges, never elevated"
                ),
            }
        )
    if _BAD_BINARY_RE.search(command):
        findings.append(
            {
                "rule": "preflight.dangerous_binary",
                "severity": "block",
                "message": (
                    "command invokes a destructive disk-management binary "
                    "(mkfs / dd / shred / fdisk / parted) — refused at "
                    "pre-flight"
                ),
            }
        )
    if _EXFIL_RE.search(command):
        findings.append(
            {
                "rule": "preflight.network_exfiltration",
                "severity": "block",
                "message": (
                    "command uploads local file content to a remote endpoint "
                    "(curl/wget/nc with file source) — refused at pre-flight; "
                    "if intentional, run outside the agent"
                ),
            }
        )
    if _CRYPTO_MINER_RE.search(command):
        findings.append(
            {
                "rule": "preflight.crypto_miner",
                "severity": "block",
                "message": (
                    "command invokes a crypto-mining binary or pool URL — "
                    "refused at pre-flight"
                ),
            }
        )
    if _HIST_TAMPER_RE.search(command):
        findings.append(
            {
                "rule": "preflight.history_tamper",
                "severity": "block",
                "message": (
                    "command tampers with shell history (clear / unset "
                    "HISTFILE / overwrite history file) — refused at "
                    "pre-flight as a cover-tracks signal"
                ),
            }
        )
    return findings


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
    # Hermes-followup 2026-05-07 — local pre-flight runs before the
    # binary spawn. If we catch something here, we BLOCK regardless of
    # tirith availability. Defence-in-depth: even when the upstream
    # binary is uninstalled, sudo escalation + disk-wipers don't slip
    # through.
    pre = local_preflight(command)
    if pre:
        return TirithResult(
            action="block",
            findings=pre,
            summary=f"blocked by local pre-flight: {pre[0]['rule']}",
        )

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
