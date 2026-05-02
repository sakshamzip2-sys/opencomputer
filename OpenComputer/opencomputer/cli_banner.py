"""OpenComputer welcome banner.

Visual + structure modeled after hermes-agent's banner.py.
Independently re-implemented on rich (no code copied).

Public API:
  - build_welcome_banner(console, model, cwd, *, session_id, home) -> None
  - format_banner_version_label() -> str
  - get_available_skills() -> dict[str, list[str]]
  - get_available_tools() -> dict[str, list[str]]
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from opencomputer import __version__

__all__ = [
    "build_welcome_banner",
    "format_banner_version_label",
    "get_available_skills",
    "get_available_tools",
]


def _git_short_sha() -> Optional[str]:
    """Return 7-char git SHA of HEAD, or None if not in a git repo."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            stderr=subprocess.DEVNULL,
            cwd=Path(__file__).parent,
            text=True,
            timeout=2,
        ).strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def format_banner_version_label() -> str:
    """`OpenComputer v0.1.0 · sha`."""
    sha = _git_short_sha()
    if sha:
        return f"OpenComputer v{__version__} · {sha}"
    return f"OpenComputer v{__version__}"


def get_available_skills() -> dict[str, list[str]]:
    raise NotImplementedError("Lands in Task 11")


def get_available_tools() -> dict[str, list[str]]:
    raise NotImplementedError("Lands in Task 11")


def build_welcome_banner(*args, **kwargs) -> None:
    raise NotImplementedError("Lands in Task 12")
