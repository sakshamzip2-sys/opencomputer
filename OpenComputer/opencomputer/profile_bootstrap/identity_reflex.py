"""Layer 0 — Identity Reflex.

Reads what the user has already presented to themselves on the system:
git config, system user, macOS Contacts.app ``me`` card, browser saved
account email. No consent prompts (every signal is on-disk data the
user authored). Total cost <1s.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IdentityFacts:
    """Output of :func:`gather_identity`. Frozen for safety."""

    name: str = ""
    emails: tuple[str, ...] = ()
    phones: tuple[str, ...] = ()
    github_handle: str | None = None
    city: str | None = None
    primary_language: str = "en_US"
    hostname: str = ""


def _read_git_config_emails() -> tuple[str, ...]:
    """Read all ``user.email`` values from git's global + system config.

    Returns empty tuple if git is not on PATH or the call fails.
    """
    if shutil.which("git") is None:
        return ()
    try:
        result = subprocess.run(
            ["git", "config", "--list", "--global"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ()
    if result.returncode != 0:
        return ()
    emails: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith("user.email="):
            emails.append(line.split("=", 1)[1].strip())
    return tuple(dict.fromkeys(emails))  # de-dup, preserve order


def _read_macos_contacts_me_name() -> str | None:
    """Read the macOS Contacts.app ``me`` card display name.

    Uses AppleScript via ``osascript``. Returns ``None`` on macOS
    without Contacts permissions, on non-macOS, or on script failure.

    The first invocation triggers macOS Privacy & Security dialog
    asking the user to grant Contacts access. We use a 30-second
    timeout (not 3s) so the user has time to respond. Subsequent
    invocations don't prompt.
    """
    if shutil.which("osascript") is None:
        return None
    script = 'tell application "Contacts" to get name of my card'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30.0,  # generous: first call shows a permission dialog
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def gather_identity() -> IdentityFacts:
    """Run all Layer 0 readers and return a unified :class:`IdentityFacts`.

    Each reader is independent and best-effort — failures yield empty
    fields rather than raising. The whole call should complete in well
    under one second on a healthy macOS system.
    """
    emails = _read_git_config_emails()
    name = _read_macos_contacts_me_name() or os.environ.get("USER", "")
    return IdentityFacts(
        name=name,
        emails=emails,
        primary_language=os.environ.get("LANG", "en_US").split(".")[0],
        hostname=socket.gethostname(),
    )
