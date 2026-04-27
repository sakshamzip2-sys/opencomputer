"""Skills Guard — static-analysis security scanner for SKILL.md content.

Trust-tier model with regex pattern catalogue. Every skill that crosses a
trust boundary (community installs, agent-created drafts) gets scanned
before activation or persistence; unscanned-by-default builtin skills
ship with the repo.

Tiers:
- ``builtin``       — bundled with this repo, never blocked
- ``trusted``       — known-good authors (openai/skills, anthropics/skills, ...)
- ``community``     — third-party imports; blocked on caution+ findings
- ``agent-created`` — drafted by the agent itself; "ask" verdict on dangerous

The catalogue (patterns + invisibles + structural limits) lives in
:mod:`opencomputer.skills_guard.threat_patterns`. Scan logic is in
:mod:`opencomputer.skills_guard.scanner`. Policy decisions are in
:mod:`opencomputer.skills_guard.policy`. Keep the split — adding new
patterns should mean editing one file, not three.
"""

from __future__ import annotations

from .policy import (
    INSTALL_POLICY,
    PolicyDecision,
    format_scan_report,
    resolve_trust_level,
    should_allow_install,
)
from .scanner import (
    Finding,
    ScanResult,
    content_hash,
    scan_file,
    scan_skill,
)

__all__ = [
    "Finding",
    "INSTALL_POLICY",
    "PolicyDecision",
    "ScanResult",
    "content_hash",
    "format_scan_report",
    "resolve_trust_level",
    "scan_file",
    "scan_skill",
    "should_allow_install",
]
