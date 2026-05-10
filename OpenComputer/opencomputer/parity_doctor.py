"""``oc parity-doctor`` — check OC parity against an upstream spec.

The companion to ``docs/OC-FROM-OPENCLAW.md``: parses the spec into a
list of features, runs a deterministic grep-based check per feature
against the live tree, and emits a colour-coded status table. Turning a
700-line markdown audit into a 30-line self-checking artifact.

Status semantics:

* ``shipped``     — every required symbol/file is present
* ``partial``     — some symbols present, others missing
* ``scaffolded``  — primitive present but full surface missing
* ``missing``     — no evidence the feature exists

The check registry is the canonical source of truth for which
features have been ported and which haven't. Adding a check here is
how you mark something "done at parity-doctor level".

This module deliberately performs **no** LLM calls — checks must be
fast (<1s for the whole report) and deterministic (CI-friendly).
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "FEATURE_CHECKS",
    "FeatureCheck",
    "FeatureRecord",
    "FeatureStatus",
    "parse_spec",
    "render_markdown",
    "run_checks",
]


FeatureStatus = Literal["shipped", "partial", "scaffolded", "missing"]


@dataclass(frozen=True, slots=True)
class FeatureRecord:
    """A single TIER N feature parsed out of the spec."""

    number: int
    title: str
    tier: int


@dataclass(frozen=True, slots=True)
class FeatureCheck:
    """Static check declaration for one spec feature.

    Each check declares the symbols / files we expect to see when the
    feature is shipped. The runner greps for them and computes status.

    Attributes:
        number:       1-based spec index (matches ``FeatureRecord.number``).
        title:        short label for output.
        symbols:      strings (regex) to grep for under ``opencomputer/``.
                      All must hit for ``shipped``.
        scaffolded_symbols: strings that, if found alone, downgrade the
                      status to ``scaffolded`` (a primitive landed but
                      not the full surface).
        notes:        prose printed alongside the status.
    """

    number: int
    title: str
    symbols: tuple[str, ...] = field(default_factory=tuple)
    scaffolded_symbols: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One row in the rendered table."""

    record: FeatureRecord
    status: FeatureStatus
    matched: tuple[str, ...]
    missing: tuple[str, ...]
    notes: str


# ─── spec parsing ─────────────────────────────────────────────────────


_TIER_RE = re.compile(r"^## TIER\s+(\d+)\b", re.MULTILINE)
_HEADING_RE = re.compile(r"^### (\d+)\.\s+(.+?)\s*$", re.MULTILINE)


def parse_spec(spec_path: Path) -> list[FeatureRecord]:
    """Parse ``docs/OC-FROM-OPENCLAW.md`` into ordered feature records."""
    text = spec_path.read_text(encoding="utf-8")
    # Find every "## TIER N" boundary and the position; anything between
    # boundary K and K+1 belongs to tier K.
    tier_positions: list[tuple[int, int]] = [
        (int(m.group(1)), m.start()) for m in _TIER_RE.finditer(text)
    ]
    if not tier_positions:
        return []
    tier_positions.append((-1, len(text)))  # sentinel
    out: list[FeatureRecord] = []
    for i in range(len(tier_positions) - 1):
        tier_n, start = tier_positions[i]
        end = tier_positions[i + 1][1]
        for m in _HEADING_RE.finditer(text, start, end):
            out.append(
                FeatureRecord(
                    number=int(m.group(1)),
                    title=m.group(2).strip(),
                    tier=tier_n,
                )
            )
    return out


# ─── grep harness ─────────────────────────────────────────────────────


# The parity-doctor source itself contains every check symbol as a
# string literal. We must exclude it from the scan or every check
# would match on its own definition.
_SELF_GLOB = "parity_doctor.py"
_SELF_GLOB_CLI = "cli_parity_doctor.py"


def _has_match(symbol: str, search_root: Path) -> bool:
    """Return True iff ``symbol`` is found anywhere under *search_root*.

    Uses ripgrep when available (fast); falls back to Python's regex
    walker if not. Either way, returns a plain bool — callers don't
    care which engine answered.

    Excludes the parity-doctor's own source files so check symbols
    don't self-match against their declarations in
    :data:`FEATURE_CHECKS`.
    """
    rg = _which_rg()
    if rg:
        try:
            proc = subprocess.run(
                [
                    rg, "--quiet", "--no-messages",
                    "--glob", f"!**/{_SELF_GLOB}",
                    "--glob", f"!**/{_SELF_GLOB_CLI}",
                    "-e", symbol, str(search_root),
                ],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            # Fall through to Python walker.
            pass
    return _python_grep(symbol, search_root)


_RG_NOT_PROBED = object()  # sentinel = not yet probed
_RG_PATH: str | None | object = _RG_NOT_PROBED


def _which_rg() -> str | None:
    """Cache the ripgrep lookup so repeated checks don't re-stat."""
    global _RG_PATH
    if _RG_PATH is _RG_NOT_PROBED:
        import shutil

        _RG_PATH = shutil.which("rg")
    return _RG_PATH  # type: ignore[return-value]


def _python_grep(symbol: str, root: Path) -> bool:
    """Pure-Python fallback when ripgrep isn't on PATH."""
    pattern = re.compile(symbol)
    for path in root.rglob("*.py"):
        if path.name in (_SELF_GLOB, _SELF_GLOB_CLI):
            continue
        try:
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


def _classify(
    matched: list[str],
    missing: list[str],
    scaffolded_hits: list[str],
) -> FeatureStatus:
    if matched and not missing:
        return "shipped"
    if matched and missing:
        return "partial"
    if scaffolded_hits and not matched:
        return "scaffolded"
    return "missing"


# ─── check registry ───────────────────────────────────────────────────


# Each entry mirrors a spec heading. Symbols are conservative: if a
# rename happens, the parity-doctor will mark partial/missing — that's
# the correct signal. False positives are far worse than a re-run.
FEATURE_CHECKS: tuple[FeatureCheck, ...] = (
    FeatureCheck(
        number=1,
        title="Heartbeat / Proactive Loop",
        symbols=("HEARTBEAT_LANE", "DEFAULT_HEARTBEAT_PROMPT"),
        notes="opencomputer/heartbeat.py — cron-backed lane",
    ),
    FeatureCheck(
        number=2,
        title="Model Failover Chain",
        symbols=("call_with_fallback", "call_with_provider_fallback"),
        notes="opencomputer/agent/fallback.py + fallback_provider_resolver.py",
    ),
    FeatureCheck(
        number=3,
        title="Structured Secrets Management (SecretRefs)",
        symbols=(
            "class SecretRef",
            "class EnvSecretProvider",
            "class ExecSecretProvider",
            "class SecretRegistry",
        ),
        scaffolded_symbols=("class SecretRef",),
        notes="plugin_sdk/wire_primitives.py + opencomputer/security/secrets.py + cli_secrets.py",
    ),
    FeatureCheck(
        number=4,
        title="Skill Requirements Gating (requires: frontmatter)",
        symbols=("class SkillRequirements", "_evaluate_skill_requirements"),
        notes="opencomputer/agent/memory.py — requires: { binaries, env, os, plugins }",
    ),
    FeatureCheck(
        number=5,
        title="Deterministic Session-to-Agent Binding",
        symbols=(
            "class AgentBinding",
            "agent_router",
        ),
        scaffolded_symbols=("agent_router",),
        notes="gateway/agent_router.py exists; full 8-tier priority chain not yet implemented",
    ),
    FeatureCheck(
        number=6,
        title="Lobster: Deterministic Workflow Pipelines",
        symbols=("class LobsterPipeline", "resumeToken"),
        notes="not implemented",
    ),
    FeatureCheck(
        number=7,
        title="Tool-Loop Detection",
        symbols=("class LoopDetector", "LoopAbortError"),
        notes="opencomputer/agent/loop_safety.py",
    ),
    FeatureCheck(
        number=8,
        title="Tokenjuice: Tool Result Compaction",
        symbols=("def compact_tool_result", "tokenjuice"),
        notes="not implemented",
    ),
    FeatureCheck(
        number=9,
        title="Trajectory Bundles (Session Flight Recorder)",
        # Matches the OpenClaw shape — a session-scoped flight recorder
        # writing events.jsonl + session-branch.json. OC's
        # ``export-trajectory`` evolution CLI is a *different* concept
        # (training data) so we deliberately don't match on that name.
        symbols=("class TrajectoryBundle", "session-branch.json"),
        notes="not implemented (OC evolution/export-trajectory is a different artifact)",
    ),
    FeatureCheck(
        number=10,
        title="Broadcast Groups",
        symbols=("broadcastGroups", "broadcast_groups"),
        notes="not implemented",
    ),
    FeatureCheck(
        number=11,
        title="Standing Orders",
        symbols=("class StandingOrder", "def load_standing_orders"),
        scaffolded_symbols=("standing_orders",),
        notes="opencomputer/agent/standing_orders.py",
    ),
    FeatureCheck(
        number=12,
        title="Thinking Levels (Per-Run Effort Control)",
        symbols=("class EffortLevel", "effort_policy"),
        scaffolded_symbols=("effort_policy",),
        notes="opencomputer/agent/effort_policy.py + reasoning_cmd.py",
    ),
    FeatureCheck(
        number=13,
        title="Steer: In-Flight Agent Redirection",
        symbols=("def cmd_steer", "queue_steer"),
        scaffolded_symbols=("steer",),
        notes="opencomputer/acp/session.py + cli_ui/slash_handlers.py",
    ),
    FeatureCheck(
        number=14,
        title="Exec Approvals (Granular, Per-Command)",
        symbols=("class ApprovalsConfig", "class CommandPattern"),
        scaffolded_symbols=("class ApprovalsConfig",),
        notes="opencomputer/security/approvals.py — mode-based; pattern-based per-command pending",
    ),
    FeatureCheck(
        number=15,
        title="ACP: External Harness Protocol",
        symbols=("class AcpServer", "class AcpSession"),
        scaffolded_symbols=("acp",),
        notes="opencomputer/acp/server.py — internal harness only; external spawn pending",
    ),
    FeatureCheck(
        number=16,
        title="Gateway Health Dashboard",
        symbols=("class DashboardServer", "/dashboard"),
        scaffolded_symbols=("dashboard",),
        notes="opencomputer/dashboard/ — health surface partial",
    ),
    FeatureCheck(
        number=17,
        title="Sandboxed Tool Execution",
        symbols=("class SandboxBackend", "docker"),
        scaffolded_symbols=("sandbox",),
        notes="opencomputer/sandbox/ — backend chain partial",
    ),
    FeatureCheck(
        number=18,
        title="Multi-Account Channel Support",
        # Looks for either a dedicated multi-account class or a per-channel
        # ``accounts`` map config dataclass — neither exists today.
        symbols=("class MultiAccountChannel", "ChannelAccountsConfig"),
        notes="single account per channel today",
    ),
    FeatureCheck(
        number=19,
        title="Plugin SDK for Channel Adapters",
        symbols=("class BaseChannelAdapter", "class ChannelAdapter"),
        notes="plugin_sdk/channel_contract.py",
    ),
    FeatureCheck(
        number=20,
        title="Context Pruning Modes",
        # OC ships ``ContextPruningConfig`` with ``mode`` ∈
        # {none, sliding, cache-ttl}; ``cache-ttl`` is the
        # discriminating symbol since it's a literal that won't
        # appear elsewhere in the codebase.
        symbols=("class ContextPruningConfig", '"cache-ttl"'),
        notes="opencomputer/agent/context_pruning.py — sliding + cache-ttl modes",
    ),
)


# ─── runner ───────────────────────────────────────────────────────────


def run_checks(
    *,
    spec_path: Path,
    repo_root: Path,
) -> list[CheckResult]:
    """Parse the spec and run every registered check.

    Returns one :class:`CheckResult` per spec feature; features that
    appear in the spec but have no registered check are returned as
    ``missing`` with empty matched/missing tuples and a stub note.
    """
    records = parse_spec(spec_path)
    by_number = {c.number: c for c in FEATURE_CHECKS}
    out: list[CheckResult] = []
    search_roots = [
        repo_root / "opencomputer",
        repo_root / "plugin_sdk",
        repo_root / "extensions",
    ]
    for record in records:
        check = by_number.get(record.number)
        if check is None:
            out.append(
                CheckResult(
                    record=record,
                    status="missing",
                    matched=(),
                    missing=(),
                    notes="no check registered yet",
                )
            )
            continue
        matched: list[str] = []
        missing: list[str] = []
        scaffolded_hits: list[str] = []
        for symbol in check.symbols:
            hit = any(_has_match(symbol, root) for root in search_roots if root.exists())
            (matched if hit else missing).append(symbol)
        for symbol in check.scaffolded_symbols:
            if any(_has_match(symbol, root) for root in search_roots if root.exists()):
                scaffolded_hits.append(symbol)
        status = _classify(matched, missing, scaffolded_hits)
        out.append(
            CheckResult(
                record=record,
                status=status,
                matched=tuple(matched),
                missing=tuple(missing),
                notes=check.notes,
            )
        )
    return out


# ─── markdown report ──────────────────────────────────────────────────


_STATUS_ICON = {
    "shipped": "✅",
    "partial": "🟡",
    "scaffolded": "🟠",
    "missing": "❌",
}


def render_markdown(results: Iterable[CheckResult]) -> str:
    """Format *results* as a Markdown table for piping into a report."""
    rows = list(results)
    lines: list[str] = [
        "| #  | Tier | Status | Feature | Notes |",
        "|----|------|--------|---------|-------|",
    ]
    for r in rows:
        icon = _STATUS_ICON[r.status]
        lines.append(
            f"| {r.record.number} | {r.record.tier} | {icon} {r.status} | "
            f"{r.record.title} | {r.notes or ''} |"
        )
    summary = _summary_line(rows)
    return "\n".join(lines) + "\n\n" + summary + "\n"


def _summary_line(results: list[CheckResult]) -> str:
    by_status: dict[str, int] = {"shipped": 0, "partial": 0, "scaffolded": 0, "missing": 0}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    total = len(results)
    parts = [f"{by_status[s]} {s}" for s in ("shipped", "partial", "scaffolded", "missing")]
    return f"**Total: {total}** — " + ", ".join(parts) + "."
