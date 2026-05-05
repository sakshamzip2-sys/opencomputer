"""``oc optimize`` — find waste in OpenComputer's setup + workflow.

Inspired by codeburn's ``optimize`` command (different tool: codeburn
tracks cost across 18 AI coding tools; ``oc optimize`` is OC-specific).
Operates entirely on data OC already records — no external service.

Heuristics covered (priority-ordered):

1. **Re-read files**: same path read N times across multiple sessions
   when the content didn't materially change. Wastes input tokens.
2. **Low Read:Edit ratio**: sessions where Edit calls outnumber Read
   calls suggest blind edits → retry cycles. Wastes output tokens.
3. **Ghost skills**: skills under ``<profile_home>/skills/`` that have
   never been invoked in the last 30 days. Cost is the schema bytes
   shipped with every system prompt.
4. **Ghost agents**: same pattern for agents.
5. **Bloated context files**: ``SOUL.md`` / ``USER.md`` / ``MEMORY.md``
   over the 8 KB threshold. Loaded into every system prompt.
6. **High cache-write-to-read ratio**: cache-creation tokens vastly
   exceed cache-read tokens — most cache writes never hit. Suggests
   long-tail one-off requests rather than repeat workflows.

Each finding has an estimated token + USD saving and a copy-paste fix
when applicable. Findings rank by ``impact_tokens × urgency``. The
report concludes with an A-F grade.

Run::

    oc optimize                    # default: last 30 days
    oc optimize -p today           # today only
    oc optimize -p week            # last 7 days
    oc optimize --top 10           # show top N findings
    oc optimize --json             # machine-readable
    oc optimize --grade            # one-line A-F grade
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import typer

optimize_app = typer.Typer(help="Find waste in your OpenComputer setup + workflow.")

# Approximate Anthropic Sonnet 4.6 input pricing as a back-of-envelope
# default for token-to-USD conversion. The exact number varies by
# model; the goal here is order-of-magnitude triage, not billing.
USD_PER_INPUT_TOKEN = 3.0 / 1_000_000

# Threshold for bloated context files. Anything below this is fine.
BLOATED_CONTEXT_THRESHOLD_BYTES = 8 * 1024

# Cache write:read ratio worse than this is flagged.
WORST_CACHE_RATIO = 5.0


@dataclass
class Finding:
    """One optimization opportunity surfaced by a heuristic."""

    severity: str  # "HIGH" | "MED" | "LOW"
    category: str
    detail: str
    estimated_tokens_saved: int
    fix: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def estimated_usd_saved(self) -> float:
        return self.estimated_tokens_saved * USD_PER_INPUT_TOKEN

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "category": self.category,
            "detail": self.detail,
            "estimated_tokens_saved": self.estimated_tokens_saved,
            "estimated_usd_saved": round(self.estimated_usd_saved, 4),
            "fix": self.fix,
            "extra": self.extra,
        }


def _resolve_profile_home() -> Path:
    """Return the active profile home, mirroring oc's existing logic."""
    import os

    env = os.environ.get("OPENCOMPUTER_HOME", "").strip()
    if env:
        return Path(env)
    base = Path.home() / ".opencomputer"
    # Default profile lives at base/default if it exists, else base itself.
    default = base / "default"
    if default.is_dir():
        return default
    return base


def _open_sessions_db(profile_home: Path) -> sqlite3.Connection | None:
    candidates = [profile_home / "sessions.db", Path.home() / ".opencomputer" / "sessions.db"]
    for path in candidates:
        if path.is_file():
            conn = sqlite3.connect(str(path))
            conn.row_factory = sqlite3.Row
            return conn
    return None


def _period_to_seconds(period: str) -> float:
    now = time.time()
    if period == "today":
        return now - 86400
    if period == "week":
        return now - 7 * 86400
    if period == "30days" or period == "month":
        return now - 30 * 86400
    if period == "all":
        return 0
    raise typer.BadParameter(
        f"unknown period {period!r}. Use today / week / 30days / all."
    )


# ─── Heuristic 1: Re-read files ──────────────────────────────────────


def _heuristic_reread_files(
    conn: sqlite3.Connection, since: float
) -> list[Finding]:
    """Find paths read 5+ times across distinct sessions."""
    rows = conn.execute(
        "SELECT session_id, tool_calls FROM messages "
        "WHERE role = 'assistant' AND tool_calls IS NOT NULL "
        "AND timestamp >= ?",
        (since,),
    ).fetchall()

    path_sessions: dict[str, set[str]] = {}
    path_count: dict[str, int] = {}

    for row in rows:
        try:
            calls = json.loads(row["tool_calls"])
        except (TypeError, ValueError):
            continue
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = call.get("name", "")
            if name != "Read":
                continue
            args = call.get("arguments", {}) or {}
            path = str(args.get("file_path", "")).strip()
            if not path:
                continue
            path_sessions.setdefault(path, set()).add(row["session_id"])
            path_count[path] = path_count.get(path, 0) + 1

    findings: list[Finding] = []
    for path, sessions in path_sessions.items():
        n_reads = path_count[path]
        n_sessions = len(sessions)
        if n_reads < 5 or n_sessions < 2:
            continue
        # Estimate: the file probably averages ~3 KB each read; assume
        # 1 token ≈ 4 chars; saving (n_reads − 1) reads of that file.
        try:
            file_bytes = Path(path).stat().st_size
        except OSError:
            file_bytes = 3000
        est_tokens = max(0, (n_reads - 1) * file_bytes // 4)
        sev = "HIGH" if est_tokens > 50_000 else "MED" if est_tokens > 5_000 else "LOW"
        findings.append(
            Finding(
                severity=sev,
                category="reread_file",
                detail=(
                    f"{path} read {n_reads}× across {n_sessions} sessions "
                    f"(~{file_bytes} bytes/read)"
                ),
                estimated_tokens_saved=est_tokens,
                fix=(
                    "Pin to the system prompt or cache via "
                    "`oc skills add` / a CLAUDE.md anchor so subsequent "
                    "sessions don't re-read it."
                ),
                extra={"path": path, "reads": n_reads, "sessions": n_sessions},
            )
        )
    return findings


# ─── Heuristic 2: Low Read:Edit ratio ────────────────────────────────


def _heuristic_low_read_edit(
    conn: sqlite3.Connection, since: float
) -> list[Finding]:
    rows = conn.execute(
        "SELECT session_id, tool_calls FROM messages "
        "WHERE role = 'assistant' AND tool_calls IS NOT NULL "
        "AND timestamp >= ?",
        (since,),
    ).fetchall()

    by_session: dict[str, dict[str, int]] = {}
    for row in rows:
        try:
            calls = json.loads(row["tool_calls"])
        except (TypeError, ValueError):
            continue
        if not isinstance(calls, list):
            continue
        sess = row["session_id"]
        bucket = by_session.setdefault(sess, {"Read": 0, "Edit": 0})
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = call.get("name", "")
            if name == "Read":
                bucket["Read"] += 1
            elif name in ("Edit", "MultiEdit", "Write"):
                bucket["Edit"] += 1

    findings: list[Finding] = []
    bad_sessions = 0
    total_excess_edits = 0
    for sess, c in by_session.items():
        if c["Edit"] < 3:
            continue  # too few edits to draw a conclusion
        if c["Read"] >= c["Edit"]:
            continue
        bad_sessions += 1
        total_excess_edits += c["Edit"] - c["Read"]

    if bad_sessions >= 3:
        # Each blind edit retry ≈ 200 output tokens wasted (rough).
        est_tokens = total_excess_edits * 200
        sev = "MED" if bad_sessions >= 10 else "LOW"
        findings.append(
            Finding(
                severity=sev,
                category="low_read_edit_ratio",
                detail=(
                    f"{bad_sessions} session(s) edited more than they read "
                    f"({total_excess_edits} excess Edit calls)"
                ),
                estimated_tokens_saved=est_tokens,
                fix=(
                    "Reinforce the 'Read before Edit' workflow rule in "
                    "your active profile's SOUL.md or skills."
                ),
                extra={
                    "bad_sessions": bad_sessions,
                    "excess_edits": total_excess_edits,
                },
            )
        )
    return findings


# ─── Heuristic 3 & 4: Ghost skills and agents ────────────────────────


def _heuristic_ghost_dir(
    conn: sqlite3.Connection,
    profile_home: Path,
    since: float,
    *,
    dirname: str,
    tool_name_match: tuple[str, ...],
    category: str,
) -> list[Finding]:
    target_dir = profile_home / dirname
    if not target_dir.is_dir():
        return []
    candidates = sorted(p.name for p in target_dir.iterdir() if p.is_dir() or p.suffix == ".md")
    if not candidates:
        return []

    rows = conn.execute(
        "SELECT tool_calls FROM messages "
        "WHERE role = 'assistant' AND tool_calls IS NOT NULL "
        "AND timestamp >= ?",
        (since,),
    ).fetchall()

    invoked: set[str] = set()
    for row in rows:
        try:
            calls = json.loads(row["tool_calls"])
        except (TypeError, ValueError):
            continue
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("name", "") in tool_name_match:
                args = call.get("arguments", {}) or {}
                # Skill / Delegate / Agent typically pass the target name
                # via 'skill' / 'agent_type' / 'subagent_type' / 'name'.
                for key in ("skill", "agent_type", "subagent_type", "name"):
                    val = args.get(key)
                    if isinstance(val, str) and val:
                        invoked.add(val)

    ghosts = [c for c in candidates if c not in invoked and c.removesuffix(".md") not in invoked]
    if not ghosts:
        return []

    # Each unused skill/agent ships ~150 tokens of schema overhead per
    # session. Estimate against last 30 days of session count.
    sessions_in_window = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE started_at >= ?",
        (since,),
    ).fetchone()["n"]
    est_tokens = len(ghosts) * 150 * sessions_in_window
    sev = "MED" if est_tokens > 50_000 else "LOW"
    fix_cmd = "oc skills disable" if dirname == "skills" else "oc agents remove"
    return [
        Finding(
            severity=sev,
            category=category,
            detail=(
                f"{len(ghosts)} unused {dirname[:-1]}(s) (last {int((time.time() - since) / 86400)}d): "
                f"{', '.join(ghosts[:5])}{'...' if len(ghosts) > 5 else ''}"
            ),
            estimated_tokens_saved=est_tokens,
            fix=f"{fix_cmd} <name> for each one you don't need.",
            extra={"unused": ghosts, "sessions_in_window": sessions_in_window},
        )
    ]


# ─── Heuristic 5: Bloated context files ──────────────────────────────


def _heuristic_bloated_context(profile_home: Path) -> list[Finding]:
    findings: list[Finding] = []
    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        path = profile_home / name
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size <= BLOATED_CONTEXT_THRESHOLD_BYTES:
            continue
        excess_bytes = size - BLOATED_CONTEXT_THRESHOLD_BYTES
        # Loaded into every system prompt: assume ~50 sessions/month.
        est_tokens = (excess_bytes // 4) * 50
        sev = "MED" if excess_bytes > 16 * 1024 else "LOW"
        findings.append(
            Finding(
                severity=sev,
                category="bloated_context",
                detail=(
                    f"{name} is {size // 1024} KB ({excess_bytes // 1024} KB "
                    f"over the {BLOATED_CONTEXT_THRESHOLD_BYTES // 1024} KB threshold)"
                ),
                estimated_tokens_saved=est_tokens,
                fix=(
                    f"Trim {name} — move historical context to a wiki note "
                    f"(WikiMemoryAdd) or split by topic with explicit re-imports."
                ),
                extra={"path": str(path), "bytes": size},
            )
        )
    return findings


# ─── Heuristic 6: Cache write-to-read ratio ─────────────────────────


def _heuristic_cache_overhead(
    conn: sqlite3.Connection, since: float
) -> list[Finding]:
    row = conn.execute(
        "SELECT SUM(cache_write_tokens) AS w, SUM(cache_read_tokens) AS r "
        "FROM sessions WHERE started_at >= ?",
        (since,),
    ).fetchone()
    write = row["w"] or 0
    read = row["r"] or 0
    if write < 10_000 or read < 1:
        return []  # not enough signal
    ratio = write / max(read, 1)
    if ratio < WORST_CACHE_RATIO:
        return []
    # Each cache write is paid input × 1.25 (Anthropic). Wasted writes
    # = writes that produced no reads. Approximate: writes − reads.
    est_tokens = max(0, write - read)
    sev = "MED" if ratio > 10 else "LOW"
    return [
        Finding(
            severity=sev,
            category="cache_overhead",
            detail=(
                f"Cache write:read ratio is {ratio:.1f}:1 over "
                f"{int((time.time() - since) / 86400)}d "
                f"(write={write:,}, read={read:,} tokens)"
            ),
            estimated_tokens_saved=est_tokens,
            fix=(
                "Either: shorten system prompt (less to cache per turn), "
                "increase cache TTL via provider config, or accept that "
                "your workload is too one-off for cache to pay back."
            ),
            extra={"write_tokens": write, "read_tokens": read, "ratio": round(ratio, 2)},
        )
    ]


# ─── Aggregation + grading ───────────────────────────────────────────


def _grade(findings: list[Finding]) -> str:
    """A-F based on weighted severity sum."""
    weight = {"HIGH": 3, "MED": 1, "LOW": 0.3}
    score = sum(weight.get(f.severity, 0) for f in findings)
    if score == 0:
        return "A"
    if score < 2:
        return "B"
    if score < 5:
        return "C"
    if score < 10:
        return "D"
    if score < 20:
        return "E"
    return "F"


def _all_findings(profile_home: Path, since: float) -> list[Finding]:
    findings: list[Finding] = []
    findings.extend(_heuristic_bloated_context(profile_home))

    conn = _open_sessions_db(profile_home)
    if conn is None:
        return findings
    try:
        findings.extend(_heuristic_reread_files(conn, since))
        findings.extend(_heuristic_low_read_edit(conn, since))
        findings.extend(
            _heuristic_ghost_dir(
                conn,
                profile_home,
                since,
                dirname="skills",
                tool_name_match=("Skill",),
                category="ghost_skill",
            )
        )
        findings.extend(
            _heuristic_ghost_dir(
                conn,
                profile_home,
                since,
                dirname="agents",
                tool_name_match=("Delegate", "Agent", "Task"),
                category="ghost_agent",
            )
        )
        findings.extend(_heuristic_cache_overhead(conn, since))
    finally:
        conn.close()

    # Rank by token savings, then severity.
    sev_rank = {"HIGH": 0, "MED": 1, "LOW": 2}
    findings.sort(
        key=lambda f: (sev_rank.get(f.severity, 99), -f.estimated_tokens_saved)
    )
    return findings


# ─── CLI entry ───────────────────────────────────────────────────────


@optimize_app.callback(invoke_without_command=True)
def optimize_command(
    period: str = typer.Option("30days", "--period", "-p", help="today | week | 30days | all"),
    top: int = typer.Option(0, "--top", help="Show only top N findings (0 = all)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    grade_only: bool = typer.Option(False, "--grade", help="Print one-line A-F grade only."),
):
    """Find waste in your OpenComputer setup + workflow."""
    profile_home = _resolve_profile_home()
    since = _period_to_seconds(period)
    findings = _all_findings(profile_home, since)
    if top > 0:
        findings = findings[:top]
    grade = _grade(findings)

    if grade_only:
        typer.echo(f"oc optimize grade: {grade} ({len(findings)} finding(s))")
        return

    if json_output:
        payload = {
            "grade": grade,
            "period": period,
            "profile_home": str(profile_home),
            "findings": [f.to_dict() for f in findings],
        }
        typer.echo(json.dumps(payload, indent=2, default=str))
        return

    if not findings:
        typer.echo(f"Grade: {grade} — no waste detected in the last {period}.")
        return

    typer.echo(f"Grade: {grade} ({len(findings)} finding(s) over {period})\n")
    for i, f in enumerate(findings, start=1):
        usd = f"${f.estimated_usd_saved:.4f}" if f.estimated_usd_saved >= 0.0001 else "≈ $0"
        typer.echo(
            f"{i}. [{f.severity}] {f.category}\n"
            f"   → {f.detail}\n"
            f"   → Save: ~{f.estimated_tokens_saved:,} tokens ({usd})"
        )
        if f.fix:
            typer.echo(f"   → Fix: {f.fix}")
        typer.echo("")
