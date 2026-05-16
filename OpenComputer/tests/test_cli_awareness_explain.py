"""M1 T1.4 — CLI tests for ``opencomputer awareness explain <id>``.

``explain`` shows full provenance for one node: its fields, every
incident edge, and the live decay-adjusted recency weight. It accepts
a full node id OR a unique id prefix (the 8-char form ``review`` prints).
"""
from __future__ import annotations

import time

from typer.testing import CliRunner

from opencomputer.cli import app
from plugin_sdk.user_model import Edge, Node

runner = CliRunner()


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")  # wide render — no table truncation
    from opencomputer.user_model.store import UserModelStore

    return UserModelStore()


def test_explain_unknown_id_exits_nonzero(tmp_path, monkeypatch):
    """Explaining a non-existent id exits 1 with a clear message."""
    _store(tmp_path, monkeypatch)
    result = runner.invoke(app, ["awareness", "explain", "does-not-exist"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (getattr(result, "stderr", "") or "")
    assert "no fact" in combined.lower() or "not found" in combined.lower()


def test_explain_shows_node_fields(tmp_path, monkeypatch):
    """Explain by full id renders the node's kind, value and confidence."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(
        Node(node_id="node-aaaa-1111", kind="preference",
             value="tone_preference: terse", confidence=0.87)
    )
    result = runner.invoke(app, ["awareness", "explain", "node-aaaa-1111"])
    assert result.exit_code == 0, result.stdout
    assert "preference" in result.stdout
    assert "terse" in result.stdout
    assert "0.87" in result.stdout


def test_explain_resolves_unique_prefix(tmp_path, monkeypatch):
    """A unique id prefix resolves to the one matching node."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="prefixmatch-001", kind="attribute",
                           value="UNIQUEVALUE"))
    result = runner.invoke(app, ["awareness", "explain", "prefixmatch"])
    assert result.exit_code == 0, result.stdout
    assert "UNIQUEVALUE" in result.stdout


def test_explain_ambiguous_prefix_exits_nonzero(tmp_path, monkeypatch):
    """A prefix matching 2+ nodes exits 1 and lists the candidates."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="dup-1", kind="attribute", value="first"))
    store.insert_node(Node(node_id="dup-2", kind="attribute", value="second"))
    result = runner.invoke(app, ["awareness", "explain", "dup-"])
    assert result.exit_code == 1
    combined = (result.stdout or "") + (getattr(result, "stderr", "") or "")
    assert "ambiguous" in combined.lower()
    # Both candidate ids surface so the user can disambiguate.
    assert "dup-1" in combined and "dup-2" in combined


def test_explain_shows_incident_edges(tmp_path, monkeypatch):
    """Incident edges (and the node on the other end) appear in the output."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="src-node", kind="attribute",
                           value="uses Python"))
    store.insert_node(Node(node_id="dst-node", kind="preference",
                           value="prefers Python"))
    store.insert_edge(Edge(kind="asserts", from_node="src-node",
                           to_node="dst-node"))
    result = runner.invoke(app, ["awareness", "explain", "src-node"])
    assert result.exit_code == 0, result.stdout
    assert "asserts" in result.stdout
    # The far end of the edge is identified.
    assert "dst-node"[:8] in result.stdout or "prefers Python" in result.stdout


def test_explain_reports_live_decay_weight(tmp_path, monkeypatch):
    """An aged edge shows a live decay-adjusted weight below its stored 1.0."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="aged-a", kind="attribute", value="A"))
    store.insert_node(Node(node_id="aged-b", kind="attribute", value="B"))
    old = time.time() - 86400 * 120  # 120 days old
    store.insert_edge(Edge(kind="asserts", from_node="aged-a", to_node="aged-b",
                           recency_weight=1.0, created_at=old))
    result = runner.invoke(app, ["awareness", "explain", "aged-a"])
    assert result.exit_code == 0, result.stdout
    assert "decay" in result.stdout.lower()


def test_explain_orphan_node_is_handled(tmp_path, monkeypatch):
    """A node with no incident edges explains cleanly, no crash."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="lonely", kind="identity", value="name: X"))
    result = runner.invoke(app, ["awareness", "explain", "lonely"])
    assert result.exit_code == 0, result.stdout
    assert "no incident edges" in result.stdout.lower()


# ─── M3 T3.6 — explain --session score breakdown ─────────────────────


def test_explain_session_shows_score_breakdown(tmp_path, monkeypatch):
    """`explain --session` renders the reranker per-term score breakdown."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="s1", kind="identity", value="name: X"))
    store.insert_node(Node(node_id="s2", kind="attribute",
                           value="uses Python"))
    result = runner.invoke(app, ["awareness", "explain", "--session"])
    assert result.exit_code == 0, result.stdout
    out = result.stdout.lower()
    assert "kind" in out and "recency" in out and "bm25" in out


def test_explain_session_with_query_runs_bm25(tmp_path, monkeypatch):
    """--session --query exercises the BM25 term and still renders."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="s1", kind="attribute",
                           value="writes rust code"))
    result = runner.invoke(
        app, ["awareness", "explain", "--session", "--query", "rust help"]
    )
    assert result.exit_code == 0, result.stdout
    assert "rust" in result.stdout


def test_explain_with_no_id_and_no_session_exits_nonzero(
    tmp_path, monkeypatch
):
    """`explain` with neither a node id nor --session is an error."""
    _store(tmp_path, monkeypatch)
    result = runner.invoke(app, ["awareness", "explain"])
    assert result.exit_code == 1
