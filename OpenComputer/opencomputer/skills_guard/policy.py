"""Trust-tier dispatch + install-decision API.

Resolves a skill's ``source`` string (e.g. ``"openai/skills/code-review"``,
``"agent-created"``, ``"builtin"``) to one of four trust levels and looks up
the install verdict for ``(trust_level, scan_verdict)`` in
:data:`INSTALL_POLICY`.

The matrix:

|              | safe   | caution | dangerous |
|--------------|--------|---------|-----------|
| builtin      | allow  | allow   | allow     |
| trusted      | allow  | allow   | block     |
| community    | allow  | block   | block     |
| agent-created| allow  | allow   | ask       |

``ask`` returns ``None`` from :func:`should_allow_install` so callers can
prompt the user. ``block`` returns ``False``; ``allow`` returns ``True``.
"""

from __future__ import annotations

from typing import Literal

from .scanner import ScanResult

PolicyDecision = Literal["allow", "block", "ask"]

# Source prefixes considered first-party / well-vetted. These are
# allowlisted upstream catalogs from which we accept ``caution``-level
# findings without blocking.
TRUSTED_REPOS: frozenset[str] = frozenset({
    "openai/skills",
    "anthropics/skills",
    "claude-plugins/everything-claude-code",
    "claude-plugins/superpowers",
})

# Trust-level × scan-verdict → policy decision.
INSTALL_POLICY: dict[str, tuple[PolicyDecision, PolicyDecision, PolicyDecision]] = {
    #                  safe      caution    dangerous
    "builtin":       ("allow",  "allow",   "allow"),
    "trusted":       ("allow",  "allow",   "block"),
    "community":     ("allow",  "block",   "block"),
    "agent-created": ("allow",  "allow",   "ask"),
}

# Index into the per-tier tuple above.
_VERDICT_INDEX: dict[str, int] = {"safe": 0, "caution": 1, "dangerous": 2}


def resolve_trust_level(source: str) -> str:
    """Map a source string to one of: builtin / trusted / community / agent-created.

    Recognized:

    - ``"builtin"`` / ``"official/..."`` → ``builtin``
    - ``"agent-created"`` → ``agent-created``
    - prefix matches ``TRUSTED_REPOS`` → ``trusted``
    - everything else → ``community``

    Common skills-hub aliases (``"skills-sh/..."``, etc.) are stripped before
    matching so a skill imported from the hub still resolves to its
    underlying author.
    """
    prefix_aliases = ("skills-sh/", "skills.sh/", "skils-sh/", "skils.sh/")
    normalized = source
    for prefix in prefix_aliases:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break

    if normalized == "agent-created":
        return "agent-created"
    if normalized == "builtin" or normalized.startswith("official/") or normalized == "official":
        return "builtin"
    for trusted in TRUSTED_REPOS:
        if normalized == trusted or normalized.startswith(f"{trusted}/"):
            return "trusted"
    return "community"


def should_allow_install(
    result: ScanResult, force: bool = False
) -> tuple[bool | None, str]:
    """Return ``(decision, reason)``.

    ``decision`` is:
    - ``True`` → allow
    - ``False`` → block (use ``force=True`` to override)
    - ``None`` → ``ask`` — the caller should prompt the user before installing.

    ``reason`` is a single human-readable line describing the decision.
    """
    policy = INSTALL_POLICY.get(result.trust_level, INSTALL_POLICY["community"])
    vi = _VERDICT_INDEX.get(result.verdict, 2)
    decision = policy[vi]

    if decision == "allow":
        return True, f"Allowed ({result.trust_level} source, {result.verdict} verdict)"

    if force:
        return True, (
            f"Force-installed despite {result.verdict} verdict "
            f"({len(result.findings)} findings)"
        )

    if decision == "ask":
        return None, (
            f"Requires confirmation ({result.trust_level} source + "
            f"{result.verdict} verdict, {len(result.findings)} findings)"
        )

    return False, (
        f"Blocked ({result.trust_level} source + {result.verdict} verdict, "
        f"{len(result.findings)} findings). Use --force to override."
    )


def format_scan_report(result: ScanResult) -> str:
    """Human-readable multi-line report — for CLI output / chat replies."""
    lines: list[str] = [
        f"Scan: {result.skill_name} ({result.source}/{result.trust_level})  "
        f"Verdict: {result.verdict.upper()}"
    ]

    if result.findings:
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        sorted_findings = sorted(
            result.findings, key=lambda f: order.get(f.severity, 4)
        )
        for f in sorted_findings:
            sev = f.severity.upper().ljust(8)
            cat = f.category.ljust(14)
            pid = f.pattern_id.ljust(28)
            loc = f"{f.file}:{f.line}".ljust(24)
            excerpt = f.match[:60]
            lines.append(f'  {sev} {cat} {pid} {loc} "{excerpt}"')
        lines.append("")

    allowed, reason = should_allow_install(result)
    if allowed is True:
        status = "ALLOWED"
    elif allowed is None:
        status = "NEEDS CONFIRMATION"
    else:
        status = "BLOCKED"
    lines.append(f"Decision: {status} — {reason}")

    return "\n".join(lines)


__all__ = [
    "INSTALL_POLICY",
    "PolicyDecision",
    "TRUSTED_REPOS",
    "format_scan_report",
    "resolve_trust_level",
    "should_allow_install",
]
