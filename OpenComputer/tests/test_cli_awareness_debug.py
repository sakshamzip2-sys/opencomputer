"""M5 T5.2 — CLI tests for ``opencomputer awareness debug``.

`debug` is the machine-readable counterpart of `explain --session`: a
JSON dump (graph counts, reranker weights, top-ranked facts with score
breakdowns) suitable for pasting into a bug report.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from opencomputer.cli import app
from plugin_sdk.user_model import Node

runner = CliRunner()


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")
    from opencomputer.user_model.store import UserModelStore

    return UserModelStore()


def test_debug_outputs_valid_json(tmp_path, monkeypatch):
    """The command emits parseable JSON, exit 0."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="d1", kind="identity", value="name: X"))
    result = runner.invoke(app, ["awareness", "debug"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)


def test_debug_reports_graph_counts(tmp_path, monkeypatch):
    """Graph section counts nodes, and the needs_review flag."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="d1", kind="identity", value="name: X"))
    store.insert_node(Node(node_id="d2", kind="attribute", value="y",
                           metadata={"needs_review": True}))
    payload = json.loads(runner.invoke(app, ["awareness", "debug"]).stdout)
    assert payload["graph"]["nodes_total"] == 2
    assert payload["graph"]["needs_review"] == 1


def test_debug_top_facts_carry_score_breakdown(tmp_path, monkeypatch):
    """Each top fact carries the reranker score + per-term breakdown."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="d1", kind="identity", value="name: X"))
    payload = json.loads(runner.invoke(app, ["awareness", "debug"]).stdout)
    assert len(payload["top_facts"]) == 1
    fact = payload["top_facts"][0]
    assert "score" in fact
    assert set(fact["breakdown"]) == {
        "kind", "confidence", "recency", "bm25", "drift",
    }


def test_debug_empty_graph(tmp_path, monkeypatch):
    """An empty graph produces valid JSON with no facts."""
    _store(tmp_path, monkeypatch)
    payload = json.loads(runner.invoke(app, ["awareness", "debug"]).stdout)
    assert payload["graph"]["nodes_total"] == 0
    assert payload["top_facts"] == []
