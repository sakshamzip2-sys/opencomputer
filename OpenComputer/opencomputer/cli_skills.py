"""``opencomputer skill`` subcommand — Skills Guard interface.

Today: ``opencomputer skill scan <path>`` runs the static-analysis scanner
against a skill file or directory and prints a verdict + findings table.
Future: list / verify / install would land here as well.

Why a separate ``skill`` (singular) namespace when ``skills`` (plural)
already exists?

- ``opencomputer skills`` is a leaf command that lists installed skills.
  Refactoring it into a subapp would change the surface other docs already
  reference. Keeping the leaf command intact, we add ``skill`` (singular)
  for *operating on one skill at a time*: scan, in future verify/install.
- This also matches Hermes's CLI shape (``hermes skills install`` operates
  on one; ``hermes skills list`` enumerates) without forcing OC to mirror
  the exact verb tree.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from opencomputer.skills_guard import (
    format_scan_report,
    scan_skill,
    should_allow_install,
)

console = Console()

skill_app = typer.Typer(
    name="skill",
    help="Operate on one skill (scan, verify, install).",
    no_args_is_help=True,
)


@skill_app.command("scan")
def scan(
    path: Path = typer.Argument(
        ..., help="Path to a SKILL.md or skill directory.", exists=True,
    ),
    source: str = typer.Option(
        "community",
        "--source",
        "-s",
        help=(
            "Trust source for the scan. Default 'community' applies the "
            "strictest policy. Use 'builtin' to scan a bundled skill, "
            "'agent-created' for an evolution draft, or a known repo "
            "(e.g. 'openai/skills/code-review') to apply the trusted "
            "policy."
        ),
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit machine-readable JSON instead of report."
    ),
) -> None:
    """Scan a SKILL.md or skill directory for known threat patterns.

    Exit code:
    - 0 — verdict is allow / safe
    - 1 — verdict requires confirmation (agent-created + dangerous)
    - 2 — verdict is blocked
    """
    result = scan_skill(path, source=source)
    decision, reason = should_allow_install(result)

    if json_output:
        import json
        # Use plain ``print`` not ``console.print`` so the output is
        # JSON-parsable byte-for-byte. Rich wraps long strings and
        # injects soft-wrap control bytes that break ``json.loads``.
        print(json.dumps(_result_to_dict(result, decision, reason), indent=2))
    else:
        console.print(format_scan_report(result))

    if decision is True:
        raise typer.Exit(0)
    if decision is None:
        raise typer.Exit(1)
    raise typer.Exit(2)


def _result_to_dict(result, decision, reason: str) -> dict:
    return {
        "skill_name": result.skill_name,
        "source": result.source,
        "trust_level": result.trust_level,
        "verdict": result.verdict,
        "decision": (
            "allow" if decision is True else "ask" if decision is None else "block"
        ),
        "reason": reason,
        "scanned_at": result.scanned_at,
        "summary": result.summary,
        "findings": [
            {
                "pattern_id": f.pattern_id,
                "severity": f.severity,
                "category": f.category,
                "file": f.file,
                "line": f.line,
                "match": f.match,
                "description": f.description,
            }
            for f in result.findings
        ],
    }


__all__ = ["skill_app"]
