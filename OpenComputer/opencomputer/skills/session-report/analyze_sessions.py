#!/usr/bin/env python3
"""Analyze OpenComputer sessions and emit a JSON report.

Reads ``~/.opencomputer/<profile>/sessions.db`` for every profile (or just one
when ``--profile <name>`` is given) and produces the JSON shape consumed by
``template.html``: ``overall``, ``by_project`` (i.e. by source/platform),
``by_subagent_type``, ``by_skill``, ``cache_breaks``, ``top_prompts``.

Usage::

    python3 analyze_sessions.py [--since 7d|24h|all]
                                [--profile <name>]
                                [--out <path>]
                                [--top N]
                                [--cache-break N]

Defaults: ``--since 7d``, all profiles, ``--top 15``, ``--cache-break 100000``,
output to stdout.

The OC version differs from the Claude-Code one because OC stores sessions
in a SQLite ``SessionDB`` (one DB per profile), not as JSONL transcripts.
This means we have direct access to per-session token totals, model, source,
and ``compactions_count`` columns — no JSONL parsing or dedup logic needed.

Subagent transcripts live in ``subagents`` table when present (delegate
lineage). Skill invocations are derived from the ``tool_usage`` table.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# ─── CLI ────────────────────────────────────────────────────────────────────


def parse_since(value: str | None) -> float | None:
    """Convert ``7d`` / ``24h`` / ``all`` / None to a unix-seconds threshold."""
    if value is None or value == "all":
        return None
    if len(value) < 2:
        return None
    unit = value[-1]
    try:
        n = int(value[:-1])
    except ValueError:
        return None
    if unit == "d":
        return time.time() - n * 86400
    if unit == "h":
        return time.time() - n * 3600
    return None


def discover_profile_dbs(profile_filter: str | None = None) -> list[Path]:
    """Return ``[<profile-name>, sessions.db]`` for every profile on disk."""
    home = Path(os.environ.get("HOME", "~")).expanduser()
    base = home / ".opencomputer"
    if not base.exists():
        return []
    out: list[Path] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        # Skip non-profile dirs (logs, cache, …). A profile has sessions.db.
        db = child / "sessions.db"
        if not db.exists():
            continue
        if profile_filter and child.name != profile_filter:
            continue
        out.append(db)
    return out


# ─── DB queries ─────────────────────────────────────────────────────────────


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def query_sessions(
    conn: sqlite3.Connection, since: float | None
) -> list[sqlite3.Row]:
    """Return session rows (one per session) honoring the time window."""
    sql = "SELECT * FROM sessions"
    args: tuple[Any, ...] = ()
    if since is not None:
        sql += " WHERE started_at >= ?"
        args = (since,)
    sql += " ORDER BY started_at DESC"
    return list(conn.execute(sql, args))


def query_top_prompts(
    conn: sqlite3.Connection, since: float | None, n: int
) -> list[dict[str, Any]]:
    """Return the top-N user prompts by character length (proxy for cost)."""
    sql = (
        "SELECT m.session_id, m.content, m.timestamp, s.title, s.model "
        "FROM messages m LEFT JOIN sessions s ON s.id = m.session_id "
        "WHERE m.role = 'user'"
    )
    args: tuple[Any, ...] = ()
    if since is not None:
        sql += " AND m.timestamp >= ?"
        args = (since,)
    sql += " ORDER BY length(m.content) DESC LIMIT ?"
    args = args + (n,)
    out: list[dict[str, Any]] = []
    for row in conn.execute(sql, args):
        text = row["content"] or ""
        snippet = text[:200].replace("\n", " ")
        out.append(
            {
                "session": row["session_id"],
                "title": row["title"] or "",
                "model": row["model"] or "",
                "ts": row["timestamp"],
                "chars": len(text),
                "snippet": snippet,
            }
        )
    return out


def query_subagents(
    conn: sqlite3.Connection, since: float | None
) -> dict[str, dict[str, Any]]:
    """Return per-subagent-type aggregates if the ``subagents`` table exists.

    ``subagents`` is the delegate-lineage table (2026-05-10). Rows look like
    ``(session_id, parent_session_id, agent_type, started_at, ended_at)``.
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='subagents'"
    )
    if not cur.fetchone():
        return {}
    sql = (
        "SELECT s.id, s.input_tokens + s.output_tokens AS total_tokens, "
        "       sub.agent_type, sub.started_at "
        "FROM subagents sub LEFT JOIN sessions s ON s.id = sub.session_id"
    )
    args: tuple[Any, ...] = ()
    if since is not None:
        sql += " WHERE sub.started_at >= ?"
        args = (since,)
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(sql, args):
        agent_type = row["agent_type"] or "unknown"
        bucket = out.setdefault(
            agent_type,
            {"sessions": 0, "tokens_total": 0, "calls": 0},
        )
        bucket["calls"] += 1
        if row["total_tokens"] is not None:
            bucket["tokens_total"] += row["total_tokens"]
    return out


def query_skill_usage(
    conn: sqlite3.Connection, since: float | None
) -> dict[str, dict[str, Any]]:
    """Return per-skill invocation counts if the ``tool_usage`` table exists.

    ``tool_usage`` records every tool execution. Skill invocations show up
    as rows with ``tool_name`` matching ``Skill`` / ``mcp__skill__*``.
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_usage'"
    )
    if not cur.fetchone():
        return {}
    sql = (
        "SELECT tool_name, COUNT(*) AS calls FROM tool_usage "
        "WHERE tool_name LIKE 'Skill%' OR tool_name LIKE 'mcp__skill%'"
    )
    args: tuple[Any, ...] = ()
    if since is not None:
        sql += " AND timestamp >= ?"
        args = (since,)
    sql += " GROUP BY tool_name"
    return {
        (row["tool_name"] or "unknown"): {
            "sessions": 0,
            "calls": int(row["calls"] or 0),
            "tokens_total": 0,
        }
        for row in conn.execute(sql, args)
    }


def find_cache_breaks(
    sessions: list[sqlite3.Row], threshold: int
) -> list[dict[str, Any]]:
    """Sessions with high uncached input tokens (a cache-miss heuristic).

    OC SessionDB tracks ``input_tokens`` (uncached) and
    ``cache_read_tokens`` separately. A session whose uncached input_tokens
    exceeds the threshold is a likely cache break.
    """
    out: list[dict[str, Any]] = []
    for s in sessions:
        uncached = int(s["input_tokens"] or 0)
        if uncached < threshold:
            continue
        out.append(
            {
                "session": s["id"],
                "title": s["title"] or "",
                "ts": s["started_at"],
                "uncached": uncached,
                "cached_read": int(s["cache_read_tokens"] or 0),
            }
        )
    out.sort(key=lambda r: -r["uncached"])
    return out


# ─── Aggregation ────────────────────────────────────────────────────────────


def aggregate(
    sessions: list[sqlite3.Row],
    cache_break_threshold: int,
) -> dict[str, Any]:
    """Roll session rows into the ``overall`` and ``by_project`` blocks."""
    cb = find_cache_breaks(sessions, cache_break_threshold)
    by_source: dict[str, dict[str, Any]] = {}

    overall_input = overall_output = 0
    overall_cache_read = overall_cache_write = 0
    overall_compactions = 0
    earliest = latest = 0.0

    for s in sessions:
        src = s["source"] or s["platform"] or "unknown"
        b = by_source.setdefault(
            src,
            {
                "sessions": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "compactions": 0,
                "hours": {"active": 0.0},
                "cache_breaks_over_100k": 0,
            },
        )
        b["sessions"] += 1
        b["input_tokens"] += int(s["input_tokens"] or 0)
        b["output_tokens"] += int(s["output_tokens"] or 0)
        b["cache_read_tokens"] += int(s["cache_read_tokens"] or 0)
        b["cache_write_tokens"] += int(s["cache_write_tokens"] or 0)
        b["compactions"] += int(s["compactions_count"] or 0)
        if int(s["input_tokens"] or 0) >= cache_break_threshold:
            b["cache_breaks_over_100k"] += 1

        overall_input += int(s["input_tokens"] or 0)
        overall_output += int(s["output_tokens"] or 0)
        overall_cache_read += int(s["cache_read_tokens"] or 0)
        overall_cache_write += int(s["cache_write_tokens"] or 0)
        overall_compactions += int(s["compactions_count"] or 0)
        if s["started_at"]:
            earliest = (
                s["started_at"]
                if not earliest
                else min(earliest, s["started_at"])
            )
            latest = max(latest, s["started_at"])

    overall = {
        "sessions": len(sessions),
        "input_tokens": {
            "uncached": overall_input,
            "cache_read": overall_cache_read,
            "cache_write": overall_cache_write,
            "total": overall_input + overall_cache_read + overall_cache_write,
        },
        "output_tokens": overall_output,
        "compactions": overall_compactions,
        "cache_breaks_over_100k": len(cb),
        "earliest_session_at": earliest,
        "latest_session_at": latest,
    }
    return {
        "overall": overall,
        "by_project": by_source,
        "cache_breaks": cb,
    }


# ─── Main ───────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="7d")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--cache-break", type=int, default=100_000)
    args = parser.parse_args(argv)

    since = parse_since(args.since)
    dbs = discover_profile_dbs(args.profile)
    if not dbs:
        msg = (
            "No OpenComputer profiles with sessions.db found at "
            "~/.opencomputer/<profile>/."
        )
        print(json.dumps({"error": msg}), file=sys.stderr)
        return 2

    all_sessions: list[sqlite3.Row] = []
    sub: dict[str, dict[str, Any]] = {}
    skills: dict[str, dict[str, Any]] = {}
    top_prompts: list[dict[str, Any]] = []
    for db in dbs:
        try:
            conn = _connect(db)
        except sqlite3.Error as exc:
            print(
                f"warn: could not open {db}: {exc}",
                file=sys.stderr,
            )
            continue
        all_sessions.extend(query_sessions(conn, since))
        for k, v in query_subagents(conn, since).items():
            sub.setdefault(k, {"sessions": 0, "tokens_total": 0, "calls": 0})
            sub[k]["calls"] += v["calls"]
            sub[k]["tokens_total"] += v["tokens_total"]
        for k, v in query_skill_usage(conn, since).items():
            skills.setdefault(
                k, {"sessions": 0, "tokens_total": 0, "calls": 0}
            )
            skills[k]["calls"] += v["calls"]
        top_prompts.extend(query_top_prompts(conn, since, args.top))
        conn.close()

    top_prompts.sort(key=lambda r: -r["chars"])
    top_prompts = top_prompts[: args.top]

    rolled = aggregate(all_sessions, args.cache_break)
    rolled["by_subagent_type"] = sub
    rolled["by_skill"] = skills
    rolled["top_prompts"] = top_prompts
    rolled["since"] = args.since
    rolled["generated_at"] = time.time()

    payload = json.dumps(rolled, indent=2, sort_keys=True, default=str)
    if args.out:
        Path(args.out).write_text(payload)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
