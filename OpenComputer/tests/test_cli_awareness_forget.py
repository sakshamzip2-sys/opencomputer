"""M1 T1.5 — CLI tests for ``opencomputer awareness forget <id>``.

``forget`` is the user's recourse when the agent learns something wrong.
Default is a reversible soft-delete (``metadata.deleted`` flag); ``--hard``
drops the row (cascading its edges). Identity-kind facts are foundational
— forgetting one requires an explicit ``--confirm``.
"""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.cli import app
from plugin_sdk.user_model import Edge, Node

runner = CliRunner()


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")  # wide render — no table truncation
    from opencomputer.user_model.store import UserModelStore

    return UserModelStore()


def test_forget_soft_delete_sets_tombstone_flag(tmp_path, monkeypatch):
    """Default forget keeps the row but flags metadata.deleted = True."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="pref-1", kind="preference",
                           value="prefers dark mode"))
    result = runner.invoke(app, ["awareness", "forget", "pref-1"])
    assert result.exit_code == 0, result.stdout
    node = store.get_node("pref-1")
    assert node is not None, "soft-delete must keep the row"
    assert node.metadata.get("deleted") is True


def test_forget_soft_deleted_drops_out_of_review(tmp_path, monkeypatch):
    """End-to-end: a forgotten fact no longer shows in `awareness review`."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="pref-2", kind="preference",
                           value="GHOSTFACT"))
    runner.invoke(app, ["awareness", "forget", "pref-2"])
    review = runner.invoke(app, ["awareness", "review"])
    assert review.exit_code == 0
    assert "GHOSTFACT" not in review.stdout


def test_forget_hard_delete_removes_row(tmp_path, monkeypatch):
    """--hard drops the node row entirely."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="attr-1", kind="attribute", value="x"))
    result = runner.invoke(app, ["awareness", "forget", "attr-1", "--hard"])
    assert result.exit_code == 0, result.stdout
    assert store.get_node("attr-1") is None


def test_forget_hard_delete_cascades_edges(tmp_path, monkeypatch):
    """Hard delete drops the node's incident edges via ON DELETE CASCADE."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="edge-a", kind="attribute", value="a"))
    store.insert_node(Node(node_id="edge-b", kind="attribute", value="b"))
    store.insert_edge(Edge(kind="asserts", from_node="edge-a", to_node="edge-b"))
    runner.invoke(app, ["awareness", "forget", "edge-a", "--hard"])
    assert store.get_node("edge-a") is None
    assert store.list_edges(from_node="edge-a") == []


def test_forget_identity_without_confirm_is_refused(tmp_path, monkeypatch):
    """Identity facts are foundational — forget without --confirm is refused."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="id-1", kind="identity",
                           value="name: Saksham", confidence=1.0))
    result = runner.invoke(app, ["awareness", "forget", "id-1"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (getattr(result, "stderr", "") or "")
    assert "--confirm" in combined
    # The node must survive a refused forget.
    survivor = store.get_node("id-1")
    assert survivor is not None
    assert not survivor.metadata.get("deleted")


def test_forget_identity_with_confirm_succeeds(tmp_path, monkeypatch):
    """--confirm lets an identity fact be (soft) forgotten."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="id-2", kind="identity",
                           value="city: Pune", confidence=1.0))
    result = runner.invoke(
        app, ["awareness", "forget", "id-2", "--confirm"]
    )
    assert result.exit_code == 0, result.stdout
    node = store.get_node("id-2")
    assert node is not None and node.metadata.get("deleted") is True


def test_forget_identity_hard_with_confirm_removes_row(tmp_path, monkeypatch):
    """--hard --confirm removes an identity row outright."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="id-3", kind="identity",
                           value="email: x@y.com", confidence=1.0))
    result = runner.invoke(
        app, ["awareness", "forget", "id-3", "--hard", "--confirm"]
    )
    assert result.exit_code == 0, result.stdout
    assert store.get_node("id-3") is None


def test_forget_unknown_id_exits_nonzero(tmp_path, monkeypatch):
    """Forgetting a non-existent id exits 1."""
    _store(tmp_path, monkeypatch)
    result = runner.invoke(app, ["awareness", "forget", "nope"])
    assert result.exit_code == 1


def test_forget_soft_delete_preserves_incident_edges(tmp_path, monkeypatch):
    """Soft-delete keeps the node's edges.

    Regression: a metadata write via insert_node (INSERT OR REPLACE)
    would cascade-drop incident edges. Soft-delete must be reversible —
    that requires the edges to survive.
    """
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="keep-a", kind="attribute", value="a"))
    store.insert_node(Node(node_id="keep-b", kind="attribute", value="b"))
    store.insert_edge(Edge(kind="asserts", from_node="keep-a", to_node="keep-b"))
    result = runner.invoke(app, ["awareness", "forget", "keep-a"])
    assert result.exit_code == 0, result.stdout
    assert store.count_edges() == 1
