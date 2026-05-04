"""Tests for the oc kanban orgchart command (Wave 6.E.14)."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from opencomputer.kanban import db


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    db.init_db()
    return tmp_path


def _run_cli(*argv: str) -> tuple[int, str]:
    from opencomputer.kanban import cli as kbcli
    parser = argparse.ArgumentParser(prog="oc", add_help=False)
    sub = parser.add_subparsers(dest="cmd")
    kbcli.build_parser(sub)
    parsed = parser.parse_args(["kanban", *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = kbcli.kanban_command(parsed) or 0
    return rc, buf.getvalue()


# ---- empty board ----


def test_orgchart_empty_board(kanban_home: Path):
    rc, out = _run_cli("orgchart")
    assert rc == 0
    assert "Auto-routing rules" in out
    assert "(none" in out  # no rules


# ---- rules-only output ----


def test_orgchart_with_rules_only(kanban_home: Path):
    with db.connect() as conn:
        db.add_assignment_rule(
            conn, pattern_kind="title_regex", pattern="^deploy:",
            assignee="deploy-bot", priority=100,
        )
        db.add_assignment_rule(
            conn, pattern_kind="default", pattern="*",
            assignee="catchall", priority=0,
        )
    rc, out = _run_cli("orgchart", "--depth", "0")
    assert rc == 0
    assert "deploy-bot" in out
    assert "catchall" in out
    # Higher-priority rule comes first
    assert out.index("deploy-bot") < out.index("catchall")


# ---- assignees + counts (depth=2) ----


def test_orgchart_includes_running_counts(kanban_home: Path):
    with db.connect() as conn:
        db.create_task(conn, title="t1", body=None, assignee="alice")
        # Move task to running
        conn.execute("UPDATE tasks SET status='running'")
        conn.commit()
    rc, out = _run_cli("orgchart")
    assert rc == 0
    assert "alice" in out
    assert "running=1" in out


def test_orgchart_includes_done_recent_count(kanban_home: Path):
    import time
    now = int(time.time())
    with db.connect() as conn:
        tid = db.create_task(conn, title="t1", body=None, assignee="bob")
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=? WHERE id=?",
            (now, tid),
        )
        conn.commit()
    rc, out = _run_cli("orgchart", "--days", "7")
    assert rc == 0
    assert "bob" in out
    assert "done_recent=1" in out


# ---- depth gating ----


def test_orgchart_depth_0_skips_assignees_section(kanban_home: Path):
    with db.connect() as conn:
        db.create_task(conn, title="t", body=None, assignee="x")
    rc, out = _run_cli("orgchart", "--depth", "0")
    assert rc == 0
    assert "Active workers" not in out


def test_orgchart_depth_1_no_metrics(kanban_home: Path):
    """At depth=1, assignees show but per-assignee counts don't."""
    with db.connect() as conn:
        db.create_task(conn, title="t", body=None, assignee="x")
    rc, out = _run_cli("orgchart", "--depth", "1")
    assert rc == 0
    assert "Active workers" in out
    assert "x" in out
    assert "running=" not in out  # metrics suppressed at depth=1


# ---- JSON output ----


def test_orgchart_json_output(kanban_home: Path):
    with db.connect() as conn:
        db.add_assignment_rule(
            conn, pattern_kind="default", pattern="*",
            assignee="x", priority=0,
        )
        db.create_task(conn, title="t", body=None, assignee="x")
    rc, out = _run_cli("orgchart", "--json")
    assert rc == 0
    data = json.loads(out)
    assert "rules" in data
    assert "assignees" in data
    assert data["depth"] == 2
    assert len(data["rules"]) == 1
    assert data["rules"][0]["assignee"] == "x"
    assert "x" in data["assignees"]
