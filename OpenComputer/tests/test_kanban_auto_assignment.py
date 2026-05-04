"""Tests for kanban auto-assignment routing (Wave 6.E.9)."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from opencomputer.kanban import db


@pytest.fixture()
def kanban_home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("OC_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("OC_KANBAN_DB", raising=False)
    monkeypatch.delenv("OC_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("OC_KANBAN_WORKSPACES_ROOT", raising=False)
    db.init_db()
    return tmp_path


# ---- helpers ----


def _add(conn, **kw):
    return db.add_assignment_rule(conn, **kw)


# ---- validation ----


def test_add_rejects_bad_kind(kanban_home: Path):
    with db.connect() as conn:
        with pytest.raises(db.InvalidRuleError):
            _add(conn, pattern_kind="bogus", pattern="x", assignee="y")


def test_add_rejects_unparseable_regex(kanban_home: Path):
    with db.connect() as conn:
        with pytest.raises(db.InvalidRuleError):
            _add(conn, pattern_kind="title_regex", pattern="[unclosed", assignee="x")


def test_add_rejects_empty_assignee(kanban_home: Path):
    with db.connect() as conn:
        with pytest.raises(db.InvalidRuleError):
            _add(conn, pattern_kind="default", pattern="*", assignee="")


def test_add_returns_id_then_lists(kanban_home: Path):
    with db.connect() as conn:
        rid = _add(conn, pattern_kind="default", pattern="*", assignee="alice")
        assert rid > 0
        rules = db.list_assignment_rules(conn)
    assert len(rules) == 1
    assert rules[0]["assignee"] == "alice"


def test_delete_returns_true_when_present(kanban_home: Path):
    with db.connect() as conn:
        rid = _add(conn, pattern_kind="default", pattern="*", assignee="alice")
        assert db.delete_assignment_rule(conn, rid) is True
        assert db.delete_assignment_rule(conn, rid) is False  # already gone


# ---- resolve_assignee ----


def test_default_rule_matches_anything(kanban_home: Path):
    with db.connect() as conn:
        _add(conn, pattern_kind="default", pattern="*", assignee="catch-all")
        out = db.resolve_assignee(conn, title="anything", tenant=None)
    assert out == "catch-all"


def test_title_regex_matches(kanban_home: Path):
    with db.connect() as conn:
        _add(conn, pattern_kind="title_regex", pattern="^deploy:", assignee="deployer")
        assert db.resolve_assignee(conn, title="deploy: prod", tenant=None) == "deployer"
        assert db.resolve_assignee(conn, title="ship: prod", tenant=None) is None


def test_tenant_matches_exact(kanban_home: Path):
    with db.connect() as conn:
        _add(conn, pattern_kind="tenant", pattern="ops", assignee="ops-bot")
        assert db.resolve_assignee(conn, title="anything", tenant="ops") == "ops-bot"
        assert db.resolve_assignee(conn, title="anything", tenant="dev") is None


def test_priority_order_first_match_wins(kanban_home: Path):
    with db.connect() as conn:
        _add(conn, pattern_kind="default", pattern="*", assignee="catch-all", priority=0)
        _add(conn, pattern_kind="title_regex", pattern="^urgent",
             assignee="urgent-bot", priority=100)
    with db.connect() as conn:
        # urgent-bot wins over catch-all due to higher priority
        assert db.resolve_assignee(conn, title="urgent: do it", tenant=None) == "urgent-bot"
        # catch-all picks up everything else
        assert db.resolve_assignee(conn, title="just normal", tenant=None) == "catch-all"


def test_no_match_returns_none(kanban_home: Path):
    with db.connect() as conn:
        _add(conn, pattern_kind="title_regex", pattern="^deploy:", assignee="deployer")
        assert db.resolve_assignee(conn, title="hello", tenant=None) is None


# ---- dispatch integration ----


def test_dispatch_assigns_unassigned_via_rule(kanban_home: Path):
    with db.connect() as conn:
        _add(conn, pattern_kind="default", pattern="*", assignee="auto-bot")
        # Create a task with no assignee
        tid = db.create_task(
            conn, title="t1", body=None, assignee=None,
        )
    # Dry-run dispatch should pick up the auto-assigned value
    with db.connect() as conn:
        res = db.dispatch_once(conn, dry_run=True)
    assigned_ids = [t[0] for t in res.spawned]
    assert tid in assigned_ids
    spawn_record = next(t for t in res.spawned if t[0] == tid)
    assert spawn_record[1] == "auto-bot"


def test_dispatch_skips_when_no_rule_matches(kanban_home: Path):
    with db.connect() as conn:
        tid = db.create_task(conn, title="t1", body=None, assignee=None)
    with db.connect() as conn:
        res = db.dispatch_once(conn, dry_run=True)
    assert tid in res.skipped_unassigned


def test_dispatch_explicit_assignee_overrides_rule(kanban_home: Path):
    """Rules only fire when assignee IS NULL — explicit always wins."""
    with db.connect() as conn:
        _add(conn, pattern_kind="default", pattern="*", assignee="auto-bot")
        tid = db.create_task(conn, title="t1", body=None, assignee="explicit-user")
    with db.connect() as conn:
        res = db.dispatch_once(conn, dry_run=True)
    spawn_record = next(t for t in res.spawned if t[0] == tid)
    assert spawn_record[1] == "explicit-user"


# ---- CLI roundtrip ----


def _run_cli(verb: str, *argv: str) -> tuple[int, str]:
    from opencomputer.kanban import cli as kbcli
    parser = argparse.ArgumentParser(prog="oc", add_help=False)
    sub = parser.add_subparsers(dest="cmd")
    kbcli.build_parser(sub)
    parsed = parser.parse_args(["kanban", verb, *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = kbcli.kanban_command(parsed) or 0
    return rc, buf.getvalue()


def test_cli_rules_add_list_rm_test(kanban_home: Path):
    rc, out = _run_cli(
        "rules", "add",
        "--kind", "title_regex",
        "--pattern", "^deploy:",
        "--assignee", "deploy-bot",
        "--priority", "10",
    )
    assert rc == 0
    assert "added rule" in out

    rc, out = _run_cli("rules", "list")
    assert rc == 0
    assert "deploy-bot" in out

    rc, out = _run_cli("rules", "test", "deploy: prod")
    assert rc == 0
    assert "deploy-bot" in out

    rc, out = _run_cli("rules", "test", "unrelated title")
    assert rc == 0
    assert "no rule matches" in out

    # rm
    with db.connect() as conn:
        rules = db.list_assignment_rules(conn)
    rid = rules[0]["id"]
    rc, _ = _run_cli("rules", "rm", str(rid))
    assert rc == 0


def test_cli_rules_add_bad_regex_fails(kanban_home: Path):
    rc, _ = _run_cli(
        "rules", "add",
        "--kind", "title_regex",
        "--pattern", "[unclosed",
        "--assignee", "x",
    )
    assert rc == 1
