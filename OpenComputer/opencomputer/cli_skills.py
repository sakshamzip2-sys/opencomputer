"""``opencomputer skill[s]`` CLI surface.

This module hosts two Typer apps that intentionally cohabit one file:

1. ``skill_app`` (singular) — Skills Guard interface
   (``opencomputer skill scan <path>``). Operates on a single skill.

2. ``app`` (plural ``skills``) — Auto-skill-evolution review surface
   (``opencomputer skills {list,review,accept,reject,evolution}``).
   Manages the lifecycle of *proposed* skills produced by the auto-evolution
   subscriber and the skills the user has already activated.

Privacy contract for ``skills evolution status``
------------------------------------------------
The ``status`` subcommand is **aggregate-only**. Tests in
``tests/test_skill_evolution_cli.py`` enforce that it never echoes:

* specific session IDs,
* specific app names,
* specific proposed-skill names,
* skill content / transcripts.

Counts and timing deltas are fine. The motivating threat model: a user
glancing at their terminal mid-day shouldn't accidentally reveal that a
sensitive session produced a skill named ``auto-bank-login-flow``.

The ``list`` subcommand is allowed to show proposed-skill *names* and
*descriptions* (those are user-facing affordances), but never
``provenance.session_id``. ``review`` may show the full body because the
user themselves drove the request.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import types
from pathlib import Path

import typer
from rich.console import Console

from opencomputer.skills_guard import (
    format_scan_report,
    scan_skill,
    should_allow_install,
)

console = Console()


# ─── singular ``skill`` namespace — Skills Guard ────────────────────────────

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


# ─── plural ``skills`` namespace — auto-skill-evolution ─────────────────────

app = typer.Typer(
    name="skills",
    help="Skill catalog & auto-evolution controls (list, review, accept, reject).",
    no_args_is_help=True,
)

evolution_app = typer.Typer(
    name="evolution",
    help="Toggle and inspect the auto-skill-evolution subscriber.",
    no_args_is_help=True,
)
app.add_typer(evolution_app, name="evolution")


# ── extensions.skill_evolution alias bootstrap ──────────────────────────────
#
# The plugin lives at ``extensions/skill-evolution/`` (hyphenated). Python
# module names use underscores, so we register a synthetic namespace package
# pointing at the hyphenated dir on first import. tests/conftest.py also
# does this for the test runner; this helper makes the same alias available
# when the CLI is invoked outside pytest.

def _ensure_skill_evolution_alias() -> None:
    if "extensions.skill_evolution.candidate_store" in sys.modules:
        return
    project_root = Path(__file__).resolve().parent.parent
    ext_dir = project_root / "extensions"
    se_dir = ext_dir / "skill-evolution"
    if not se_dir.exists():
        return
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(ext_dir)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg
    if "extensions.skill_evolution" not in sys.modules:
        mod = types.ModuleType("extensions.skill_evolution")
        mod.__path__ = [str(se_dir)]
        mod.__package__ = "extensions.skill_evolution"
        sys.modules["extensions.skill_evolution"] = mod
        sys.modules["extensions"].skill_evolution = mod  # type: ignore[attr-defined]
    parent = sys.modules["extensions.skill_evolution"]
    for sub in ("pattern_detector", "skill_extractor", "candidate_store", "subscriber"):
        full_name = f"extensions.skill_evolution.{sub}"
        if full_name in sys.modules:
            setattr(parent, sub, sys.modules[full_name])
            continue
        init = se_dir / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.skill_evolution"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)
        setattr(parent, sub, sub_mod)


def _profile_home() -> Path:
    env = os.environ.get("OPENCOMPUTER_PROFILE_HOME")
    if env:
        return Path(env)
    from opencomputer.agent.config import _home  # lazy: avoid import cycles

    return _home()


_STATE_FILENAME = "evolution_state.json"
_HEARTBEAT_FILENAME = "evolution_heartbeat"


def _state_path() -> Path:
    return _profile_home() / "skills" / _STATE_FILENAME


def _heartbeat_path() -> Path:
    return _profile_home() / "skills" / _HEARTBEAT_FILENAME


def _read_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _format_relative(ts: float, now: float | None = None) -> str:
    if not ts:
        return "never"
    delta = (now if now is not None else time.time()) - ts
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{delta:.0f}s ago"
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


def _read_active_skills(profile_home: Path) -> list[tuple[str, str]]:
    """Return ``[(name, description), ...]`` for active (non-proposed) skills.

    Active skills live directly under ``<profile_home>/skills/`` (any dir
    not starting with ``_`` or ``.``). Description is parsed from the YAML
    frontmatter of ``SKILL.md``; missing frontmatter yields an empty string.
    """
    skills_dir = profile_home / "skills"
    out: list[tuple[str, str]] = []
    if not skills_dir.is_dir():
        return out
    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith(("_", ".")):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue
        description = _parse_skill_description(skill_md)
        out.append((child.name, description))
    return out


def _parse_skill_description(skill_md: Path) -> str:
    """Extract ``description:`` from YAML frontmatter; ``""`` if absent."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    # Find closing ---
    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        return ""
    fm = rest[:end]
    for line in fm.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("description:"):
            return stripped.split(":", 1)[1].strip()
    return ""


# ── ``skills list`` ────────────────────────────────────────────────────────


@app.command("list")
def list_skills() -> None:
    """List active skills and proposed candidates.

    Proposals are clearly labelled. Provenance.session_id is intentionally
    omitted (it's PII-ish) — only name + description + confidence are shown.
    """
    _ensure_skill_evolution_alias()
    from extensions.skill_evolution.candidate_store import list_candidates

    profile_home = _profile_home()

    active = _read_active_skills(profile_home)
    proposed = list_candidates(profile_home)

    typer.echo("active skills:")
    if not active:
        typer.echo("  (none)")
    else:
        for name, description in active:
            if description:
                typer.echo(f"  - {name}: {description}")
            else:
                typer.echo(f"  - {name}")

    typer.echo("")
    typer.echo("proposed skills (auto-generated, awaiting review):")
    if not proposed:
        typer.echo("  (none)")
    else:
        for cand in proposed:
            line = f"  [proposed] {cand.name}"
            if cand.description:
                line += f": {cand.description}"
            if cand.confidence_score:
                line += f"  (confidence={cand.confidence_score})"
            typer.echo(line)


# ── ``skills accept`` ─────────────────────────────────────────────────────


@app.command("accept")
def accept(
    name: str = typer.Argument(..., help="Proposed skill name to accept."),
) -> None:
    """Promote a proposed skill to active by moving it out of ``_proposed/``."""
    _ensure_skill_evolution_alias()
    from extensions.skill_evolution.candidate_store import accept_candidate

    profile_home = _profile_home()
    try:
        dest = accept_candidate(profile_home, name)
    except FileNotFoundError:
        typer.echo(f"error: no proposed skill named {name!r}", err=True)
        raise typer.Exit(code=1) from None
    except FileExistsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"accepted: {name} → {dest}")


# ── ``skills reject`` ─────────────────────────────────────────────────────


@app.command("reject")
def reject(
    name: str = typer.Argument(..., help="Proposed skill name to reject."),
) -> None:
    """Delete a proposed skill from ``_proposed/``."""
    _ensure_skill_evolution_alias()
    from extensions.skill_evolution.candidate_store import reject_candidate

    profile_home = _profile_home()
    if not reject_candidate(profile_home, name):
        typer.echo(f"error: no proposed skill named {name!r}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"rejected: {name}")


# ── ``skills review`` ─────────────────────────────────────────────────────


_REVIEW_PROMPT = "Accept (a) / Reject (r) / Skip (s) / View body (v) / Quit (q)"


@app.command("review")
def review() -> None:
    """Walk through proposed skills interactively (newest first).

    User-initiated; the body of each proposal can be shown on demand
    (``v``). Accept/reject/skip/quit map to the candidate-store primitives.
    """
    _ensure_skill_evolution_alias()
    from extensions.skill_evolution.candidate_store import (
        accept_candidate,
        get_candidate,
        list_candidates,
        reject_candidate,
    )

    profile_home = _profile_home()
    candidates = list_candidates(profile_home)
    if not candidates:
        typer.echo("no proposed skills awaiting review.")
        return

    typer.echo(f"reviewing {len(candidates)} proposed skill(s) (newest first):")
    typer.echo("")

    for cand in candidates:
        typer.echo(f"── {cand.name} ──")
        if cand.description:
            typer.echo(f"  description: {cand.description}")
        if cand.confidence_score:
            typer.echo(f"  confidence: {cand.confidence_score}")
        if cand.generated_at:
            typer.echo(f"  generated: {_format_relative(cand.generated_at)}")
        typer.echo("")

        # Prompt loop — re-prompt on bad input or after a `v` view.
        while True:
            choice = typer.prompt(
                _REVIEW_PROMPT, default="s", show_default=False
            ).strip().lower()
            if choice in ("a", "accept"):
                try:
                    dest = accept_candidate(profile_home, cand.name)
                    typer.echo(f"  accepted → {dest}")
                except (FileExistsError, FileNotFoundError) as exc:
                    typer.echo(f"  error: {exc}")
                break
            if choice in ("r", "reject"):
                if reject_candidate(profile_home, cand.name):
                    typer.echo("  rejected.")
                else:
                    typer.echo("  error: candidate not found (already removed?)")
                break
            if choice in ("s", "skip"):
                typer.echo("  skipped.")
                break
            if choice in ("v", "view"):
                full = get_candidate(profile_home, cand.name)
                if full is None:
                    typer.echo("  error: candidate body unreadable")
                else:
                    typer.echo("  ───── body ─────")
                    for line in full.body.splitlines():
                        typer.echo(f"  {line}")
                    typer.echo("  ────────────────")
                continue  # re-prompt after viewing
            if choice in ("q", "quit"):
                typer.echo("aborting review.")
                return
            typer.echo("  unknown choice; try a / r / s / v / q.")
        typer.echo("")


# ── ``skills evolution {on,off,status}`` ──────────────────────────────────


@evolution_app.command("on")
def evolution_on() -> None:
    """Enable the auto-skill-evolution subscriber.

    Writes ``<profile_home>/skills/evolution_state.json`` with
    ``{"enabled": true}``. The subscriber polls this file at event arrival
    time, so there's no daemon to restart.
    """
    state = _read_state()
    state["enabled"] = True
    _write_state(state)
    typer.echo("auto-skill-evolution: enabled.")
    typer.echo(
        "  Sessions ending with novel patterns may be staged as proposals."
    )
    typer.echo("  Run `opencomputer skills review` to walk through them.")


@evolution_app.command("off")
def evolution_off() -> None:
    """Disable the auto-skill-evolution subscriber."""
    state = _read_state()
    state["enabled"] = False
    _write_state(state)
    typer.echo("auto-skill-evolution: disabled.")


@evolution_app.command("status")
def evolution_status() -> None:
    """Show aggregate-only state.

    Privacy contract: this output never includes specific session IDs,
    app names, or proposed-skill names. Tests in
    ``tests/test_skill_evolution_cli.py`` enforce that contract.
    """
    _ensure_skill_evolution_alias()
    from extensions.skill_evolution.candidate_store import list_candidates

    state = _read_state()
    enabled = bool(state.get("enabled", False))
    if not enabled:
        typer.echo("auto-skill-evolution: disabled (feature is opt-in).")
        typer.echo("  (run `opencomputer skills evolution on` to enable.)")
        return

    profile_home = _profile_home()
    candidates = list_candidates(profile_home)
    typer.echo("auto-skill-evolution: enabled")
    typer.echo(f"proposed candidates: {len(candidates)}")

    hb = _heartbeat_path()
    if hb.exists():
        try:
            ts = float(hb.read_text(encoding="utf-8").strip())
            typer.echo(f"last heartbeat: {_format_relative(ts)}")
        except (OSError, ValueError):
            typer.echo("last heartbeat: unreadable")
    else:
        typer.echo("last heartbeat: never (no events observed yet)")


__all__ = ["app", "evolution_app", "skill_app"]
