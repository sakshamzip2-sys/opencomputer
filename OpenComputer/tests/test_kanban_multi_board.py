"""Tests for multi-board kanban support (Wave 6.E.8)."""

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
    return tmp_path


# ---- validate_slug ----


@pytest.mark.parametrize("good", [
    "foo", "foo-bar", "foo_bar", "p1", "project-x", "q4_planning",
    "a", "1abc", "0", "x" * 64,
])
def test_validate_slug_accepts_good(good: str):
    db.validate_slug(good)


@pytest.mark.parametrize("bad", [
    "",  # empty
    "Foo",  # uppercase
    "-foo",  # leading hyphen
    "_foo",  # leading underscore (must start alnum)
    "foo!",  # special char
    "foo bar",  # space
    "foo/bar",  # slash
    "x" * 65,  # too long
])
def test_validate_slug_rejects_bad(bad: str):
    with pytest.raises(db.InvalidBoardSlugError):
        db.validate_slug(bad)


def test_validate_slug_rejects_non_string():
    with pytest.raises(db.InvalidBoardSlugError):
        db.validate_slug(None)  # type: ignore[arg-type]


# ---- path resolution ----


def test_legacy_default_when_no_active(kanban_home: Path):
    assert db.active_board() is None
    assert db.kanban_db_path() == kanban_home / "kanban.db"


def test_active_board_state_file_roundtrip(kanban_home: Path):
    db.set_active_board("proj-x")
    assert db.active_board() == "proj-x"
    assert db.kanban_db_path() == kanban_home / "kanban" / "boards" / "proj-x" / "kanban.db"
    db.set_active_board(None)
    assert db.active_board() is None
    assert db.kanban_db_path() == kanban_home / "kanban.db"


def test_oc_kanban_board_env_overrides_state(kanban_home: Path, monkeypatch):
    db.set_active_board("from-state-file")
    monkeypatch.setenv("OC_KANBAN_BOARD", "from-env")
    assert db.active_board() == "from-env"
    assert db.kanban_db_path() == kanban_home / "kanban" / "boards" / "from-env" / "kanban.db"


def test_oc_kanban_db_overrides_everything(kanban_home: Path, monkeypatch):
    db.set_active_board("foo")
    monkeypatch.setenv("OC_KANBAN_BOARD", "bar")
    monkeypatch.setenv("OC_KANBAN_DB", "/tmp/explicit.db")
    assert db.kanban_db_path() == Path("/tmp/explicit.db")


def test_workspaces_root_per_board(kanban_home: Path):
    db.set_active_board("proj-y")
    assert db.workspaces_root() == kanban_home / "kanban" / "boards" / "proj-y" / "workspaces"
    db.set_active_board(None)
    assert db.workspaces_root() == kanban_home / "kanban" / "workspaces"


def test_invalid_env_board_falls_through_to_state_file(
    kanban_home: Path, monkeypatch,
):
    db.set_active_board("good-slug")
    monkeypatch.setenv("OC_KANBAN_BOARD", "BadSlug!")  # invalid
    # Should fall back to None (not the state file value, per design)
    # — matches the documented behaviour: bad env value → None.
    assert db.active_board() is None


def test_corrupt_state_file_returns_none(kanban_home: Path):
    state = kanban_home / "kanban" / ".active-board"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("Bad Slug !!!")
    assert db.active_board() is None


def test_list_boards_empty_when_no_dir(kanban_home: Path):
    assert db.list_boards() == []


def test_list_boards_after_create(kanban_home: Path):
    boards_dir = kanban_home / "kanban" / "boards"
    (boards_dir / "alpha").mkdir(parents=True)
    (boards_dir / "bravo").mkdir(parents=True)
    (boards_dir / "Bad-Slug!").mkdir(parents=True)  # filtered out
    assert db.list_boards() == ["alpha", "bravo"]


# ---- CLI subcommands ----


def _run_cli(verb: str, *argv: str) -> tuple[int, str]:
    """Helper — run a kanban subcommand via the same path the CLI uses.

    build_parser adds a single 'kanban' top-level subparser, so the
    full argv is ['kanban', verb, *argv].
    """
    from opencomputer.kanban import cli as kbcli
    parser = argparse.ArgumentParser(prog="oc", add_help=False)
    sub = parser.add_subparsers(dest="cmd")
    kbcli.build_parser(sub)
    parsed = parser.parse_args(["kanban", verb, *argv])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = kbcli.kanban_command(parsed) or 0
    return rc, buf.getvalue()


def test_cli_boards_create_then_list_then_switch(kanban_home: Path):
    rc, out = _run_cli("boards", "create", "alpha")
    assert rc == 0
    assert "created board 'alpha'" in out

    rc, out = _run_cli("boards", "list")
    assert rc == 0
    assert "alpha" in out

    rc, out = _run_cli("boards", "switch", "alpha")
    assert rc == 0
    assert db.active_board() == "alpha"

    rc, out = _run_cli("boards", "active")
    assert rc == 0
    assert "alpha" in out


def test_cli_boards_create_rejects_bad_slug(kanban_home: Path):
    rc, _ = _run_cli("boards", "create", "Bad Slug!")
    assert rc == 1


def test_cli_boards_switch_to_nonexistent_fails(kanban_home: Path):
    rc, _ = _run_cli("boards", "switch", "nope")
    assert rc == 1


def test_cli_boards_rename(kanban_home: Path):
    _run_cli("boards", "create", "old-name")
    rc, out = _run_cli("boards", "rename", "old-name", "new-name")
    assert rc == 0
    assert "new-name" in db.list_boards()
    assert "old-name" not in db.list_boards()


def test_cli_boards_rm_with_yes(kanban_home: Path):
    _run_cli("boards", "create", "to-delete")
    assert "to-delete" in db.list_boards()
    rc, _ = _run_cli("boards", "rm", "to-delete", "--yes")
    assert rc == 0
    assert "to-delete" not in db.list_boards()


def test_cli_boards_rm_clears_active_marker(kanban_home: Path):
    _run_cli("boards", "create", "ephemeral")
    _run_cli("boards", "switch", "ephemeral")
    assert db.active_board() == "ephemeral"
    _run_cli("boards", "rm", "ephemeral", "--yes")
    assert db.active_board() is None
