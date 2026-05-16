"""M1 T1.6 — CLI tests for ``opencomputer awareness correct <id> <new>``.

``correct`` is the user fixing a fact. It does three things atomically:
create the new-valued node, write a ``supersedes`` edge new→old (the
durable provenance M3/M4 honor), and soft-delete the old node so the
correction takes effect immediately. Identity facts need ``--confirm``.
"""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.cli import app
from plugin_sdk.user_model import Node

runner = CliRunner()


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")  # wide render — no table truncation
    from opencomputer.user_model.store import UserModelStore

    return UserModelStore()


def test_correct_creates_new_valued_node(tmp_path, monkeypatch):
    """A node carrying the corrected value exists after the command."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="old-1", kind="preference",
                           value="lives in Bangalore"))
    result = runner.invoke(
        app, ["awareness", "correct", "old-1", "lives in San Francisco"]
    )
    assert result.exit_code == 0, result.stdout
    matches = [n for n in store.list_nodes(limit=1000)
               if n.value == "lives in San Francisco"]
    assert len(matches) == 1


def test_correct_writes_supersedes_edge_new_to_old(tmp_path, monkeypatch):
    """A supersedes edge points from the new node to the old one."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="old-2", kind="preference",
                           value="uses tabs"))
    runner.invoke(app, ["awareness", "correct", "old-2", "uses spaces"])
    supersedes = store.list_edges(kind="supersedes")
    assert len(supersedes) == 1
    edge = supersedes[0]
    assert edge.to_node == "old-2"
    new_node = store.get_node(edge.from_node)
    assert new_node is not None and new_node.value == "uses spaces"


def test_correct_soft_deletes_the_old_node(tmp_path, monkeypatch):
    """The corrected-away node is soft-deleted, not left live."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="old-3", kind="preference",
                           value="wrong fact"))
    runner.invoke(app, ["awareness", "correct", "old-3", "right fact"])
    old = store.get_node("old-3")
    assert old is not None and old.metadata.get("deleted") is True


def test_correct_takes_effect_in_review(tmp_path, monkeypatch):
    """End-to-end: review shows the new value and hides the old."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="old-4", kind="preference",
                           value="OLDFACT"))
    runner.invoke(app, ["awareness", "correct", "old-4", "NEWFACT"])
    review = runner.invoke(app, ["awareness", "review"])
    assert review.exit_code == 0
    assert "NEWFACT" in review.stdout
    assert "OLDFACT" not in review.stdout


def test_correct_new_node_keeps_old_kind(tmp_path, monkeypatch):
    """Correcting a value keeps the node's kind (you fix the value, not kind)."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="old-5", kind="goal",
                           value="learn Rust"))
    runner.invoke(app, ["awareness", "correct", "old-5", "learn Go"])
    matches = [n for n in store.list_nodes(limit=1000)
               if n.value == "learn Go"]
    assert len(matches) == 1
    assert matches[0].kind == "goal"


def test_correct_identity_without_confirm_is_refused(tmp_path, monkeypatch):
    """Correcting an identity fact without --confirm is refused, exit 1."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="id-x", kind="identity",
                           value="name: Sakshamm", confidence=1.0))
    result = runner.invoke(
        app, ["awareness", "correct", "id-x", "name: Saksham"]
    )
    assert result.exit_code == 1
    combined = (result.stdout or "") + (getattr(result, "stderr", "") or "")
    assert "--confirm" in combined
    survivor = store.get_node("id-x")
    assert survivor is not None
    assert not survivor.metadata.get("deleted")


def test_correct_identity_with_confirm_succeeds(tmp_path, monkeypatch):
    """--confirm lets an identity fact be corrected."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="id-y", kind="identity",
                           value="city: Mumbia", confidence=1.0))
    result = runner.invoke(
        app, ["awareness", "correct", "id-y", "city: Mumbai", "--confirm"]
    )
    assert result.exit_code == 0, result.stdout
    corrected = store.get_node("id-y")
    assert corrected is not None
    assert corrected.metadata.get("deleted") is True


def test_correct_unknown_id_exits_nonzero(tmp_path, monkeypatch):
    """Correcting a non-existent id exits 1."""
    _store(tmp_path, monkeypatch)
    result = runner.invoke(app, ["awareness", "correct", "ghost", "whatever"])
    assert result.exit_code == 1


def test_correct_to_identical_value_is_noop(tmp_path, monkeypatch):
    """Correcting a fact to its current value changes nothing, exit 0."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="same-1", kind="preference",
                           value="unchanged"))
    result = runner.invoke(
        app, ["awareness", "correct", "same-1", "unchanged"]
    )
    assert result.exit_code == 0
    assert store.list_edges(kind="supersedes") == []
    unchanged = store.get_node("same-1")
    assert unchanged is not None
    assert not unchanged.metadata.get("deleted")
