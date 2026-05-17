"""Gateway-vs-CLI intelligence-parity telemetry probe.

M1 of ``docs/superpowers/specs/2026-05-17-gateway-vs-cli-parity/PLAN.md``.

The CLI and the gateway construct the *same* :class:`AgentLoop`, but ten
mechanisms quietly make a gateway turn behave differently from a CLI turn
(routing prompt-override, tool allowlist, reply truncation, …). This
module is the single seam that records — per gateway turn — which of
those ten mechanisms fired, into the ``gateway_parity_log`` table of the
profile's ``audit.db`` (schema v21).

Design:

* :data:`MECHANISMS` is the canonical catalogue. It is the *only* place
  the ten mechanism ids + severity weights live; both the dispatcher
  instrumentation and ``oc gateway diagnose`` import it.
* The dispatcher builds one :class:`ParityProbe` per turn, calls
  :meth:`ParityProbe.observe` as each mechanism is evaluated, and
  :meth:`ParityProbe.flush` once at turn-end. Flush writes **all ten**
  rows in a single transaction — unobserved mechanisms land as
  ``fired=0`` so the rollup denominator is a clean turn-count.
* Writes are best-effort: a SQLite failure is logged at WARNING and
  swallowed (the three-tier-swallow contract — telemetry must never
  wedge the agent loop).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("opencomputer.gateway.parity_probe")


# ── Mechanism catalogue ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Mechanism:
    """One parity-affecting mechanism.

    ``severity`` is the calibrated impact weight from the ANALYSIS.md
    ranking — 4 = CRITICAL, 3 = HIGH, 2 = MEDIUM. It multiplies the
    measured fire-rate to produce the M2 prioritisation score, so the
    top-3 picked for M3 reflect impact × frequency, not frequency alone.
    """

    id: str
    label: str
    severity: int
    description: str


#: Canonical ten-mechanism catalogue. Order matches ANALYSIS.md §"The 10
#: mechanisms"; ``id`` values are the stable enum stored in the DB.
MECHANISMS: tuple[Mechanism, ...] = (
    Mechanism(
        "prompt_override",
        "system_prompt_override wipes the PromptBuilder",
        4,
        "A routing rule supplied a system prompt, so declarative / skills "
        "/ memory / SOUL injection was switched off for this turn.",
    ),
    Mechanism(
        "tool_allowlist",
        "Tool allowlist via enabled_plugins",
        3,
        "The gateway loop was built with a non-wildcard allowed_tools set; "
        "the CLI never restricts tools.",
    ),
    Mechanism(
        "reply_truncation",
        "Reply truncated to the platform message cap",
        3,
        "The outgoing reply exceeded the adapter's max_message_length and "
        "was cut by truncate_smart.",
    ),
    Mechanism(
        "channel_prompt_overlay",
        "Per-channel prompt + skills overlay",
        3,
        "_build_channel_runtime injected a channel-scoped prompt and/or "
        "skill bodies, separate from routing rules.",
    ),
    Mechanism(
        "no_interactive_consent",
        "No interactive consent (button round-trip)",
        2,
        "Gateway sessions cannot prompt for tool approval synchronously; "
        "consent goes through an async button/text round-trip.",
    ),
    Mechanism(
        "profile_rebind",
        "Profile rebind switched memory / model",
        3,
        "A bindings or routing rule rebound this turn to a different "
        "profile, with its own MEMORY/USER/SOUL and model.",
    ),
    Mechanism(
        "persona_casual_register",
        "Persona classifier picked a casual register",
        2,
        "The gateway turn carries a chat agent_context; the persona "
        "overlay leans casual relative to a CLI task session.",
    ),
    Mechanism(
        "routing_decision_invisible",
        "Routing fired but no chat-visible surface showed it",
        2,
        "A binding/routing rule matched and changed behaviour, but the "
        "user saw no badge explaining which rule fired.",
    ),
    Mechanism(
        "runtime_footer_off",
        "runtime_footer disabled — model/context not visible",
        2,
        "display.runtime_footer.enabled is false, so the reply carries no "
        "model · context% · cwd line.",
    ),
    Mechanism(
        "compaction_long_session",
        "Compaction ran on this long-lived session",
        2,
        "CompactionEngine summarised earlier history this turn; long "
        "gateway sessions lose early context CLI sessions still hold.",
    ),
)

_BY_ID: dict[str, Mechanism] = {m.id: m for m in MECHANISMS}
MECHANISM_IDS: frozenset[str] = frozenset(_BY_ID)


def mechanism_label(mechanism_id: str) -> str:
    """Human-readable label for ``mechanism_id`` (or the id itself if unknown)."""
    m = _BY_ID.get(mechanism_id)
    return m.label if m is not None else mechanism_id


# ── ParityProbe — per-turn accumulator ────────────────────────────────


class ParityProbe:
    """Accumulates per-mechanism observations for one gateway turn.

    The dispatcher constructs one of these per turn, calls
    :meth:`observe` as it evaluates each mechanism, then :meth:`flush`
    exactly once. A mechanism that is never observed flushes as
    ``fired=0`` — "evaluated, did not fire".
    """

    __slots__ = ("session_id", "turn_id", "platform", "_obs")

    def __init__(self, *, session_id: str, turn_id: int, platform: str) -> None:
        self.session_id = session_id
        self.turn_id = int(turn_id)
        self.platform = platform or "unknown"
        self._obs: dict[str, tuple[bool, dict]] = {}

    def observe(
        self,
        mechanism_id: str,
        fired: bool,
        detail: dict | None = None,
    ) -> None:
        """Record whether ``mechanism_id`` fired this turn.

        Raises :class:`ValueError` on an unknown mechanism id — a typo
        in instrumentation should fail loudly in tests, not silently
        drop telemetry in production.
        """
        if mechanism_id not in MECHANISM_IDS:
            raise ValueError(
                f"unknown parity mechanism {mechanism_id!r}; "
                f"valid ids: {sorted(MECHANISM_IDS)}"
            )
        self._obs[mechanism_id] = (bool(fired), dict(detail or {}))

    @property
    def pending(self) -> dict[str, tuple[bool, dict]]:
        """A copy of the observations recorded so far (for tests/inspection)."""
        return dict(self._obs)

    def flush(self, audit_db_path: Path | str) -> int:
        """Write all ten mechanism rows for this turn. Returns rows written.

        Best-effort: returns 0 (and logs WARNING) on any SQLite failure.
        """
        return record_parity_observations(
            audit_db_path,
            session_id=self.session_id,
            turn_id=self.turn_id,
            platform=self.platform,
            observations=self._obs,
        )


# ── Writer ────────────────────────────────────────────────────────────


def record_parity_observations(
    audit_db_path: Path | str,
    *,
    session_id: str,
    turn_id: int,
    platform: str,
    observations: dict[str, tuple[bool, dict]],
) -> int:
    """Append one row per mechanism to ``gateway_parity_log``.

    Always writes exactly ``len(MECHANISMS)`` rows: any mechanism absent
    from ``observations`` is written as ``fired=0`` with an empty detail.
    This keeps the rollup denominator a clean per-mechanism turn-count.

    Mirrors :func:`opencomputer.agent.loop_safety.record_loop_trip`:
    runs :func:`apply_migrations` so a legacy ``audit.db`` self-heals to
    v21, then batch-inserts in one transaction. Best-effort — a SQLite
    failure is logged at WARNING and swallowed; returns rows written
    (``len(MECHANISMS)`` on success, ``0`` on failure).
    """
    try:
        from opencomputer.agent.state import apply_migrations

        ts = time.time()
        rows = []
        for m in MECHANISMS:
            fired, detail = observations.get(m.id, (False, {}))
            rows.append(
                (
                    ts,
                    session_id,
                    int(turn_id),
                    platform,
                    m.id,
                    1 if fired else 0,
                    json.dumps(detail, default=str, separators=(",", ":")),
                )
            )
        conn = sqlite3.connect(audit_db_path)
        try:
            apply_migrations(conn)
            conn.executemany(
                "INSERT INTO gateway_parity_log "
                "(ts, session_id, turn_id, platform, mechanism_id, fired, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        return len(rows)
    except (sqlite3.Error, OSError):
        logger.warning(
            "gateway parity telemetry: failed to write to audit.db at %s "
            "(dispatch continues unaffected)",
            audit_db_path,
            exc_info=True,
        )
        return 0


# ── Readers ───────────────────────────────────────────────────────────


def query_parity_log(
    audit_db_path: Path | str,
    *,
    session_id: str | None = None,
    since: float | None = None,
    limit: int = 200,
) -> list[dict]:
    """Return ``gateway_parity_log`` rows newest-first, filtered + decoded.

    ``detail`` is JSON-decoded back into a dict; ``fired`` is a bool.
    A missing DB / missing table yields an empty list (the table only
    exists once the gateway has run at least once under v21).
    """
    # Guard before connect: ``sqlite3.connect`` on a missing path would
    # CREATE an empty DB — a read-only query must never have that side
    # effect. (Plain connect avoids the file: URI's space-encoding trap.)
    if not Path(audit_db_path).exists():
        return []
    try:
        conn = sqlite3.connect(audit_db_path)
    except sqlite3.Error:
        return []
    try:
        sql = (
            "SELECT id, ts, session_id, turn_id, platform, mechanism_id, "
            "fired, detail FROM gateway_parity_log WHERE 1=1"
        )
        params: list[object] = []
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        try:
            raw = conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            # Table absent on a pre-v21 / never-run audit.db.
            return []
    finally:
        conn.close()
    out: list[dict] = []
    cols = ("id", "ts", "session_id", "turn_id", "platform", "mechanism_id")
    for r in raw:
        try:
            detail = json.loads(r[7]) if r[7] else {}
        except (json.JSONDecodeError, TypeError):
            detail = {}
        out.append(
            {
                **dict(zip(cols, r[:6], strict=True)),
                "fired": bool(r[6]),
                "detail": detail,
            }
        )
    return out


def rollup_parity_log(
    audit_db_path: Path | str,
    *,
    since: float | None = None,
) -> list[dict]:
    """Aggregate fire-rate + priority per mechanism, ordered by priority.

    Returns one dict per mechanism (always all ten — mechanisms with no
    telemetry land at zero) with keys ``mechanism_id``, ``label``,
    ``severity``, ``turns``, ``fired_count``, ``fire_rate`` and
    ``priority_score`` (= ``fire_rate * severity``). Ordered by
    ``priority_score`` descending — the head of this list is what M2
    locks in as the top-3 to fix.
    """
    counts: dict[str, tuple[int, int]] = {m.id: (0, 0) for m in MECHANISMS}
    # Guard before connect — never let a read create an empty audit.db.
    if Path(audit_db_path).exists():
        try:
            conn = sqlite3.connect(audit_db_path)
            try:
                sql = (
                    "SELECT mechanism_id, COUNT(*), SUM(fired) "
                    "FROM gateway_parity_log"
                )
                params: list[object] = []
                if since is not None:
                    sql += " WHERE ts >= ?"
                    params.append(since)
                sql += " GROUP BY mechanism_id"
                for mid, turns, fired in conn.execute(sql, params).fetchall():
                    if mid in counts:
                        counts[mid] = (int(turns or 0), int(fired or 0))
            except sqlite3.Error:
                pass  # table absent → all-zero rollup
            finally:
                conn.close()
        except sqlite3.Error:
            pass  # connect failed → all-zero rollup

    out: list[dict] = []
    for m in MECHANISMS:
        turns, fired_count = counts[m.id]
        fire_rate = (fired_count / turns) if turns else 0.0
        out.append(
            {
                "mechanism_id": m.id,
                "label": m.label,
                "severity": m.severity,
                "turns": turns,
                "fired_count": fired_count,
                "fire_rate": fire_rate,
                "priority_score": fire_rate * m.severity,
            }
        )
    out.sort(key=lambda r: r["priority_score"], reverse=True)
    return out


__all__ = [
    "MECHANISMS",
    "MECHANISM_IDS",
    "Mechanism",
    "ParityProbe",
    "mechanism_label",
    "query_parity_log",
    "record_parity_observations",
    "rollup_parity_log",
]
