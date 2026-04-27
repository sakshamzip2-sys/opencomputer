"""A.4 — Vibe classifier + SessionDB column tests."""
from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

from opencomputer.agent.state import SessionDB
from opencomputer.agent.vibe_classifier import VALID_VIBES, classify_vibe

# ── classify_vibe heuristics ─────────────────────────────────────────


def test_frustrated_keywords():
    assert classify_vibe(["why doesn't this work, I keep getting errors"]) == "frustrated"
    assert classify_vibe(["this is frustrating"]) == "frustrated"
    assert classify_vibe(["nothing works"]) == "frustrated"


def test_excited_keywords():
    assert classify_vibe(["amazing! that worked!"]) == "excited"
    assert classify_vibe(["let's ship this"]) == "excited"
    assert classify_vibe(["I love this"]) == "excited"
    assert classify_vibe(["worked!!"]) == "excited"


def test_tired_keywords():
    assert classify_vibe(["ok, going to sleep, long day"]) == "tired"
    assert classify_vibe(["I'm exhausted"]) == "tired"


def test_curious_keywords():
    assert classify_vibe(["why does this work?"]) == "curious"
    assert classify_vibe(["tell me more about it"]) == "curious"
    assert classify_vibe(["how come the API does that"]) == "curious"


def test_stuck_keywords():
    assert classify_vibe(["I'm stuck on this for hours"]) == "stuck"
    assert classify_vibe(["no idea what to try next"]) == "stuck"


def test_calm_default():
    assert classify_vibe(["ok thanks"]) == "calm"
    assert classify_vibe(["sure, that makes sense"]) == "calm"
    assert classify_vibe([]) == "calm"
    assert classify_vibe([""]) == "calm"


def test_priority_stuck_beats_frustrated():
    assert classify_vibe(["I'm stuck and frustrated"]) == "stuck"


def test_priority_excited_beats_curious():
    assert classify_vibe(["amazing this worked but also why does it work"]) == "excited"


def test_only_last_3_messages_considered():
    msgs = [
        "old frustration: nothing works",
        "old frustration: still doesn't work",
        "old frustration: keep getting errors",
        "ok thanks, that helped",
    ]
    # Cap is on message COUNT — last 3 still includes 2 frustration-leaning
    # plus 1 calm closer. Frustration-leaning regex hits, so frustrated
    # wins via priority.
    assert classify_vibe(msgs) == "frustrated"
    # If we slice further to just the calm closer:
    assert classify_vibe(msgs[-1:]) == "calm"


def test_all_vibes_in_vocabulary():
    samples = [
        ["nothing works"],
        ["amazing!"],
        ["going to sleep"],
        ["why?"],
        ["thanks"],
        ["I'm stuck"],
    ]
    for s in samples:
        assert classify_vibe(s) in VALID_VIBES


# ── SessionDB vibe API ────────────────────────────────────────────────


def test_session_vibe_starts_none():
    with tempfile.TemporaryDirectory() as td:
        db = SessionDB(Path(td) / "t.db")
        sid = str(uuid.uuid4())
        db.create_session(sid)
        v, ts = db.get_session_vibe(sid)
        assert v is None
        assert ts is None


def test_session_vibe_round_trip():
    with tempfile.TemporaryDirectory() as td:
        db = SessionDB(Path(td) / "t.db")
        sid = str(uuid.uuid4())
        db.create_session(sid)
        db.set_session_vibe(sid, "frustrated")
        v, ts = db.get_session_vibe(sid)
        assert v == "frustrated"
        assert ts is not None
        assert ts > 0


def test_list_recent_session_vibes_orders_by_updated():
    import time as _t

    with tempfile.TemporaryDirectory() as td:
        db = SessionDB(Path(td) / "t.db")
        s1 = str(uuid.uuid4())
        s2 = str(uuid.uuid4())
        db.create_session(s1)
        db.create_session(s2)
        db.set_session_vibe(s1, "tired")
        _t.sleep(0.01)
        db.set_session_vibe(s2, "curious")
        rows = db.list_recent_session_vibes()
        assert len(rows) == 2
        assert rows[0]["id"] == s2
        assert rows[0]["vibe"] == "curious"
        assert rows[1]["id"] == s1


def test_list_recent_session_vibes_excludes_unset():
    with tempfile.TemporaryDirectory() as td:
        db = SessionDB(Path(td) / "t.db")
        s1 = str(uuid.uuid4())
        s2 = str(uuid.uuid4())
        db.create_session(s1)
        db.create_session(s2)
        db.set_session_vibe(s1, "calm")
        rows = db.list_recent_session_vibes()
        assert len(rows) == 1
        assert rows[0]["id"] == s1
