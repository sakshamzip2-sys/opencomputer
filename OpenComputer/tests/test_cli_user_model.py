"""CLI tests for ``opencomputer user-model ...`` (Phase 3.C)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli_user_model import user_model_app
from opencomputer.inference.storage import MotifStore
from opencomputer.user_model.importer import MotifImporter
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.inference import Motif

runner = CliRunner()


def _patched_stores(user_db: Path, motif_db: Path | None = None):
    """Patch both store classes in ``cli_user_model`` + the importer's
    :class:`MotifStore` construction so every CLI command sees the
    isolated tmp DBs instead of the global ``~/.opencomputer`` path.
    """
    patches = [
        patch(
            "opencomputer.cli_user_model.UserModelStore",
            return_value=UserModelStore(db_path=user_db),
        ),
    ]
    if motif_db is not None:
        def _fresh_importer(*args, **kwargs):  # type: ignore[no-untyped-def]
            return MotifImporter(
                store=UserModelStore(db_path=user_db),
                motif_store=MotifStore(db_path=motif_db),
            )

        patches.append(
            patch(
                "opencomputer.cli_user_model.MotifImporter",
                side_effect=_fresh_importer,
            )
        )
    return patches


def test_nodes_list_shows_added_node(tmp_path: Path) -> None:
    """Manually added node shows up in ``nodes list``."""
    db = tmp_path / "graph.sqlite"
    # Seed directly via a plain store.
    UserModelStore(db_path=db).upsert_node(
        kind="attribute", value="uses Python"
    )

    with patch(
        "opencomputer.cli_user_model.UserModelStore",
        return_value=UserModelStore(db_path=db),
    ):
        result = runner.invoke(user_model_app, ["nodes", "list"])
    assert result.exit_code == 0
    assert "uses Python" in result.stdout


def test_nodes_add_inserts_node(tmp_path: Path) -> None:
    """``nodes add`` persists a node the subsequent list can see."""
    db = tmp_path / "graph.sqlite"
    UserModelStore(db_path=db)  # init schema

    with patch(
        "opencomputer.cli_user_model.UserModelStore",
        return_value=UserModelStore(db_path=db),
    ):
        add_result = runner.invoke(
            user_model_app,
            ["nodes", "add", "--kind", "goal", "--value", "ship 3.C"],
        )
        assert add_result.exit_code == 0
        list_result = runner.invoke(user_model_app, ["nodes", "list"])

    assert "ship 3.C" in list_result.stdout


def test_search_finds_node(tmp_path: Path) -> None:
    """FTS5 search routes through the store's ``search_nodes_fts``."""
    db = tmp_path / "graph.sqlite"
    store = UserModelStore(db_path=db)
    store.upsert_node(kind="attribute", value="uses Python tooling")
    store.upsert_node(kind="attribute", value="prefers JavaScript")

    with patch(
        "opencomputer.cli_user_model.UserModelStore",
        return_value=UserModelStore(db_path=db),
    ):
        result = runner.invoke(user_model_app, ["search", "Python"])
    assert result.exit_code == 0
    assert "Python" in result.stdout
    assert "JavaScript" not in result.stdout


def test_import_motifs_runs_without_error(tmp_path: Path) -> None:
    """``import-motifs`` picks up a seeded motif and reports non-zero counts."""
    user_db = tmp_path / "graph.sqlite"
    motif_db = tmp_path / "motifs.sqlite"
    # Seed one motif into the motif-store.
    ms = MotifStore(db_path=motif_db)
    ms.insert(
        Motif(
            kind="temporal",
            confidence=0.8,
            support=5,
            summary="",
            payload={"label": "Read", "hour": 9, "day_of_week": 1, "count": 5},
            evidence_event_ids=("e1",),
        )
    )

    def _importer_factory(*args, **kwargs):  # type: ignore[no-untyped-def]
        return MotifImporter(
            store=UserModelStore(db_path=user_db),
            motif_store=MotifStore(db_path=motif_db),
        )

    with patch(
        "opencomputer.cli_user_model.MotifImporter",
        side_effect=_importer_factory,
    ):
        result = runner.invoke(user_model_app, ["import-motifs"])
    assert result.exit_code == 0
    # Expected output format: "imported N new node(s), M edge(s)".
    assert "node(s)" in result.stdout
    assert "edge(s)" in result.stdout


def test_context_command_prints_selection(tmp_path: Path) -> None:
    """``context`` command prints the ranked nodes."""
    db = tmp_path / "graph.sqlite"
    store = UserModelStore(db_path=db)
    store.upsert_node(kind="attribute", value="uses Python")
    store.upsert_node(kind="attribute", value="uses Bash")
    store.upsert_node(kind="goal", value="learn Rust")

    from opencomputer.user_model.context import ContextRanker

    with patch(
        "opencomputer.cli_user_model.ContextRanker",
        return_value=ContextRanker(store=UserModelStore(db_path=db)),
    ):
        result = runner.invoke(user_model_app, ["context", "--top-k", "5"])
    assert result.exit_code == 0
    assert "total_score" in result.stdout


def test_edges_list_shows_rows(tmp_path: Path) -> None:
    """``edges list`` prints a row per edge."""
    db = tmp_path / "graph.sqlite"
    from plugin_sdk.user_model import Edge

    store = UserModelStore(db_path=db)
    a = store.upsert_node(kind="attribute", value="a")
    b = store.upsert_node(kind="attribute", value="b")
    store.insert_edge(
        Edge(kind="asserts", from_node=a.node_id, to_node=b.node_id)
    )

    with patch(
        "opencomputer.cli_user_model.UserModelStore",
        return_value=UserModelStore(db_path=db),
    ):
        result = runner.invoke(user_model_app, ["edges", "list"])
    assert result.exit_code == 0
    assert "asserts" in result.stdout


def test_nodes_list_rejects_invalid_kind(tmp_path: Path) -> None:
    """Unknown ``--kind`` is rejected with non-zero exit."""
    db = tmp_path / "graph.sqlite"
    UserModelStore(db_path=db)
    with patch(
        "opencomputer.cli_user_model.UserModelStore",
        return_value=UserModelStore(db_path=db),
    ):
        result = runner.invoke(
            user_model_app, ["nodes", "list", "--kind", "garbage"]
        )
    assert result.exit_code != 0
