"""SessionDB persists Message.reasoning_replay_blocks across save+load."""

from opencomputer.agent.state import SessionDB
from plugin_sdk import Message


def test_replay_blocks_roundtrip(tmp_path):
    db = SessionDB(tmp_path / "test.db")
    session_id = "test-session"
    db.create_session(session_id, title="t")
    blocks = [
        {"type": "thinking", "thinking": "let me work through this", "signature": "sig-roundtrip"}
    ]
    msg = Message(
        role="assistant",
        content="working on it",
        reasoning_replay_blocks=blocks,
    )
    db.append_message(session_id, msg)
    loaded = db.get_messages(session_id)
    assert len(loaded) == 1
    assert loaded[0].reasoning_replay_blocks == blocks


def test_replay_blocks_none_persists_as_none(tmp_path):
    db = SessionDB(tmp_path / "test.db")
    session_id = "test-session"
    db.create_session(session_id, title="t")
    msg = Message(role="user", content="hello")
    db.append_message(session_id, msg)
    loaded = db.get_messages(session_id)
    assert loaded[0].reasoning_replay_blocks is None


def test_replay_blocks_corrupt_json_falls_back_to_none(tmp_path):
    db = SessionDB(tmp_path / "test.db")
    session_id = "test-session"
    db.create_session(session_id, title="t")
    msg = Message(role="user", content="hi")
    db.append_message(session_id, msg)
    # Corrupt the column manually to simulate a half-broken row.
    with db._connect() as conn:  # noqa: SLF001
        conn.execute(
            "UPDATE messages SET reasoning_replay_blocks = ? WHERE session_id = ?",
            ("not-valid-json{", session_id),
        )
        conn.commit()
    # Loader must tolerate corruption — falls back to None, doesn't crash.
    loaded = db.get_messages(session_id)
    assert loaded[0].reasoning_replay_blocks is None
