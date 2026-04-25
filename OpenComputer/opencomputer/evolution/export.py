"""Trajectory export with redacted bundles (P-14).

Bundles existing-redacted ``TrajectoryEvents`` for a single session_id into a
ZIP file containing:

- ``manifest.json`` — record-level metadata (session_id, event counts,
  timestamps, reward, schema_version, part info)
- ``events.jsonl``  — one TrajectoryEvent per line, JSON-serialised, with the
  secondary regex sweep applied to all string-valued metadata fields
- ``redaction.json``— per-pattern hit counts (NO raw matches stored)

The schema-level privacy rule (TrajectoryEvent.__post_init__) already rejects
any string-value metadata > 200 chars at construction time, so records on disk
are already redacted-by-construction.  This module does a SECONDARY regex sweep
on those short string fields (file paths, error message previews, …) before
writing them to ``events.jsonl``.

If a single trajectory's serialised payload exceeds ``max_bundle_size_mb``,
the export is split into multiple ZIP files named ``<base>.zip``,
``<base>_part2.zip``, … with ``manifest.part_index`` / ``manifest.total_parts``
pointers.  Within a part, events are kept in seq order.

Public API:

    >>> from opencomputer.evolution.export import bundle
    >>> paths = bundle("sess-abc")              # default output_path under profile home
    >>> paths = bundle("sess-abc", output_path=Path("/tmp/out.zip"), max_bundle_size_mb=10)

The function returns the list of ZIP paths actually written (length 1 in the
common case, ≥ 2 when split).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from opencomputer.agent.config import _home
from opencomputer.evolution.redaction import (
    PATTERN_NAMES,
    empty_counts,
    merge_counts,
    redact_metadata,
)
from opencomputer.evolution.storage import (
    _build_record,
    init_db,
)
from opencomputer.evolution.trajectory import (
    SCHEMA_VERSION_CURRENT,
    TrajectoryEvent,
    TrajectoryRecord,
)

# ---------------------------------------------------------------------------
# Storage query — by session_id (added here to avoid touching public storage API)
# ---------------------------------------------------------------------------


def list_records_by_session(
    session_id: str,
    conn: sqlite3.Connection | None = None,
) -> list[TrajectoryRecord]:
    """Return all TrajectoryRecords for *session_id*, ordered by created_at ASC.

    A single session can produce multiple records (e.g. fork / resume cycles),
    so we return all of them.  Each record includes its events in seq order.
    Uses the ``idx_traj_session`` index for an efficient lookup.

    When *conn* is omitted we open a fresh connection AND run pending
    migrations — calling this on a brand-new profile must not raise
    ``no such table`` even before any records exist.
    """
    _own_conn = conn is None
    if _own_conn:
        conn = init_db()
    assert conn is not None
    try:
        rows = conn.execute(
            """
            SELECT * FROM trajectory_records
            WHERE session_id = ?
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
        records: list[TrajectoryRecord] = []
        for row in rows:
            event_rows = conn.execute(
                "SELECT * FROM trajectory_events WHERE record_id = ? ORDER BY seq",
                (row["id"],),
            ).fetchall()
            records.append(_build_record(row, event_rows))
        return records
    finally:
        if _own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_output_path(session_id: str) -> Path:
    """Return ``<profile_home>/trajectory_exports/<session_id>_<unix_ts>.zip``.

    The directory is created if missing.
    """
    out_dir = _home() / "trajectory_exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return out_dir / f"{safe_id}_{int(time.time())}.zip"


def _serialise_event(event: TrajectoryEvent) -> tuple[str, dict[str, int]]:
    """Apply the redaction sweep to *event*'s metadata and return JSON line + counts.

    Returns ``(json_line_no_newline, hit_counts)`` — the caller writes the
    newline so it can stop mid-event when a part hits the size cap.
    """
    redacted_meta, hits = redact_metadata(event.metadata)
    payload = asdict(event)
    payload["metadata"] = redacted_meta
    return json.dumps(payload, sort_keys=True), hits


def _record_summary(record: TrajectoryRecord, conn: sqlite3.Connection) -> dict[str, object]:
    """Return the per-record summary dict (id, schema_version, timing, reward, events)."""
    reward: float | None = None
    if record.id is not None:
        row = conn.execute(
            "SELECT reward_score FROM trajectory_records WHERE id = ?",
            (record.id,),
        ).fetchone()
        if row is not None and row["reward_score"] is not None:
            reward = float(row["reward_score"])
    return {
        "record_id": record.id,
        "schema_version": record.schema_version,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "completion_flag": record.completion_flag,
        "reward_score": reward,
        "event_count": len(record.events),
    }


def _empty_bundle(
    *,
    session_id: str,
    output_path: Path,
) -> list[Path]:
    """Write a single ZIP with empty events.jsonl + summary manifest.

    Used when no records exist for the session (so callers always get a ZIP).
    """
    import zipfile

    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "session_id": session_id,
        "schema_version": SCHEMA_VERSION_CURRENT,
        "exported_at": time.time(),
        "records": [],
        "total_events": 0,
        "part_index": 1,
        "total_parts": 1,
    }
    with zipfile.ZipFile(output_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        zf.writestr("events.jsonl", "")
        zf.writestr(
            "redaction.json",
            json.dumps(empty_counts(), indent=2, sort_keys=True),
        )
    return [output_path]


# ---------------------------------------------------------------------------
# Public bundle()
# ---------------------------------------------------------------------------


def bundle(
    session_id: str,
    *,
    output_path: Path | None = None,
    max_bundle_size_mb: int = 50,
) -> list[Path]:
    """Bundle all trajectory records for *session_id* into one or more ZIP files.

    Args:
        session_id: The session whose records to export.  All records (one or
            more, in created_at ASC order) are flattened into ``events.jsonl``.
        output_path: Where to write the (first) ZIP.  Defaults to
            ``<profile_home>/trajectory_exports/<session_id>_<unix_ts>.zip``.
        max_bundle_size_mb: Soft cap (in megabytes) for any single output ZIP.
            When the in-progress ZIP would exceed this size, the writer
            finalises it and starts a ``_part2.zip`` continuation; subsequent
            parts (3, 4, …) follow the same pattern.

    Returns:
        List of ZIP paths actually written.  Length 1 in the common case, ≥ 2
        when split.

    Raises:
        ValueError: if ``max_bundle_size_mb < 1`` (we round up the threshold to
            ensure each part can hold at least one event).
    """
    import zipfile

    if max_bundle_size_mb < 1:
        raise ValueError(
            f"max_bundle_size_mb must be >= 1 (got {max_bundle_size_mb})"
        )

    if output_path is None:
        output_path = _default_output_path(session_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    conn = init_db()
    try:
        records = list_records_by_session(session_id, conn=conn)
        if not records:
            return _empty_bundle(session_id=session_id, output_path=output_path)

        # Pre-serialise every event with redaction applied.  We accumulate the
        # JSONL bytes per part; each ``part`` gets its own manifest carrying
        # the subset of records / events it actually contains.
        max_bytes = max_bundle_size_mb * 1024 * 1024

        # Build (record_index, event_index, json_line_no_newline, hits) tuples.
        per_event: list[tuple[int, int, str, dict[str, int]]] = []
        for r_idx, record in enumerate(records):
            for e_idx, event in enumerate(record.events):
                line, hits = _serialise_event(event)
                per_event.append((r_idx, e_idx, line, hits))

        record_summaries = [_record_summary(r, conn) for r in records]

    finally:
        conn.close()

    # Partition events into parts that respect max_bytes.  Manifest + redaction
    # bytes are tiny relative to events — we reserve a 16 KiB slack for them.
    SLACK = 16 * 1024
    effective_cap = max(max_bytes - SLACK, 1024)

    parts: list[list[tuple[int, int, str, dict[str, int]]]] = [[]]
    cur_bytes = 0
    for entry in per_event:
        line_bytes = len(entry[2].encode("utf-8")) + 1  # +1 for trailing newline
        if cur_bytes + line_bytes > effective_cap and parts[-1]:
            parts.append([])
            cur_bytes = 0
        parts[-1].append(entry)
        cur_bytes += line_bytes

    total_parts = len(parts)
    written: list[Path] = []

    base_path = output_path
    base_stem = base_path.stem
    base_suffix = base_path.suffix or ".zip"
    base_dir = base_path.parent

    for part_idx, entries in enumerate(parts, start=1):
        if part_idx == 1:
            target = base_path
        else:
            target = base_dir / f"{base_stem}_part{part_idx}{base_suffix}"

        # Determine which record summaries appear in this part (by record_index)
        record_indices_in_part = sorted({e[0] for e in entries})
        part_records = [record_summaries[i] for i in record_indices_in_part]

        manifest: dict[str, object] = {
            "session_id": session_id,
            "schema_version": SCHEMA_VERSION_CURRENT,
            "exported_at": time.time(),
            "records": part_records,
            "total_events": len(entries),
            "part_index": part_idx,
            "total_parts": total_parts,
        }

        # Per-part redaction counts — sum across the events in this part.
        part_counts: Mapping[str, int] = merge_counts(*[e[3] for e in entries])

        events_jsonl = "".join(line + "\n" for (_r, _e, line, _h) in entries)

        with zipfile.ZipFile(target, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
            zf.writestr("events.jsonl", events_jsonl)
            zf.writestr(
                "redaction.json",
                json.dumps(dict(part_counts), indent=2, sort_keys=True),
            )

        written.append(target)

    # If we somehow ended up with zero events but had records (e.g. records
    # with empty .events tuples), fall back to the empty-bundle writer so
    # callers always get exactly one ZIP file in that degenerate case.
    if not written:
        return _empty_bundle(session_id=session_id, output_path=base_path)

    return written


# Re-export the redaction pattern names so callers can introspect what the
# secondary sweep covers without importing ``redaction`` directly.
__all__ = [
    "bundle",
    "list_records_by_session",
    "PATTERN_NAMES",
]
