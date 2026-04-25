"""CLI tests for ``opencomputer inference motifs ...`` (Phase 3.B)."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from opencomputer.cli_inference import inference_app
from opencomputer.inference.storage import MotifStore
from plugin_sdk.inference import Motif

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_bus():
    """Bus swap+restore — preserves cross-file singleton invariant."""
    from opencomputer.ingestion import bus as bus_module
    from opencomputer.ingestion.bus import reset_default_bus

    saved = bus_module.default_bus
    reset_default_bus()
    yield
    bus_module.default_bus = saved


def _make_motif(
    *,
    kind: str = "temporal",
    summary: str = "test motif",
    created_at: float | None = None,
) -> Motif:
    return Motif(
        kind=kind,  # type: ignore[arg-type]
        confidence=0.7,
        support=4,
        summary=summary,
        payload={"label": "Read", "count": 4},
        evidence_event_ids=("e1", "e2"),
        created_at=created_at if created_at is not None else time.time(),
    )


def test_motifs_list_empty(tmp_path: Path) -> None:
    """Empty store prints the dim 'no motifs found' message."""
    db = tmp_path / "m.sqlite"
    MotifStore(db_path=db)  # create empty schema
    with patch(
        "opencomputer.cli_inference.MotifStore",
        return_value=MotifStore(db_path=db),
    ):
        result = runner.invoke(inference_app, ["motifs", "list"])
    assert result.exit_code == 0
    assert "no motifs found" in result.stdout


def test_motifs_list_with_data(tmp_path: Path) -> None:
    """Populated store prints a Rich table including the summary text."""
    db = tmp_path / "m.sqlite"
    store = MotifStore(db_path=db)
    store.insert(_make_motif(summary="Read on Monday morning"))
    store.insert(_make_motif(summary="Bash after Read", kind="transition"))

    with patch(
        "opencomputer.cli_inference.MotifStore",
        return_value=MotifStore(db_path=db),
    ):
        result = runner.invoke(inference_app, ["motifs", "list"])
    assert result.exit_code == 0
    # Rich's table truncates long lines but the summaries should be
    # present (possibly word-wrapped) — match on a unique substring.
    assert "Read on Monday" in result.stdout
    assert "Bash after Read" in result.stdout


def test_motifs_list_filters_by_kind(tmp_path: Path) -> None:
    """``--kind transition`` excludes temporal motifs from the table."""
    db = tmp_path / "m.sqlite"
    store = MotifStore(db_path=db)
    store.insert(_make_motif(summary="temporal-only"))
    store.insert(_make_motif(summary="transition-only", kind="transition"))

    with patch(
        "opencomputer.cli_inference.MotifStore",
        return_value=MotifStore(db_path=db),
    ):
        result = runner.invoke(
            inference_app, ["motifs", "list", "--kind", "transition"]
        )
    assert result.exit_code == 0
    assert "transition-only" in result.stdout
    assert "temporal-only" not in result.stdout


def test_motifs_list_rejects_invalid_kind(tmp_path: Path) -> None:
    """Unknown ``--kind`` value exits non-zero with a usage hint."""
    db = tmp_path / "m.sqlite"
    MotifStore(db_path=db)
    with patch(
        "opencomputer.cli_inference.MotifStore",
        return_value=MotifStore(db_path=db),
    ):
        result = runner.invoke(
            inference_app, ["motifs", "list", "--kind", "garbage"]
        )
    assert result.exit_code != 0


def test_motifs_stats_prints_counts(tmp_path: Path) -> None:
    """``stats`` prints one row per kind plus a total."""
    db = tmp_path / "m.sqlite"
    store = MotifStore(db_path=db)
    store.insert(_make_motif(kind="temporal"))
    store.insert(_make_motif(kind="temporal"))
    store.insert(_make_motif(kind="transition"))

    with patch(
        "opencomputer.cli_inference.MotifStore",
        return_value=MotifStore(db_path=db),
    ):
        result = runner.invoke(inference_app, ["motifs", "stats"])
    assert result.exit_code == 0
    assert "temporal" in result.stdout
    assert "transition" in result.stdout
    assert "implicit_goal" in result.stdout
    assert "total" in result.stdout


def test_motifs_prune_removes_old(tmp_path: Path) -> None:
    """``prune --older-than 7d`` deletes motifs older than the cutoff."""
    db = tmp_path / "m.sqlite"
    store = MotifStore(db_path=db)
    now = time.time()
    store.insert(_make_motif(summary="ancient", created_at=now - 30 * 86400))
    store.insert(_make_motif(summary="recent", created_at=now - 60))
    assert store.count() == 2

    with patch(
        "opencomputer.cli_inference.MotifStore",
        return_value=MotifStore(db_path=db),
    ):
        result = runner.invoke(
            inference_app, ["motifs", "prune", "--older-than", "7d"]
        )
    assert result.exit_code == 0
    assert "deleted 1 motif" in result.stdout
    # And the remaining motif is the recent one.
    fresh_store = MotifStore(db_path=db)
    remaining = fresh_store.list()
    assert len(remaining) == 1
    assert remaining[0].summary == "recent"
