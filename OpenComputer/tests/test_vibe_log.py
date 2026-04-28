"""Per-turn vibe-log persistence (2026-04-28).

The vibe classifier was previously gated behind ``persona_id ==
"companion"``, so on a real production DB *every* session row carried
``vibe = NULL``. This test suite locks in the new contract:

1. ``vibe_log`` table exists at SCHEMA_VERSION and accepts inserts.
2. ``SessionDB.record_vibe`` appends one row per call, with the
   classifier-version tag preserved.
3. ``record_vibe`` resolves the latest user ``message_id`` automatically
   when the caller doesn't pass one.
4. ``list_vibe_log_for_session`` returns rows newest-first.
5. The persona overlay path runs the classifier + log even when the
   active persona is NOT companion (the regression we're guarding
   against — production was producing zero verdicts).
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

from opencomputer.agent.state import SCHEMA_VERSION, SessionDB
from plugin_sdk.core import Message

# ── schema shape ──────────────────────────────────────────────────────


def test_vibe_log_table_exists_at_target_schema(tmp_path: Path) -> None:
    SessionDB(tmp_path / "fresh.db")
    with sqlite3.connect(tmp_path / "fresh.db") as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vibe_log'"
        ).fetchone()
        assert row is not None, "vibe_log table missing — migration didn't run"
        cols = {c[1] for c in conn.execute("PRAGMA table_info(vibe_log)")}
        assert {
            "id",
            "session_id",
            "message_id",
            "vibe",
            "classifier_version",
            "timestamp",
        } <= cols
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION


def test_vibe_log_indexes_present(tmp_path: Path) -> None:
    SessionDB(tmp_path / "fresh.db")
    with sqlite3.connect(tmp_path / "fresh.db") as conn:
        idx_names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='vibe_log'"
            )
        }
        assert "idx_vibe_log_session" in idx_names
        assert "idx_vibe_log_classifier" in idx_names


# ── record_vibe semantics ─────────────────────────────────────────────


def _seed_session(db: SessionDB, session_id: str = "s1") -> int:
    """Create a session + one user message, return the message id."""
    db.create_session(session_id, platform="cli", model="m")
    db.append_message(
        session_id,
        Message(role="user", content="hi there"),
    )
    with sqlite3.connect(db.db_path) as conn:
        return int(
            conn.execute(
                "SELECT id FROM messages WHERE session_id=? AND role='user' "
                "ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()[0]
        )


def test_record_vibe_inserts_row_and_returns_id(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    msg_id = _seed_session(db)
    rid = db.record_vibe("s1", "frustrated", message_id=msg_id)
    assert rid > 0
    rows = db.list_vibe_log_for_session("s1")
    assert len(rows) == 1
    r = rows[0]
    assert r["session_id"] == "s1"
    assert r["message_id"] == msg_id
    assert r["vibe"] == "frustrated"
    assert r["classifier_version"] == "regex_v1"


def test_record_vibe_resolves_message_id_when_omitted(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    msg_id = _seed_session(db)
    db.record_vibe("s1", "calm")
    row = db.list_vibe_log_for_session("s1")[0]
    assert row["message_id"] == msg_id, "should auto-resolve latest user msg id"


def test_record_vibe_classifier_version_preserved(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    _seed_session(db)
    db.record_vibe("s1", "curious", classifier_version="embed_v1")
    row = db.list_vibe_log_for_session("s1")[0]
    assert row["classifier_version"] == "embed_v1"


def test_list_vibe_log_returns_newest_first(tmp_path: Path) -> None:
    db = SessionDB(tmp_path / "s.db")
    _seed_session(db)
    base = time.time()
    db.record_vibe("s1", "calm", timestamp=base)
    db.record_vibe("s1", "stuck", timestamp=base + 1.0)
    db.record_vibe("s1", "excited", timestamp=base + 2.0)
    vibes = [r["vibe"] for r in db.list_vibe_log_for_session("s1")]
    assert vibes == ["excited", "stuck", "calm"]


def test_record_vibe_with_no_user_messages_yet(tmp_path: Path) -> None:
    """``record_vibe`` must not crash if no user message exists yet."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s1", platform="cli", model="m")
    rid = db.record_vibe("s1", "calm")
    assert rid > 0
    row = db.list_vibe_log_for_session("s1")[0]
    assert row["message_id"] is None


# ── regression: vibe runs even when persona != companion ──────────────


def test_persona_overlay_classifies_vibe_for_non_companion(
    tmp_path: Path,
) -> None:
    """The headline regression: pre-2026-04-28 production carried zero
    vibe verdicts because the classifier sat inside ``if persona_id ==
    'companion':``. We now run it on every persona — verify by mocking
    the classifier to return a known label and checking the log row
    landed under a non-companion persona id.
    """
    from opencomputer.agent.loop import AgentLoop

    db = SessionDB(tmp_path / "s.db")
    _seed_session(db)

    # Build a minimal AgentLoop instance just to call _build_persona_overlay.
    # The method only touches self.db + module-level helpers, so a bare
    # __new__ is sufficient — we don't need the full constructor surface.
    loop = AgentLoop.__new__(AgentLoop)
    loop.db = db
    loop._active_persona_id = ""

    # Force the persona classifier to pick "coding" (NOT companion) and
    # the vibe classifier to pick "frustrated" so we can detect the path.
    fake_persona = type(
        "P", (), {"persona_id": "coding", "confidence": 0.9, "reasoning": ""}
    )()
    with patch(
        "opencomputer.awareness.personas.classifier.classify",
        return_value=fake_persona,
    ), patch(
        "opencomputer.awareness.personas.registry.get_persona",
        return_value={"id": "coding", "system_prompt_overlay": "x"},
    ), patch(
        "opencomputer.awareness.personas._foreground.detect_frontmost_app",
        return_value="",
    ), patch(
        "opencomputer.agent.vibe_classifier.classify_vibe",
        return_value="frustrated",
    ):
        loop._build_persona_overlay("s1")

    rows = db.list_vibe_log_for_session("s1")
    assert len(rows) == 1, "vibe should be logged for non-companion persona"
    assert rows[0]["vibe"] == "frustrated"
    assert rows[0]["classifier_version"] == "regex_v1"
