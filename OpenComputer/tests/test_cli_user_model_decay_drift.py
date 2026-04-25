"""CLI tests for ``opencomputer user-model {decay,drift} ...`` (Phase 3.D)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from opencomputer.cli_user_model import user_model_app
from opencomputer.inference.storage import MotifStore
from opencomputer.user_model.drift_store import DriftStore
from opencomputer.user_model.store import UserModelStore
from plugin_sdk.decay import DriftConfig, DriftReport
from plugin_sdk.inference import Motif
from plugin_sdk.user_model import Edge, Node

runner = CliRunner()


def _patched(
    *,
    user_db: Path | None = None,
    motif_db: Path | None = None,
    drift_db: Path | None = None,
):
    """Return a list of context-manager patches that redirect each store
    in ``opencomputer.cli_user_model`` to an isolated temp database.

    Usage::

        with contextlib.ExitStack() as stack:
            for p in _patched(user_db=..., drift_db=...):
                stack.enter_context(p)
            ...
    """
    patches = []
    if user_db is not None:
        patches.append(
            patch(
                "opencomputer.cli_user_model.UserModelStore",
                return_value=UserModelStore(db_path=user_db),
            )
        )
    if motif_db is not None:
        patches.append(
            patch(
                "opencomputer.cli_user_model.MotifImporter",
                return_value=None,  # not used in decay/drift paths
            )
        )
    if drift_db is not None:
        patches.append(
            patch(
                "opencomputer.cli_user_model.DriftStore",
                return_value=DriftStore(db_path=drift_db),
            )
        )
    return patches


def _seed_edge(store: UserModelStore, age_days: float = 30.0) -> Edge:
    """Create a simple (A → B) edge aged ``age_days`` in the past."""
    a = store.upsert_node(kind="attribute", value="A")
    b = store.upsert_node(kind="attribute", value="B")
    now = time.time()
    edge = Edge(
        kind="asserts",
        from_node=a.node_id,
        to_node=b.node_id,
        created_at=now - age_days * 86400.0,
    )
    store.insert_edge(edge)
    return edge


# ─── decay ────────────────────────────────────────────────────────────


def test_decay_run_dry_run(tmp_path: Path) -> None:
    """``decay run`` without ``--apply`` prints a preview and touches nothing."""
    user_db = tmp_path / "graph.sqlite"
    store = UserModelStore(db_path=user_db)
    edge = _seed_edge(store, age_days=30.0)
    before = store.get_edge(edge.edge_id)
    assert before is not None

    with patch(
        "opencomputer.cli_user_model.UserModelStore",
        return_value=UserModelStore(db_path=user_db),
    ):
        result = runner.invoke(user_model_app, ["decay", "run"])

    assert result.exit_code == 0
    after = store.get_edge(edge.edge_id)
    assert after is not None
    # Nothing was persisted in dry-run mode.
    assert after.recency_weight == before.recency_weight


def test_decay_run_apply(tmp_path: Path) -> None:
    """``decay run --apply`` persists a fresh recency weight."""
    user_db = tmp_path / "graph.sqlite"
    store = UserModelStore(db_path=user_db)
    # 60 days old — well past one half-life (30d default), so weight drops.
    edge = _seed_edge(store, age_days=60.0)

    with patch(
        "opencomputer.cli_user_model.UserModelStore",
        return_value=UserModelStore(db_path=user_db),
    ):
        result = runner.invoke(user_model_app, ["decay", "run", "--apply"])

    assert result.exit_code == 0
    assert "updated" in result.stdout
    after = store.get_edge(edge.edge_id)
    assert after is not None
    # The default asserts half-life is 30d; at 60d (two half-lives), weight ≈ 0.25.
    assert after.recency_weight < 0.5


# ─── drift ────────────────────────────────────────────────────────────


def _seed_motifs(motif_db: Path, count: int = 10) -> None:
    """Seed ``count`` temporal motifs so a drift report has something to chew."""
    store = MotifStore(db_path=motif_db)
    now = time.time()
    for i in range(count):
        store.insert(
            Motif(
                kind="temporal",
                summary=f"Read high{'_suffix' if i >= count // 2 else ''}",
                created_at=now - i * 3600.0,
            )
        )


def test_drift_detect_dry_run(tmp_path: Path) -> None:
    """``drift detect`` without ``--apply`` prints report but persists nothing."""
    motif_db = tmp_path / "motifs.sqlite"
    drift_db = tmp_path / "drift.sqlite"
    _seed_motifs(motif_db, count=12)
    # Ensure the drift store DB exists but stays empty after the command.
    store = DriftStore(db_path=drift_db)
    assert store.list() == []

    with (
        patch(
            "opencomputer.cli_user_model.DriftDetector",
            side_effect=lambda drift_store=None: _build_detector(
                motif_db, drift_store, drift_db
            ),
        ),
    ):
        result = runner.invoke(user_model_app, ["drift", "detect"])

    assert result.exit_code == 0
    assert "dry-run" in result.stdout
    # Still empty — nothing persisted.
    assert DriftStore(db_path=drift_db).list() == []


def _build_detector(motif_db: Path, drift_store_arg, drift_db: Path):
    """Return a DriftDetector wired to the isolated tmp DBs.

    When the CLI passed ``drift_store=None`` (dry-run), we leave it
    ``None`` so nothing is persisted. When the CLI passed a real store
    (apply), we replace it with one pointing at the tmp ``drift_db``
    so the persisted row lands in the isolated test DB.
    """
    from opencomputer.user_model.drift import DriftDetector

    effective_store = (
        DriftStore(db_path=drift_db) if drift_store_arg is not None else None
    )
    return DriftDetector(
        motif_store=MotifStore(db_path=motif_db),
        config=DriftConfig(min_lifetime_count=1),
        drift_store=effective_store,
    )


def test_drift_detect_apply(tmp_path: Path) -> None:
    """``drift detect --apply`` persists the resulting report."""
    motif_db = tmp_path / "motifs.sqlite"
    drift_db = tmp_path / "drift.sqlite"
    _seed_motifs(motif_db, count=12)

    with (
        patch(
            "opencomputer.cli_user_model.DriftDetector",
            side_effect=lambda drift_store=None: _build_detector(
                motif_db, drift_store, drift_db
            ),
        ),
        patch(
            "opencomputer.cli_user_model.DriftStore",
            return_value=DriftStore(db_path=drift_db),
        ),
    ):
        result = runner.invoke(user_model_app, ["drift", "detect", "--apply"])

    assert result.exit_code == 0
    persisted = DriftStore(db_path=drift_db).list(limit=5)
    assert len(persisted) == 1


def test_drift_list_empty(tmp_path: Path) -> None:
    """Empty drift store prints a "no reports" message."""
    drift_db = tmp_path / "drift.sqlite"
    # Init the schema so list() is clean.
    DriftStore(db_path=drift_db)
    with patch(
        "opencomputer.cli_user_model.DriftStore",
        return_value=DriftStore(db_path=drift_db),
    ):
        result = runner.invoke(user_model_app, ["drift", "list"])
    assert result.exit_code == 0
    assert "no drift reports" in result.stdout


def test_drift_list_with_data(tmp_path: Path) -> None:
    """``drift list`` shows the stashed reports."""
    drift_db = tmp_path / "drift.sqlite"
    store = DriftStore(db_path=drift_db)
    store.insert(DriftReport(total_kl_divergence=0.3, significant=False))
    store.insert(DriftReport(total_kl_divergence=0.9, significant=True))
    with patch(
        "opencomputer.cli_user_model.DriftStore",
        return_value=DriftStore(db_path=drift_db),
    ):
        result = runner.invoke(user_model_app, ["drift", "list"])
    assert result.exit_code == 0
    # Both report ids should appear (first 8 hex chars of each).
    assert "yes" in result.stdout  # significant flag
    assert "no" in result.stdout  # and the non-significant one
