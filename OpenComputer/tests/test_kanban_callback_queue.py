"""Tests for Wave 6.E.17 — outbound callback retry queue.

PR #460 audit lens A8 deferred the callback retry queue: "if the
callback POST to sender fails, the result data is lost." This test
file exercises the production-grade replacement: enqueue + drainer +
exponential backoff + dead-letter + idempotency.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from opencomputer.gateway import kanban_dispatcher as gw_disp
from opencomputer.kanban import callback_queue as cq
from opencomputer.kanban import db
from opencomputer.kanban import remote_hosts as rh


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    db.init_db()
    return tmp_path


# ---------------------------------------------------------------------------
# Schema migration sanity
# ---------------------------------------------------------------------------


def test_schema_creates_callback_tables(kanban_home: Path):
    """Both new tables exist after init_db."""
    with db.connect() as conn:
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "kanban_delegated_tasks" in names
    assert "kanban_pending_callbacks" in names


# ---------------------------------------------------------------------------
# record_delegated_task / find_delegated_task
# ---------------------------------------------------------------------------


def test_record_and_find_delegated_task(kanban_home: Path):
    with db.connect() as conn:
        cq.record_delegated_task(
            conn,
            local_task_id="task-abc",
            sender_slug="origin",
            callback_url="http://origin:9119/cb",
        )
        result = cq.find_delegated_task(conn, "task-abc")
    assert result == ("origin", "http://origin:9119/cb")


def test_record_delegated_task_is_idempotent(kanban_home: Path):
    """Re-calling with the same task_id should not raise (REPLACE)."""
    with db.connect() as conn:
        cq.record_delegated_task(
            conn, local_task_id="t1",
            sender_slug="o1", callback_url="http://x/1",
        )
        cq.record_delegated_task(
            conn, local_task_id="t1",
            sender_slug="o2", callback_url="http://x/2",
        )
        result = cq.find_delegated_task(conn, "t1")
    # Latest wins (REPLACE semantics).
    assert result == ("o2", "http://x/2")


def test_find_returns_none_for_local_task(kanban_home: Path):
    with db.connect() as conn:
        assert cq.find_delegated_task(conn, "never-recorded") is None


def test_record_validates_inputs(kanban_home: Path):
    with db.connect() as conn:
        with pytest.raises(ValueError):
            cq.record_delegated_task(
                conn, local_task_id="", sender_slug="x", callback_url="y",
            )


# ---------------------------------------------------------------------------
# enqueue_callback / next_due / mark_delivered / mark_attempted
# ---------------------------------------------------------------------------


def test_enqueue_then_next_due_returns_row(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://o:1/cb",
            payload={"outcome": "done", "remote_task_id": "t1"},
        )
        due = cq.next_due(conn)
    assert row_id > 0
    assert len(due) == 1
    assert due[0].id == row_id
    assert due[0].sender_slug == "origin"
    assert due[0].attempt_count == 0
    assert due[0].status == "pending"
    body = json.loads(due[0].payload_json)
    assert body["outcome"] == "done"


def test_next_due_excludes_future_attempts(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c",
            payload={"x": 1},
        )
        # Manually push next_attempt_at into the future.
        with db.write_txn(conn):
            conn.execute(
                "UPDATE kanban_pending_callbacks SET next_attempt_at = ? "
                "WHERE id = ?",
                (int(time.time()) + 3600, row_id),
            )
        due = cq.next_due(conn)
    assert len(due) == 0


def test_next_due_excludes_delivered(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        cq.mark_delivered(conn, row_id)
        due = cq.next_due(conn)
    assert len(due) == 0


def test_next_due_excludes_dead(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        # 10 attempts → dead.
        for _ in range(10):
            cq.mark_attempted(conn, row_id, error="boom")
        due = cq.next_due(conn)
    assert len(due) == 0


# ---------------------------------------------------------------------------
# Backoff schedule (audit lens A6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attempts_done,expected_secs", [
    (1, 30),
    (2, 60),
    (3, 120),
    (4, 300),
    (5, 600),
    (6, 600),
    (7, 600),
    (8, 600),
])
def test_backoff_progression(attempts_done: int, expected_secs: int):
    """Attempt N → next_attempt_at = now + backoff[N-1]."""
    actual = cq._backoff_for_attempt(attempts_done)
    assert actual == expected_secs


def test_mark_attempted_applies_backoff(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        before = int(time.time())
        outcome = cq.mark_attempted(conn, row_id, error="net err")
        after = int(time.time())
        row = conn.execute(
            "SELECT * FROM kanban_pending_callbacks WHERE id = ?", (row_id,),
        ).fetchone()
    assert outcome == "pending"
    assert row["attempt_count"] == 1
    assert row["last_error"] == "net err"
    # next_attempt_at should be ~30s in the future (attempt 1 backoff).
    assert (before + 30) <= row["next_attempt_at"] <= (after + 30)


def test_mark_attempted_dead_letters_after_max(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        outcomes = []
        for _ in range(10):
            outcomes.append(cq.mark_attempted(conn, row_id, error="x"))
    # Last attempt should be 'dead', earlier ones 'pending'.
    assert outcomes[-1] == "dead"
    assert outcomes.count("pending") == 9


def test_mark_attempted_truncates_long_errors(kanban_home: Path):
    """A 600-char error gets truncated to 500 chars in the row."""
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        cq.mark_attempted(conn, row_id, error="X" * 600)
        row = conn.execute(
            "SELECT last_error FROM kanban_pending_callbacks WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert len(row["last_error"]) == 500


def test_mark_attempted_missing_row_returns_missing(kanban_home: Path):
    with db.connect() as conn:
        result = cq.mark_attempted(conn, 99999, error="x")
    assert result == "missing"


# ---------------------------------------------------------------------------
# list_dead_letters / requeue_dead_letter (operator surface)
# ---------------------------------------------------------------------------


def test_list_dead_letters_filters(kanban_home: Path):
    with db.connect() as conn:
        live = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        dead = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        for _ in range(10):
            cq.mark_attempted(conn, dead, error="boom")
        deads = cq.list_dead_letters(conn)
    assert {r.id for r in deads} == {dead}


def test_requeue_dead_letter_resets_state(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        for _ in range(10):
            cq.mark_attempted(conn, row_id, error="boom")
        assert cq.requeue_dead_letter(conn, row_id) is True
        # idempotent — second call returns False (no longer dead).
        assert cq.requeue_dead_letter(conn, row_id) is False
        row = conn.execute(
            "SELECT * FROM kanban_pending_callbacks WHERE id = ?", (row_id,),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["last_error"] is None


# ---------------------------------------------------------------------------
# Hook into complete_task / block_task / spawn_failure
# ---------------------------------------------------------------------------


def test_complete_task_enqueues_callback_for_delegated_task(kanban_home: Path):
    """End-to-end: peer creates a mirror task, records it as delegated,
    worker calls complete_task, callback gets enqueued automatically."""
    with db.connect() as conn:
        tid = db.create_task(conn, title="x", body=None, assignee="local-p")
        cq.record_delegated_task(
            conn, local_task_id=tid,
            sender_slug="origin", callback_url="http://origin/cb",
        )
        # Move to running so complete_task accepts it.
        db.claim_task(conn, tid)
        ok = db.complete_task(
            conn, tid, summary="all done", result="success",
        )
        assert ok is True
        rows = conn.execute(
            "SELECT * FROM kanban_pending_callbacks WHERE sender_slug = 'origin'"
        ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["remote_task_id"] == tid
    assert payload["outcome"] == "done"
    assert payload["summary"] == "all done"


def test_complete_task_does_not_enqueue_for_local_task(kanban_home: Path):
    """A purely local task (never delegated) generates no callback row."""
    with db.connect() as conn:
        tid = db.create_task(conn, title="x", body=None, assignee="local-p")
        db.claim_task(conn, tid)
        db.complete_task(conn, tid, summary="ok")
        rows = conn.execute(
            "SELECT * FROM kanban_pending_callbacks"
        ).fetchall()
    assert len(rows) == 0


def test_block_task_enqueues_callback_for_delegated_task(kanban_home: Path):
    with db.connect() as conn:
        tid = db.create_task(conn, title="x", body=None, assignee="local-p")
        cq.record_delegated_task(
            conn, local_task_id=tid,
            sender_slug="origin", callback_url="http://origin/cb",
        )
        db.claim_task(conn, tid)
        db.block_task(conn, tid, reason="hit a wall")
        rows = conn.execute(
            "SELECT * FROM kanban_pending_callbacks WHERE sender_slug = 'origin'"
        ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["outcome"] == "blocked"
    assert payload["error"] == "hit a wall"


def test_spawn_auto_block_enqueues_failed_callback(kanban_home: Path):
    """When a delegated task hits the spawn-failure limit, the
    auto-block path should enqueue a 'failed' callback."""
    with db.connect() as conn:
        tid = db.create_task(conn, title="x", body=None, assignee="local-p")
        cq.record_delegated_task(
            conn, local_task_id=tid,
            sender_slug="origin", callback_url="http://origin/cb",
        )
        db.claim_task(conn, tid)
        # Trip the auto-block by passing failure_limit=1.
        was_blocked = db._record_spawn_failure(
            conn, tid, "spawn boom", failure_limit=1,
        )
        assert was_blocked is True
        rows = conn.execute(
            "SELECT * FROM kanban_pending_callbacks WHERE sender_slug = 'origin'"
        ).fetchall()
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["outcome"] == "failed"
    assert "spawn boom" in payload["error"]


# ---------------------------------------------------------------------------
# Drainer tick — happy + sad paths
# ---------------------------------------------------------------------------


def _ok(payload_text: str = "{}"):
    m = MagicMock()
    m.status_code = 200
    m.text = payload_text
    return m


def test_drainer_marks_delivered_on_2xx(kanban_home: Path):
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="origin", url="http://origin:1")
        row_id = cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://origin:1/api/plugins/kanban/proxy/callback",
            payload={"remote_task_id": "t1", "outcome": "done"},
        )

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post", return_value=_ok()) as post:
        loop._tick_callback_drainer()

    assert post.call_count == 1
    call = post.call_args
    assert "slug=origin" in call.args[0]
    assert "X-OC-Signature" in call.kwargs["headers"]
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM kanban_pending_callbacks WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row["status"] == "delivered"


def test_drainer_backs_off_on_5xx(kanban_home: Path):
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="origin", url="http://origin:1")
        row_id = cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://origin:1/cb", payload={},
        )

    fail = MagicMock()
    fail.status_code = 500
    fail.text = "internal err"
    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post", return_value=fail):
        loop._tick_callback_drainer()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM kanban_pending_callbacks WHERE id = ?", (row_id,),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1
    assert "500" in row["last_error"]


def test_drainer_backs_off_on_network_error(kanban_home: Path):
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="origin", url="http://origin:1")
        row_id = cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://origin:1/cb", payload={},
        )

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post", side_effect=httpx.ConnectError("refused")):
        loop._tick_callback_drainer()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM kanban_pending_callbacks WHERE id = ?", (row_id,),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["attempt_count"] == 1


def test_drainer_dead_letters_unknown_sender(kanban_home: Path):
    """If the sender slug isn't in kanban_remote_hosts we can't sign
    anything, so the row goes straight to dead."""
    with db.connect() as conn:
        # Note: NO add_remote_host call.
        row_id = cq.enqueue_callback(
            conn, sender_slug="ghost",
            callback_url="http://ghost/cb", payload={},
        )

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post") as post:
        loop._tick_callback_drainer()

    assert post.call_count == 0
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM kanban_pending_callbacks WHERE id = ?", (row_id,),
        ).fetchone()
    assert row["status"] == "dead"
    assert "no longer registered" in row["last_error"]


def test_drainer_suppresses_repeat_errors_per_sender(kanban_home: Path):
    """When sender X has 3 due callbacks and X is down, we POST once,
    fail, then skip the other 2 X callbacks for this tick. Sender Y's
    callback still runs."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="dead", url="http://d:1")
        rh.add_remote_host(conn, slug="alive", url="http://a:1")
        for i in range(3):
            cq.enqueue_callback(
                conn, sender_slug="dead",
                callback_url=f"http://d:1/cb/{i}", payload={"i": i},
            )
        cq.enqueue_callback(
            conn, sender_slug="alive",
            callback_url="http://a:1/cb", payload={},
        )

    def _fake_post(url, *_a, **_kw):
        if "d:1" in url:
            raise httpx.ConnectError("refused")
        return _ok()

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post", side_effect=_fake_post) as post:
        loop._tick_callback_drainer()

    # 2 calls: 1 to dead (fails, suppresses rest of dead), 1 to alive.
    assert post.call_count == 2


def test_drainer_no_op_when_queue_empty(kanban_home: Path):
    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post") as post:
        loop._tick_callback_drainer()
    assert post.call_count == 0


def test_drainer_skips_future_due_rows(kanban_home: Path):
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="origin", url="http://o:1")
        row_id = cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://o:1/cb", payload={},
        )
        # Push next_attempt_at to 1h from now.
        with db.write_txn(conn):
            conn.execute(
                "UPDATE kanban_pending_callbacks SET next_attempt_at = ? "
                "WHERE id = ?",
                (int(time.time()) + 3600, row_id),
            )

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post") as post:
        loop._tick_callback_drainer()
    assert post.call_count == 0


def test_drainer_signs_callback_with_host_secret(kanban_home: Path):
    """The X-OC-Signature header must be a valid HMAC over the body
    using the host's hmac_secret. This also exercises the integration
    with the remote_hosts.signed_headers helper."""
    with db.connect() as conn:
        host = rh.add_remote_host(
            conn, slug="origin", url="http://o:1", hmac_secret="known-secret",
        )
        cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://o:1/api/plugins/kanban/proxy/callback",
            payload={"x": 1},
        )

    captured = {}

    def _capture(url, *, content, headers, **_):
        captured["url"] = url
        captured["body"] = content
        captured["sig"] = headers["X-OC-Signature"]
        return _ok()

    loop = gw_disp.KanbanDispatcherLoop()
    with patch("httpx.post", side_effect=_capture):
        loop._tick_callback_drainer()

    # Verify the signature using the same secret.
    rh.verify_request(
        captured["sig"],
        secret="known-secret",
        method="POST",
        path="/api/plugins/kanban/proxy/callback",
        body=captured["body"],
    )
    # Sanity: slug param appended to URL.
    assert f"slug={host.slug}" in captured["url"]


def test_drainer_fails_open_on_unexpected_error(kanban_home: Path):
    """A SQL error inside next_due must NOT wedge the dispatcher."""
    loop = gw_disp.KanbanDispatcherLoop()
    with patch.object(cq, "next_due", side_effect=RuntimeError("oops")):
        loop._tick_callback_drainer()  # must not raise


# ---------------------------------------------------------------------------
# Standalone run_daemon helper parity
# ---------------------------------------------------------------------------


def test_drain_pending_callbacks_helper_delivers(kanban_home: Path):
    """db._drain_pending_callbacks (used by `oc kanban daemon`) must
    deliver the same callbacks the gateway loop does."""
    with db.connect() as conn:
        rh.add_remote_host(conn, slug="origin", url="http://o:1")
        row_id = cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://o:1/cb", payload={},
        )

    with patch("httpx.post", return_value=_ok()):
        db._drain_pending_callbacks()

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM kanban_pending_callbacks WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row["status"] == "delivered"
