"""Channel ownership preflight — production-grade enforcement that OC is the sole channel handler.

Per the 2026-05-08 directive (see ``memory/user_oc_owns_all_channels.md``):

> OpenComputer is the SINGLE authoritative handler for every channel —
> Telegram, Discord, Slack, Matrix, etc. Nothing else may handle channel I/O.

The 2026-05-08 incident: the user's ``claude --channels plugin:telegram``
spawned a ``bun server.ts`` process that competed with OC for the same
Telegram bot polling slot. OC's adapter spun in conflict-retry loops for
hours; no Telegram replies got through. The fix was to kill the bun
process — but the user never knew it was there.

This module turns that silent failure into a loud, actionable refusal.

## Architecture

Before ``Gateway.start()`` connects any adapter, ``run_preflight()`` scans
the process list for known competitor patterns. If found:

* Default (``cfg.gateway.takeover_on_start = false``): raise
  :class:`ChannelOwnershipConflict` naming the competitor PID + cmdline.
  ``oc gateway start`` exits with a clear error; the operator decides.
* With ``cfg.gateway.takeover_on_start = true`` OR the
  ``--force-takeover`` flag: SIGTERM each competitor with a
  ``takeover_grace_seconds`` window, escalate to SIGKILL if needed,
  append an audit-log entry, and proceed.

## Audit log

Every takeover writes to
``<profile_home>/audit/competitor-takeover.jsonl`` — one JSON object per
competitor, fields ``ts``, ``pid``, ``kind``, ``cmdline_preview``,
``signal``, ``exit_code``, ``stale_lock_path``. Append-only, never
rotated by this module (operator handles retention).

## Why not webhook mode?

Webhook mode for Telegram would still have the "last setWebhook wins"
problem: Claude Code's bridge could re-register its own URL. The owner
ambiguity is at the bot-token layer, not the transport layer. Channel
ownership enforcement is the architecturally correct answer regardless
of whether the adapter polls or webhooks.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

logger = logging.getLogger("opencomputer.gateway.preflight")


@dataclass(frozen=True, slots=True)
class Competitor:
    """A non-OC process detected as a channel-handling competitor."""

    pid: int
    kind: str
    cmdline_preview: str  # first 200 chars

    def display(self) -> str:
        return f"PID {self.pid} ({self.kind}): {self.cmdline_preview}"


class ChannelOwnershipConflict(RuntimeError):  # noqa: N818
    """Raised when competitors are detected and takeover is not authorized.

    The class name uses ``Conflict`` (not ``Error``) because the situation
    isn't a programming error — it's a runtime ownership violation that
    the operator must explicitly resolve. The ``Error`` suffix would
    misframe what's happening.
    """

    def __init__(self, competitors: list[Competitor]) -> None:
        self.competitors = competitors
        lines = [
            "channel ownership conflict — OpenComputer is configured as "
            "the sole channel handler, but other process(es) are also "
            "handling channels:",
            *(f"  - {c.display()}" for c in competitors),
            "",
            "To resolve:",
            "  1. Stop the competitor manually:  kill -TERM "
            f"{' '.join(str(c.pid) for c in competitors)}",
            "  2. Or run once:  oc gateway preflight --force-takeover",
            "  3. Or set in ~/.opencomputer/<profile>/config.yaml:",
            "       gateway:",
            "         takeover_on_start: true",
        ]
        super().__init__("\n".join(lines))


# Known competitor patterns. Order matters — first match wins, so
# more-specific patterns come first.
_COMPETITOR_PATTERNS: ClassVar = (
    (
        "claude_code_telegram_bridge",
        re.compile(
            r"claude-plugins-official/telegram"
            r"|claude.*channels.*plugin:?telegram",
            re.IGNORECASE,
        ),
    ),
    (
        "hermes_gateway",
        re.compile(
            r"hermes[_-]?cli(?:\.main)?\s+gateway"
            r"|hermes[_-]?agent.*gateway",
            re.IGNORECASE,
        ),
    ),
    (
        "rival_oc_gateway",
        re.compile(
            r"opencomputer\b.*\bgateway"
            r"|/oc\b.*\bgateway"
            r"|\boc\s+(--[^\s]+\s+)*gateway\b",
            re.IGNORECASE,
        ),
    ),
)


def _classify(cmdline: str) -> str | None:
    """Return the competitor ``kind`` if cmdline matches a known pattern, else None."""
    for kind, pat in _COMPETITOR_PATTERNS:
        if pat.search(cmdline):
            return kind
    return None


def _ps_snapshot() -> list[tuple[int, str]]:
    """Return ``[(pid, args), ...]`` from ``ps -eo pid,args``.

    Returns an empty list if ``ps`` isn't on PATH (Linux/macOS sandbox).
    """
    if shutil.which("ps") is None:
        logger.debug("preflight: ps not on PATH; skipping competitor detection")
        return []
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid,args"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("preflight: ps invocation failed: %s", e)
        return []
    if proc.returncode != 0:
        logger.warning("preflight: ps returned rc=%d", proc.returncode)
        return []

    out: list[tuple[int, str]] = []
    for line in proc.stdout.splitlines()[1:]:  # drop header row
        parts = line.strip().split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        out.append((pid, parts[1]))
    return out


# Shell wrappers — processes whose argv[0] is a shell are NOT channel
# bridges, they're just scripts that happen to mention the patterns
# (e.g. ``ps aux | grep -E "claude-plugins-official/telegram"``).
# Without this filter, the preflight regex finds itself in pipeline
# tooling and false-positives the user's own diagnostic shell scripts.
# Caught the hard way 2026-05-08: a zsh -c that grep-quoted the pattern
# ended up in the audit log + got SIGTERM'd mid-execution.
_SHELL_WRAPPER_PREFIXES: tuple[str, ...] = (
    "/bin/sh",
    "/bin/bash",
    "/bin/zsh",
    "/bin/dash",
    "/bin/ksh",
    "/bin/csh",
    "/bin/tcsh",
    "sh ",
    "bash ",
    "zsh ",
    "/usr/bin/env sh",
    "/usr/bin/env bash",
    "/usr/bin/env zsh",
)


def _is_shell_wrapper(cmdline: str) -> bool:
    """True iff cmdline starts with a known shell interpreter.

    Shell wrappers can mention competitor patterns in their args without
    BEING competitors (they're typically diagnostic scripts: ``ps | grep
    "...telegram..."``). We skip them to avoid SIGTERM'ing the user's
    own debugging tools mid-flight.
    """
    return any(cmdline.startswith(prefix) for prefix in _SHELL_WRAPPER_PREFIXES)


def detect_competitors(*, exclude_pids: set[int] | None = None) -> list[Competitor]:
    """Scan the process table for known channel-handler competitors.

    Args:
        exclude_pids: PIDs to ignore (typically ``{os.getpid(), os.getppid()}``
            so we don't flag ourselves).

    Returns:
        List of :class:`Competitor`, deduplicated by PID. Shell-wrapper
        processes (zsh -c, bash -c, etc.) are excluded — they're
        diagnostic scripts that mention competitor patterns as
        arguments, not competitors themselves.
    """
    excluded = set(exclude_pids or ())
    excluded.add(os.getpid())
    excluded.add(os.getppid())

    found: dict[int, Competitor] = {}
    for pid, cmdline in _ps_snapshot():
        if pid in excluded:
            continue
        if _is_shell_wrapper(cmdline):
            continue
        kind = _classify(cmdline)
        if kind is None:
            continue
        # Truncate for display; keep enough to identify the process.
        preview = cmdline[:200]
        found[pid] = Competitor(pid=pid, kind=kind, cmdline_preview=preview)
    return sorted(found.values(), key=lambda c: c.pid)


def _send_signal(pid: int, sig: int) -> bool:
    """``os.kill`` with errno-tolerant return. False on ESRCH (already dead)."""
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError as e:
        logger.warning("preflight: no permission to signal PID %d: %s", pid, e)
        return False


def _is_alive(pid: int) -> bool:
    return _send_signal(pid, 0)


def takeover(
    competitors: list[Competitor],
    *,
    grace_seconds: float = 5.0,
    audit_log: Path | None = None,
) -> list[Competitor]:
    """Terminate each competitor; return list of those that survived.

    Sequence per competitor:

    1. ``SIGTERM`` (clean shutdown)
    2. Poll every 100ms up to ``grace_seconds`` for the PID to exit
    3. ``SIGKILL`` if still alive
    4. Append a record to ``audit_log`` (if provided)

    Args:
        competitors: From :func:`detect_competitors`.
        grace_seconds: Wait window for SIGTERM to land. Default 5s.
        audit_log: Append-mode JSONL file. If None, no audit. Parent
            directory is created with ``mkdir(parents=True)``.

    Returns:
        Competitors that did NOT die (caller may want to surface to the
        operator). Empty list = full success.
    """
    survivors: list[Competitor] = []
    audit_records: list[dict] = []

    for c in competitors:
        record: dict = {
            "ts": datetime.now(UTC).isoformat(),
            "pid": c.pid,
            "kind": c.kind,
            "cmdline_preview": c.cmdline_preview,
            "signal": "SIGTERM",
            "exit_code": None,
        }

        if not _is_alive(c.pid):
            record["signal"] = "already_dead"
            audit_records.append(record)
            continue

        sent = _send_signal(c.pid, 15)  # SIGTERM
        if not sent:
            record["exit_code"] = "signal_refused"
            audit_records.append(record)
            survivors.append(c)
            continue

        # Poll for exit, up to grace_seconds.
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _is_alive(c.pid):
                record["exit_code"] = "clean_sigterm"
                break
            time.sleep(0.1)
        else:
            # Escalate.
            logger.warning(
                "preflight: PID %d (%s) did not exit within %.1fs of SIGTERM "
                "— sending SIGKILL",
                c.pid, c.kind, grace_seconds,
            )
            _send_signal(c.pid, 9)
            record["signal"] = "SIGKILL"
            # Brief follow-up window so audit shows actual exit.
            for _ in range(20):  # 2s max
                if not _is_alive(c.pid):
                    record["exit_code"] = "clean_sigkill"
                    break
                time.sleep(0.1)
            else:
                record["exit_code"] = "still_alive"
                survivors.append(c)

        audit_records.append(record)

    if audit_log is not None and audit_records:
        try:
            audit_log.parent.mkdir(parents=True, exist_ok=True)
            with audit_log.open("a", encoding="utf-8") as f:
                for rec in audit_records:
                    f.write(json.dumps(rec) + "\n")
        except OSError as e:
            logger.warning("preflight: failed to append audit log %s: %s", audit_log, e)

    return survivors


def default_audit_path(profile_home: Path) -> Path:
    """Standard location for the takeover audit log."""
    return profile_home / "audit" / "competitor-takeover.jsonl"


def run_preflight(
    *,
    takeover_on_start: bool,
    grace_seconds: float = 5.0,
    audit_log: Path | None = None,
    exclude_pids: set[int] | None = None,
) -> list[Competitor]:
    """Single entry point used by ``Gateway.start()``.

    Args:
        takeover_on_start: If True, terminate competitors. If False,
            raise :class:`ChannelOwnershipConflict` instead.
        grace_seconds: SIGTERM window before SIGKILL.
        audit_log: Path to JSONL audit log; ``None`` skips audit.
        exclude_pids: Forwarded to :func:`detect_competitors`.

    Returns:
        ``[]`` on success (no competitors, or all terminated). On
        failure raises :class:`ChannelOwnershipConflict` (mode=refuse)
        OR returns the survivor list (mode=takeover, partial failure).

    Raises:
        ChannelOwnershipConflict: When competitors found and
            ``takeover_on_start = False``.
    """
    competitors = detect_competitors(exclude_pids=exclude_pids)
    if not competitors:
        return []

    if not takeover_on_start:
        raise ChannelOwnershipConflict(competitors)

    logger.warning(
        "preflight: %d competitor(s) detected — taking over (gateway."
        "takeover_on_start=true): %s",
        len(competitors),
        ", ".join(c.display() for c in competitors),
    )
    survivors = takeover(
        competitors,
        grace_seconds=grace_seconds,
        audit_log=audit_log,
    )
    if survivors:
        logger.error(
            "preflight: takeover incomplete — %d competitor(s) survived: %s",
            len(survivors),
            ", ".join(c.display() for c in survivors),
        )
    else:
        logger.info(
            "preflight: %d competitor(s) terminated cleanly", len(competitors)
        )
    return survivors


__all__ = [
    "ChannelOwnershipConflict",
    "Competitor",
    "default_audit_path",
    "detect_competitors",
    "run_preflight",
    "takeover",
]


def _format_asdict(c: Competitor) -> dict:
    """Local alias used by unit tests + audit serialization."""
    return asdict(c)
