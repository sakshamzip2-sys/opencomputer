"""Tests for opencomputer.evolution.export — trajectory bundling (P-14).

Covers:
- Empty trajectory (no records) — single ZIP with empty events.jsonl
- Synthetic-PII trajectory — all 5 patterns redacted in events.jsonl + counted in redaction.json
- redaction.json reports counts only (no raw matches)
- max_bundle_size_mb cap forces split into ``_part2.zip``, ``_part3.zip``, …
- manifest correctness (per-record summary, total_parts, part_index)
- CLI smoke test for ``opencomputer evolution export-trajectory``

Each test uses an isolated ``OPENCOMPUTER_HOME`` (via ``monkeypatch.setenv``)
so the user's real profile is never touched.
"""

from __future__ import annotations

import json
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.evolution.entrypoint import evolution_app
from opencomputer.evolution.export import bundle, list_records_by_session
from opencomputer.evolution.storage import apply_pending, insert_record, update_reward
from opencomputer.evolution.trajectory import (
    SCHEMA_VERSION_CURRENT,
    TrajectoryEvent,
    TrajectoryRecord,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_home(monkeypatch, tmp_path):
    """Set OPENCOMPUTER_HOME to a fresh tmp dir; return the dir."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def db_path(isolated_home):
    """Return the path the evolution DB will live at (created lazily by callers)."""
    evo_dir = isolated_home / "evolution"
    evo_dir.mkdir(parents=True, exist_ok=True)
    return evo_dir / "trajectory.sqlite"


def _seed_db(
    db_path: Path,
    *,
    session_id: str,
    events: list[TrajectoryEvent],
    reward: float | None = None,
    started_at: float | None = None,
    ended_at: float | None = None,
) -> int:
    """Insert one record with *events* and return its primary-key id."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    apply_pending(conn)
    rec = TrajectoryRecord(
        id=None,
        session_id=session_id,
        schema_version=SCHEMA_VERSION_CURRENT,
        started_at=started_at if started_at is not None else 1_700_000_000.0,
        ended_at=ended_at if ended_at is not None else 1_700_000_010.0,
        events=tuple(events),
        completion_flag=True,
    )
    rid = insert_record(rec, conn=conn)
    if reward is not None:
        update_reward(rid, reward, conn=conn)
    conn.close()
    return rid


def _make_event(
    *,
    session_id: str = "sess-1",
    seq: int = 0,
    metadata: dict | None = None,
    timestamp: float | None = None,
) -> TrajectoryEvent:
    return TrajectoryEvent(
        session_id=session_id,
        message_id=None,
        action_type="tool_call",
        tool_name="Read",
        outcome="success",
        timestamp=timestamp if timestamp is not None else 1_700_000_000.0 + seq,
        metadata=metadata if metadata is not None else {"seq": seq},
    )


def _read_zip(path: Path) -> dict[str, str]:
    """Return a mapping {filename: text-content} from *path*."""
    with zipfile.ZipFile(path, mode="r") as zf:
        return {name: zf.read(name).decode("utf-8") for name in zf.namelist()}


# ---------------------------------------------------------------------------
# 1. Storage helper (list_records_by_session)
# ---------------------------------------------------------------------------


def test_list_records_by_session_empty(isolated_home):
    """No records → empty list (does not raise)."""
    records = list_records_by_session("nope")
    assert records == []


def test_list_records_by_session_returns_only_matching(isolated_home, db_path):
    """Only records with the requested session_id are returned, in created_at ASC order."""
    _seed_db(db_path, session_id="sess-A", events=[_make_event(session_id="sess-A")])
    _seed_db(db_path, session_id="sess-B", events=[_make_event(session_id="sess-B")])
    _seed_db(db_path, session_id="sess-A", events=[_make_event(session_id="sess-A")])
    a_records = list_records_by_session("sess-A")
    assert len(a_records) == 2
    assert all(r.session_id == "sess-A" for r in a_records)


# ---------------------------------------------------------------------------
# 2. bundle() — empty trajectory
# ---------------------------------------------------------------------------


def test_bundle_empty_trajectory(isolated_home, tmp_path):
    """No records for the session → single ZIP with empty events.jsonl."""
    out = tmp_path / "empty.zip"
    paths = bundle("no-such-session", output_path=out)
    assert paths == [out]
    assert out.exists()

    members = _read_zip(out)
    assert set(members) == {"manifest.json", "events.jsonl", "redaction.json"}
    assert members["events.jsonl"] == ""
    manifest = json.loads(members["manifest.json"])
    assert manifest["session_id"] == "no-such-session"
    assert manifest["records"] == []
    assert manifest["total_events"] == 0
    assert manifest["part_index"] == 1
    assert manifest["total_parts"] == 1
    redaction = json.loads(members["redaction.json"])
    assert redaction == {
        "api_key": 0,
        "file_path": 0,
        "email": 0,
        "ip": 0,
        "bearer_token": 0,
    }


# ---------------------------------------------------------------------------
# 3. bundle() — synthetic-PII trajectory: all 5 patterns get redacted + counted
# ---------------------------------------------------------------------------


def test_bundle_redacts_all_patterns(isolated_home, db_path, tmp_path):
    """Each of the 5 redaction patterns appears in metadata and is scrubbed."""
    pii_events = [
        _make_event(
            seq=0,
            metadata={
                "file_path": "/Users/alice/Vscode/foo.py",
                "duration_seconds": 1.2,
            },
        ),
        _make_event(
            seq=1,
            metadata={
                "user_email": "alice@example.com",
                "remote_ip": "8.8.8.8",
            },
        ),
        _make_event(
            seq=2,
            metadata={
                "auth_header": "Bearer abc123-xyz789.tok",
                "key_value": "sk-abcdef0123456789abcdef0123",
            },
        ),
    ]
    _seed_db(db_path, session_id="sess-pii", events=pii_events, reward=0.75)

    out = tmp_path / "pii.zip"
    paths = bundle("sess-pii", output_path=out)
    assert paths == [out]

    members = _read_zip(out)
    body = members["events.jsonl"]
    # All 5 raw values must be gone from events.jsonl
    assert "/Users/alice/" not in body
    assert "alice@example.com" not in body
    assert "8.8.8.8" not in body
    assert "abc123-xyz789.tok" not in body
    assert "sk-abcdef0123456789abcdef0123" not in body
    # All 5 placeholders must be present
    assert "/Users/REDACTED/" in body
    assert "<EMAIL_REDACTED>" in body
    assert "<IP_REDACTED>" in body
    assert "Bearer <REDACTED>" in body
    assert "<API_KEY_REDACTED>" in body

    # Non-string fields preserved
    assert "1.2" in body  # duration_seconds

    # redaction.json — counts only, no raw values
    redaction = json.loads(members["redaction.json"])
    assert redaction == {
        "api_key": 1,
        "file_path": 1,
        "email": 1,
        "ip": 1,
        "bearer_token": 1,
    }
    # Sanity: nothing in redaction.json leaked PII
    raw_redaction_text = members["redaction.json"]
    assert "alice" not in raw_redaction_text
    assert "8.8.8.8" not in raw_redaction_text
    assert "sk-abcdef" not in raw_redaction_text


# ---------------------------------------------------------------------------
# 4. bundle() — manifest correctness (record summary + reward)
# ---------------------------------------------------------------------------


def test_bundle_manifest_includes_record_summary(isolated_home, db_path, tmp_path):
    """Manifest carries per-record summary fields including reward."""
    rid = _seed_db(
        db_path,
        session_id="sess-manifest",
        events=[_make_event(seq=0), _make_event(seq=1)],
        reward=0.9,
    )

    out = tmp_path / "manifest.zip"
    paths = bundle("sess-manifest", output_path=out)
    assert len(paths) == 1

    manifest = json.loads(_read_zip(out)["manifest.json"])
    assert manifest["session_id"] == "sess-manifest"
    assert manifest["schema_version"] == SCHEMA_VERSION_CURRENT
    assert manifest["total_events"] == 2
    assert manifest["part_index"] == 1
    assert manifest["total_parts"] == 1
    assert len(manifest["records"]) == 1
    rec = manifest["records"][0]
    assert rec["record_id"] == rid
    assert rec["event_count"] == 2
    assert rec["completion_flag"] is True
    assert rec["reward_score"] == pytest.approx(0.9)
    assert rec["schema_version"] == SCHEMA_VERSION_CURRENT


# ---------------------------------------------------------------------------
# 5. bundle() — events.jsonl line count + ordering
# ---------------------------------------------------------------------------


def test_bundle_events_jsonl_line_per_event_in_order(isolated_home, db_path, tmp_path):
    """One JSON line per event, in seq order; round-trips back to the same shape."""
    events = [_make_event(seq=i) for i in range(5)]
    _seed_db(db_path, session_id="sess-lines", events=events)

    out = tmp_path / "lines.zip"
    bundle("sess-lines", output_path=out)
    body = _read_zip(out)["events.jsonl"]
    lines = [line for line in body.splitlines() if line]
    assert len(lines) == 5
    # seq order preserved + each line is valid JSON with our expected fields
    for i, line in enumerate(lines):
        payload = json.loads(line)
        assert payload["session_id"] == "sess-lines"
        assert payload["action_type"] == "tool_call"
        assert payload["metadata"]["seq"] == i


# ---------------------------------------------------------------------------
# 6. bundle() — max-bundle-size split into _part2, _part3 …
# ---------------------------------------------------------------------------


def test_bundle_splits_when_over_max_bundle_size(isolated_home, db_path, tmp_path):
    """A trajectory bigger than max_bundle_size_mb spans multiple parts.

    We seed many events with moderately-large (but schema-legal: ≤200 char)
    string metadata so the serialised payload exceeds the 1MB cap and forces
    at least one split.
    """
    # 200-char string is the schema upper bound — fill it to push size up.
    # Each serialised line is ~380 bytes; 6000 lines ≈ 2.2 MB > 1 MB cap.
    big_value = "x" * 200
    events = []
    for i in range(6000):
        events.append(
            _make_event(
                seq=i,
                metadata={"seq": i, "blob": big_value},
            )
        )
    _seed_db(db_path, session_id="sess-big", events=events)

    out = tmp_path / "big.zip"
    paths = bundle("sess-big", output_path=out, max_bundle_size_mb=1)
    assert len(paths) >= 2, f"expected split, got {len(paths)} parts"
    # Naming convention: first is base; subsequent files have ``_partN`` suffix
    assert paths[0] == out
    for idx, p in enumerate(paths[1:], start=2):
        assert p.name.endswith(f"_part{idx}.zip")

    # Manifests across parts agree on total_parts and increment part_index
    total_events_across_parts = 0
    seen_indices = []
    for p in paths:
        manifest = json.loads(_read_zip(p)["manifest.json"])
        seen_indices.append(manifest["part_index"])
        assert manifest["total_parts"] == len(paths)
        total_events_across_parts += manifest["total_events"]
    assert seen_indices == list(range(1, len(paths) + 1))
    assert total_events_across_parts == 6000


def test_bundle_rejects_zero_or_negative_size(isolated_home, db_path, tmp_path):
    """``max_bundle_size_mb < 1`` is a programmer error — surface it loudly."""
    _seed_db(db_path, session_id="sess-x", events=[_make_event()])
    with pytest.raises(ValueError):
        bundle("sess-x", output_path=tmp_path / "x.zip", max_bundle_size_mb=0)


# ---------------------------------------------------------------------------
# 7. bundle() — multiple records for one session merged into one bundle
# ---------------------------------------------------------------------------


def test_bundle_concatenates_multiple_records(isolated_home, db_path, tmp_path):
    """Two records with the same session_id are flattened into one events.jsonl
    in created_at ASC order; manifest lists both records."""
    # Two separate records share a session; events stagger in time
    _seed_db(
        db_path,
        session_id="sess-multi",
        events=[_make_event(seq=0), _make_event(seq=1)],
        started_at=1_700_000_000.0,
        ended_at=1_700_000_010.0,
    )
    # Pause so created_at differs (sub-second is fine for SQLite REAL)
    time.sleep(0.01)
    _seed_db(
        db_path,
        session_id="sess-multi",
        events=[_make_event(seq=2)],
        started_at=1_700_000_100.0,
        ended_at=1_700_000_110.0,
    )

    out = tmp_path / "multi.zip"
    paths = bundle("sess-multi", output_path=out)
    assert len(paths) == 1
    members = _read_zip(out)
    manifest = json.loads(members["manifest.json"])
    assert len(manifest["records"]) == 2
    assert manifest["total_events"] == 3
    assert sum(rec["event_count"] for rec in manifest["records"]) == 3


# ---------------------------------------------------------------------------
# 8. bundle() — default output path
# ---------------------------------------------------------------------------


def test_bundle_default_output_path(isolated_home, db_path):
    """When output_path is omitted, the ZIP lands under <home>/trajectory_exports/."""
    _seed_db(db_path, session_id="sess-default", events=[_make_event()])
    paths = bundle("sess-default")
    assert len(paths) == 1
    assert paths[0].parent == isolated_home / "trajectory_exports"
    assert paths[0].name.startswith("sess-default_")
    assert paths[0].suffix == ".zip"
    assert paths[0].exists()


# ---------------------------------------------------------------------------
# 9. CLI smoke test: ``opencomputer evolution export-trajectory``
# ---------------------------------------------------------------------------


def test_cli_export_trajectory_happy_path(isolated_home, db_path, tmp_path):
    _seed_db(
        db_path,
        session_id="sess-cli",
        events=[_make_event(seq=0, metadata={"file_path": "/Users/alice/x.py"})],
    )
    out = tmp_path / "cli.zip"
    result = runner.invoke(
        evolution_app,
        ["export-trajectory", "sess-cli", "--output", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert "Exported" in result.output
    assert out.exists()
    body = _read_zip(out)["events.jsonl"]
    assert "/Users/REDACTED/" in body


def test_cli_export_trajectory_empty_session(isolated_home, tmp_path):
    out = tmp_path / "empty.zip"
    result = runner.invoke(
        evolution_app,
        ["export-trajectory", "no-such-session", "--output", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()


def test_cli_export_trajectory_invalid_size(isolated_home, db_path, tmp_path):
    _seed_db(db_path, session_id="sess-bad", events=[_make_event()])
    result = runner.invoke(
        evolution_app,
        [
            "export-trajectory",
            "sess-bad",
            "--output",
            str(tmp_path / "bad.zip"),
            "--max-bundle-size",
            "0",
        ],
    )
    assert result.exit_code != 0
    assert "Cannot export" in result.output or "must be" in result.output
