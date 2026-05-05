"""Tests for Wave 6.E.17 follow-up — `oc kanban callback` CLI surface.

Closes the only honest deferral from the original 6.E.17 design: the
``list_dead_letters`` / ``requeue_dead_letter`` helpers existed but
were not exposed via CLI. Operators previously needed raw SQL.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from opencomputer.kanban import callback_queue as cq
from opencomputer.kanban import cli as kcli
from opencomputer.kanban import db


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    db.init_db()
    return tmp_path


def _run(args: argparse.Namespace) -> tuple[int, str]:
    """Invoke a callback handler with a captured stdout."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = kcli._cmd_callback(args)
    return rc, buf.getvalue()


# ---- list ----


def test_list_pending_empty(kanban_home: Path):
    args = argparse.Namespace(
        callback_action="list", status="pending", json=False,
    )
    rc, out = _run(args)
    assert rc == 0
    assert "no callbacks" in out


def test_list_pending_shows_rows(kanban_home: Path):
    with db.connect() as conn:
        cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://o:1/cb", payload={"x": 1},
        )
    args = argparse.Namespace(
        callback_action="list", status="pending", json=False,
    )
    rc, out = _run(args)
    assert rc == 0
    assert "origin" in out
    assert "pending" in out
    assert "(1 row" in out


def test_list_json_emits_machine_readable(kanban_home: Path):
    with db.connect() as conn:
        cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://o:1/cb", payload={},
        )
    args = argparse.Namespace(
        callback_action="list", status="pending", json=True,
    )
    rc, out = _run(args)
    assert rc == 0
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert parsed[0]["sender_slug"] == "origin"
    assert parsed[0]["status"] == "pending"


def test_list_status_all_shows_delivered_and_dead(kanban_home: Path):
    with db.connect() as conn:
        live = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        dead = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        delivered = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        for _ in range(10):
            cq.mark_attempted(conn, dead, error="boom")
        cq.mark_delivered(conn, delivered)
    args = argparse.Namespace(
        callback_action="list", status="all", json=True,
    )
    rc, out = _run(args)
    assert rc == 0
    parsed = json.loads(out)
    statuses = {row["status"] for row in parsed}
    assert statuses == {"pending", "dead", "delivered"}


# ---- list-dead ----


def test_list_dead_when_empty(kanban_home: Path):
    args = argparse.Namespace(callback_action="list-dead", json=False)
    rc, out = _run(args)
    assert rc == 0
    assert "no dead-letter" in out


def test_list_dead_shows_error_and_requeue_hint(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://o/c", payload={},
        )
        for _ in range(10):
            cq.mark_attempted(conn, row_id, error="HTTP 500: boom")
    args = argparse.Namespace(callback_action="list-dead", json=False)
    rc, out = _run(args)
    assert rc == 0
    assert "HTTP 500: boom" in out
    assert "requeue" in out


def test_list_dead_json(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="origin",
            callback_url="http://o/c", payload={},
        )
        for _ in range(10):
            cq.mark_attempted(conn, row_id, error="X")
    args = argparse.Namespace(callback_action="list-dead", json=True)
    rc, out = _run(args)
    assert rc == 0
    parsed = json.loads(out)
    assert len(parsed) == 1
    assert parsed[0]["id"] == row_id
    assert parsed[0]["last_error"] == "X"


# ---- requeue ----


def test_requeue_resets_dead_letter_to_pending(kanban_home: Path):
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
        for _ in range(10):
            cq.mark_attempted(conn, row_id, error="X")
    args = argparse.Namespace(callback_action="requeue", row_id=row_id)
    rc, out = _run(args)
    assert rc == 0
    assert "pending" in out
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM kanban_pending_callbacks WHERE id = ?",
            (row_id,),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["attempt_count"] == 0
    assert row["last_error"] is None


def test_requeue_unknown_row_returns_error(kanban_home: Path):
    args = argparse.Namespace(callback_action="requeue", row_id=99999)
    buf_err = io.StringIO()
    import sys
    real_stderr = sys.stderr
    sys.stderr = buf_err
    try:
        rc = kcli._cmd_callback(args)
    finally:
        sys.stderr = real_stderr
    assert rc == 1
    assert "not found or not in 'dead'" in buf_err.getvalue()


def test_requeue_pending_row_is_rejected(kanban_home: Path):
    """Requeue is only for dead rows. A pending row should error so an
    operator doesn't accidentally reset attempt_count on something
    that's actively backing off."""
    with db.connect() as conn:
        row_id = cq.enqueue_callback(
            conn, sender_slug="o", callback_url="http://o/c", payload={},
        )
    args = argparse.Namespace(callback_action="requeue", row_id=row_id)
    buf_err = io.StringIO()
    import sys
    real_stderr = sys.stderr
    sys.stderr = buf_err
    try:
        rc = kcli._cmd_callback(args)
    finally:
        sys.stderr = real_stderr
    assert rc == 1


# ---- drain ----


def test_drain_runs_to_completion(kanban_home: Path, monkeypatch):
    """`oc kanban callback drain` should call _drain_pending_callbacks
    once and return 0 on success."""
    calls = []

    def _stub():
        calls.append("drained")

    monkeypatch.setattr(db, "_drain_pending_callbacks", _stub)
    args = argparse.Namespace(callback_action="drain")
    rc, out = _run(args)
    assert rc == 0
    assert "ok" in out
    assert calls == ["drained"]


def test_drain_reports_failure_cleanly(kanban_home: Path, monkeypatch):
    def _boom():
        raise RuntimeError("network broke")

    monkeypatch.setattr(db, "_drain_pending_callbacks", _boom)
    args = argparse.Namespace(callback_action="drain")
    buf_err = io.StringIO()
    import sys
    real_stderr = sys.stderr
    sys.stderr = buf_err
    try:
        rc = kcli._cmd_callback(args)
    finally:
        sys.stderr = real_stderr
    assert rc == 1
    assert "drain failed" in buf_err.getvalue()
    assert "network broke" in buf_err.getvalue()


# ---- dispatch (no action / unknown action) ----


def test_callback_no_action_prints_usage(kanban_home: Path):
    args = argparse.Namespace(callback_action=None)
    buf_err = io.StringIO()
    import sys
    real_stderr = sys.stderr
    sys.stderr = buf_err
    try:
        rc = kcli._cmd_callback(args)
    finally:
        sys.stderr = real_stderr
    assert rc == 0
    assert "usage:" in buf_err.getvalue()


def test_callback_unknown_action_returns_2(kanban_home: Path):
    args = argparse.Namespace(callback_action="bogus")
    buf_err = io.StringIO()
    import sys
    real_stderr = sys.stderr
    sys.stderr = buf_err
    try:
        rc = kcli._cmd_callback(args)
    finally:
        sys.stderr = real_stderr
    assert rc == 2


# ---- argparse integration: full parser path ----


def _build_full_parser() -> argparse.ArgumentParser:
    """Build the same parser tree the top-level `oc` CLI hands to kanban."""
    root = argparse.ArgumentParser(prog="oc")
    sub = root.add_subparsers(dest="cmd")
    kcli.build_parser(sub)
    return root


def test_argparse_recognizes_callback_subcommand(kanban_home: Path):
    """The top-level parser must accept `oc kanban callback list-dead`."""
    parser = _build_full_parser()
    args = parser.parse_args(["kanban", "callback", "list-dead", "--json"])
    assert args.kanban_action == "callback"
    assert args.callback_action == "list-dead"
    assert args.json is True


def test_argparse_requeue_takes_int_row_id(kanban_home: Path):
    parser = _build_full_parser()
    args = parser.parse_args(["kanban", "callback", "requeue", "42"])
    assert args.callback_action == "requeue"
    assert args.row_id == 42
    assert isinstance(args.row_id, int)
