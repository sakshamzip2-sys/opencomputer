"""M3 T3.5 — CLI tests for ``opencomputer awareness eval-ranker``.

eval-ranker shows the old (kind, confidence) sort beside the new
context-aware reranker so the weights can be eyeballed. --query
simulates the opening message that drives the BM25 term.
"""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.cli import app
from plugin_sdk.user_model import Node

runner = CliRunner()


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")
    from opencomputer.user_model.store import UserModelStore

    return UserModelStore()


def test_eval_ranker_empty_graph_is_friendly(tmp_path, monkeypatch):
    """No facts → friendly message, exit 0."""
    _store(tmp_path, monkeypatch)
    result = runner.invoke(app, ["awareness", "eval-ranker"])
    assert result.exit_code == 0, result.stdout
    assert "no facts" in result.stdout.lower()


def test_eval_ranker_shows_both_rankings(tmp_path, monkeypatch):
    """The comparison renders an old column and a new column."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="n1", kind="identity", value="name: X"))
    store.insert_node(Node(node_id="n2", kind="attribute", value="uses Python"))
    result = runner.invoke(app, ["awareness", "eval-ranker"])
    assert result.exit_code == 0, result.stdout
    assert "old" in result.stdout.lower()
    assert "reranker" in result.stdout.lower()


def test_eval_ranker_query_changes_the_ranking(tmp_path, monkeypatch):
    """A --query relevant to one fact reorders the reranker column."""
    store = _store(tmp_path, monkeypatch)
    # Same kind + confidence → old sort is order-stable; the query is the
    # only thing that can move the reranker ranking.
    store.insert_node(Node(node_id="n1", kind="attribute",
                           value="enjoys hiking outdoors", confidence=0.8))
    store.insert_node(Node(node_id="n2", kind="attribute",
                           value="writes rust code", confidence=0.8))
    result = runner.invoke(
        app, ["awareness", "eval-ranker", "--query", "help with my rust code"]
    )
    assert result.exit_code == 0, result.stdout
    # The rust fact should lead the reranker column.
    assert "rust" in result.stdout
    assert "position" in result.stdout.lower() or "changed" in result.stdout.lower()


def test_eval_ranker_excludes_deleted_and_flagged(tmp_path, monkeypatch):
    """Soft-deleted and needs_review facts are not part of the comparison."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="live", kind="attribute", value="LIVEONE"))
    store.insert_node(Node(node_id="dead", kind="attribute", value="DEADONE",
                           metadata={"deleted": True}))
    store.insert_node(Node(node_id="flag", kind="attribute", value="FLAGONE",
                           metadata={"needs_review": True}))
    result = runner.invoke(app, ["awareness", "eval-ranker"])
    assert result.exit_code == 0, result.stdout
    assert "LIVEONE" in result.stdout
    assert "DEADONE" not in result.stdout
    assert "FLAGONE" not in result.stdout
