"""M2 T2.4 — CLI tests for ``opencomputer awareness migrate``.

``migrate`` cleans up legacy graph cruft: flags agent-internal-noise
nodes with a ``needs_review`` marker and collapses the duplicate edges
left by the pre-M2 fresh-uuid importer. Dry-run by default; ``--apply``
mutates.
"""
from __future__ import annotations

from typer.testing import CliRunner

from opencomputer.cli import app
from plugin_sdk.user_model import Edge, Node

runner = CliRunner()


def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", "200")
    from opencomputer.user_model.store import UserModelStore

    return UserModelStore()


def _seed_noise(store):
    """A node the validator rejects (agent-internal label)."""
    store.insert_node(Node(node_id="noise-1", kind="attribute",
                           value="uses agent_loop"))


def _seed_dup_edges(store):
    """One node pair joined by 4 redundant edges."""
    store.insert_node(Node(node_id="da", kind="attribute", value="da-ok"))
    store.insert_node(Node(node_id="db", kind="preference", value="db-ok"))
    for i in range(4):
        store.insert_edge(Edge(edge_id=f"d{i}", kind="asserts",
                               from_node="da", to_node="db",
                               source="motif_importer", created_at=10.0 + i))


def test_migrate_dry_run_reports_and_changes_nothing(tmp_path, monkeypatch):
    """Dry-run prints a plan but mutates neither nodes nor edges."""
    store = _store(tmp_path, monkeypatch)
    _seed_noise(store)
    _seed_dup_edges(store)
    result = runner.invoke(app, ["awareness", "migrate"])
    assert result.exit_code == 0, result.stdout
    assert "dry run" in result.stdout.lower()
    # Nothing changed.
    noise = store.get_node("noise-1")
    assert noise is not None and not noise.metadata.get("needs_review")
    assert store.count_edges() == 4


def test_migrate_apply_flags_noise_nodes(tmp_path, monkeypatch):
    """--apply marks validator-rejected nodes needs_review with a reason."""
    store = _store(tmp_path, monkeypatch)
    _seed_noise(store)
    result = runner.invoke(app, ["awareness", "migrate", "--apply"])
    assert result.exit_code == 0, result.stdout
    node = store.get_node("noise-1")
    assert node is not None
    assert node.metadata.get("needs_review") is True
    assert node.metadata.get("review_reason")


def test_migrate_apply_collapses_duplicate_edges(tmp_path, monkeypatch):
    """--apply collapses the 4 redundant edges down to 1."""
    store = _store(tmp_path, monkeypatch)
    _seed_dup_edges(store)
    result = runner.invoke(app, ["awareness", "migrate", "--apply"])
    assert result.exit_code == 0, result.stdout
    assert store.count_edges() == 1


def test_migrate_leaves_legit_nodes_untouched(tmp_path, monkeypatch):
    """A genuine fact is neither flagged nor altered."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="ok-1", kind="goal", value="learn Rust"))
    runner.invoke(app, ["awareness", "migrate", "--apply"])
    node = store.get_node("ok-1")
    assert node is not None and not node.metadata.get("needs_review")


def test_migrate_apply_is_idempotent(tmp_path, monkeypatch):
    """A second --apply run flags nothing new and collapses nothing new."""
    store = _store(tmp_path, monkeypatch)
    _seed_noise(store)
    _seed_dup_edges(store)
    runner.invoke(app, ["awareness", "migrate", "--apply"])
    second = runner.invoke(app, ["awareness", "migrate", "--apply"])
    assert second.exit_code == 0, second.stdout
    # Specific: a bare "0" anywhere in stdout also matched timestamps and
    # any number containing a zero. Assert the actual report lines.
    assert "facts flagged needs_review: 0" in second.stdout
    assert "duplicate edges collapsed: 0" in second.stdout
    assert store.count_edges() == 1


def test_review_needs_review_filter(tmp_path, monkeypatch):
    """`review --needs-review` shows only the flagged nodes."""
    store = _store(tmp_path, monkeypatch)
    store.insert_node(Node(node_id="flagged", kind="attribute",
                           value="FLAGGEDFACT",
                           metadata={"needs_review": True}))
    store.insert_node(Node(node_id="normal", kind="attribute",
                           value="NORMALFACT"))
    result = runner.invoke(app, ["awareness", "review", "--needs-review"])
    assert result.exit_code == 0, result.stdout
    assert "FLAGGEDFACT" in result.stdout
    assert "NORMALFACT" not in result.stdout
