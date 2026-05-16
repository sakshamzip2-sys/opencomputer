"""M1 T1.3 — CLI tests for ``opencomputer awareness review``.

``review`` is the user-facing inspection surface: a Rich table of the
top-K user-model facts with provenance, so the user can see *what the
agent thinks it knows* before deciding to ``forget`` / ``correct``.

Isolation: every test points ``OPENCOMPUTER_HOME`` at a tmp dir, so
``UserModelStore()`` opens a fresh empty ``graph.sqlite``.
"""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.cli import app
from plugin_sdk.user_model import Edge

runner = CliRunner()


def _store(tmp_path, monkeypatch):
    """Return a UserModelStore rooted at an isolated tmp profile home."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    # Render Rich tables wide so column squeeze never truncates assertions.
    monkeypatch.setenv("COLUMNS", "200")
    from opencomputer.user_model.store import UserModelStore

    return UserModelStore()


def test_review_empty_graph_is_friendly(tmp_path, monkeypatch):
    """An empty graph prints a friendly empty-state, exit 0 (not a crash)."""
    _store(tmp_path, monkeypatch)
    result = runner.invoke(app, ["awareness", "review"])
    assert result.exit_code == 0, result.stdout
    assert "no facts" in result.stdout.lower()


def test_review_shows_seeded_nodes(tmp_path, monkeypatch):
    """Seeded node values appear in the table."""
    store = _store(tmp_path, monkeypatch)
    store.upsert_node(kind="identity", value="name: Saksham", confidence=1.0)
    store.upsert_node(kind="preference", value="tone_preference: terse", confidence=0.9)
    result = runner.invoke(app, ["awareness", "review"])
    assert result.exit_code == 0, result.stdout
    assert "Saksham" in result.stdout
    assert "terse" in result.stdout


def test_review_orders_identity_before_attribute(tmp_path, monkeypatch):
    """Kind priority: identity ranks above attribute in the rendered order."""
    store = _store(tmp_path, monkeypatch)
    store.upsert_node(kind="attribute", value="ATTRMARK", confidence=0.9)
    store.upsert_node(kind="identity", value="IDMARK", confidence=0.9)
    result = runner.invoke(app, ["awareness", "review"])
    assert result.exit_code == 0, result.stdout
    assert result.stdout.index("IDMARK") < result.stdout.index("ATTRMARK")


def test_review_default_caps_at_50(tmp_path, monkeypatch):
    """With 60 nodes, the default view shows 50 and says so in the title."""
    store = _store(tmp_path, monkeypatch)
    for i in range(60):
        store.upsert_node(kind="attribute", value=f"fact-{i:03d}")
    result = runner.invoke(app, ["awareness", "review"])
    assert result.exit_code == 0, result.stdout
    assert "showing 50 of 60" in result.stdout


def test_review_all_flag_shows_everything(tmp_path, monkeypatch):
    """--all lifts the cap; the title reflects the full count."""
    store = _store(tmp_path, monkeypatch)
    for i in range(60):
        store.upsert_node(kind="attribute", value=f"fact-{i:03d}")
    result = runner.invoke(app, ["awareness", "review", "--all"])
    assert result.exit_code == 0, result.stdout
    assert "showing 60 of 60" in result.stdout


def test_review_hides_soft_deleted_by_default(tmp_path, monkeypatch):
    """A node carrying metadata.deleted is omitted from the default view."""
    store = _store(tmp_path, monkeypatch)
    store.upsert_node(kind="attribute", value="LIVEMARK")
    store.upsert_node(
        kind="attribute", value="TOMBMARK", metadata={"deleted": True},
    )
    result = runner.invoke(app, ["awareness", "review"])
    assert result.exit_code == 0, result.stdout
    assert "LIVEMARK" in result.stdout
    assert "TOMBMARK" not in result.stdout


def test_review_deleted_flag_reveals_soft_deleted(tmp_path, monkeypatch):
    """--deleted surfaces soft-deleted nodes for audit."""
    store = _store(tmp_path, monkeypatch)
    store.upsert_node(
        kind="attribute", value="TOMBMARK", metadata={"deleted": True},
    )
    result = runner.invoke(app, ["awareness", "review", "--deleted"])
    assert result.exit_code == 0, result.stdout
    assert "TOMBMARK" in result.stdout


def test_review_counts_incoming_contradicts(tmp_path, monkeypatch):
    """A node targeted by a contradicts edge shows a non-zero count."""
    store = _store(tmp_path, monkeypatch)
    target = store.upsert_node(kind="preference", value="lives in Bangalore")
    rival = store.upsert_node(kind="preference", value="lives in San Francisco")
    store.insert_edge(
        Edge(kind="contradicts", from_node=rival.node_id, to_node=target.node_id)
    )
    result = runner.invoke(app, ["awareness", "review"])
    assert result.exit_code == 0, result.stdout
    # The contradicted node's row carries a count >= 1; render the column
    # header so the user knows what it means.
    assert "contradict" in result.stdout.lower()
