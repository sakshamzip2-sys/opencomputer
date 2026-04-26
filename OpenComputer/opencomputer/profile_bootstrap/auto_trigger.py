"""Auto-trigger for profile_bootstrap on first chat (user vision).

User's stated vision (this session, verbatim): "the chat llm should
know about the user before the user even starts using it". PR #143
shipped the bootstrap orchestrator but as a manual ``opencomputer
profile bootstrap`` invocation — most users would never discover it,
so on first chat the agent has no identity facts in context.

This module makes the bootstrap fire automatically at first chat, in
a background daemon thread, with the lightest scan possible (identity
+ git only, no browser/calendar — those are slower and more
sensitive). The chat loop never blocks on bootstrap completion;
results land in the graph for the NEXT prompt-builder pass.

Opt out with ``OPENCOMPUTER_NO_AUTO_BOOTSTRAP=1`` for users who
want explicit control.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

_log = logging.getLogger("opencomputer.profile_bootstrap.auto_trigger")


def _marker_path() -> Path:
    """Same path :mod:`opencomputer.cli_profile` uses — single source of truth."""
    from opencomputer.agent.config import _home

    return _home() / "profile_bootstrap" / "complete.json"


def should_auto_bootstrap() -> tuple[bool, str]:
    """Decide whether the auto-trigger should fire on this invocation.

    Returns ``(should_run, reason_string)`` so callers can log the
    decision without having to re-derive it. Negative reasons are
    short hints suitable for a debug log line.
    """
    if os.environ.get("OPENCOMPUTER_NO_AUTO_BOOTSTRAP"):
        return False, "opted out via OPENCOMPUTER_NO_AUTO_BOOTSTRAP"

    stdin = getattr(sys, "stdin", None)
    if stdin is None or not stdin.isatty():
        return False, "non-TTY stdin"

    if _marker_path().exists():
        return False, "already bootstrapped (marker file exists)"

    return True, "first-run; marker absent and TTY"


def kick_off_in_background() -> threading.Thread | None:
    """Run :func:`run_bootstrap` (quick mode) in a daemon thread.

    Quick mode = identity + git only. Skips:
    - quick_interview (would prompt the user; we can't ask without
      interrupting the chat startup)
    - calendar reader (slow, requires entitlements on macOS Sequoia)
    - browser history (slow on power users with thousands of entries)

    Returns the started thread on success (so tests can ``join()`` it)
    or ``None`` when the policy says skip. Every error inside the
    thread is logged at debug only — never crashes the chat loop.
    """
    should, reason = should_auto_bootstrap()
    if not should:
        _log.debug("auto-bootstrap skipped: %s", reason)
        return None

    def _worker() -> None:
        try:
            from opencomputer.profile_bootstrap.orchestrator import run_bootstrap

            scan_roots = [
                p
                for p in (
                    Path.home() / "Documents",
                    Path.home() / "Desktop",
                    Path.home() / "Downloads",
                )
                if p.exists()
            ]
            git_repos = [
                p
                for p in (
                    Path.home() / "Vscode",
                    Path.home() / "code",
                    Path.home() / "src",
                    Path.home() / "Projects",
                )
                if p.exists()
            ]
            run_bootstrap(
                interview_answers={},
                scan_roots=scan_roots,
                git_repos=git_repos,
                include_calendar=False,
                include_browser_history=False,
                marker_path=_marker_path(),
            )
            _log.debug("auto-bootstrap completed; marker written to %s", _marker_path())
        except Exception as exc:  # noqa: BLE001 — never crash the chat loop
            _log.debug("auto-bootstrap failed silently: %s", exc)

    thread = threading.Thread(
        target=_worker, daemon=True, name="oc-auto-bootstrap"
    )
    thread.start()
    return thread
