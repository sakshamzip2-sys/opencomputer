"""Tests for Round 2A P-18 — episodic-memory dreaming (EXPERIMENTAL).

Coverage matrix:

* Schema v4 migration adds ``dreamed_into`` column without dropping
  pre-existing rows (covered indirectly by ``test_dream_runner_*``,
  which spin up fresh DBs that hit the same migration path).
* ``cluster_entries`` groups by date bucket + topic-keyword overlap
  deterministically.
* ``DreamRunner.run_once`` is a no-op on empty stores.
* ``DreamRunner.run_once`` consolidates a synthetic 50-entry corpus,
  marks originals as ``dreamed_into``, and is idempotent on re-run.
* When the provider fails, the runner retries once and skips the
  cluster — originals stay un-dreamed.
* ``dream-on`` / ``dream-off`` flip the config flag round-trip.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from opencomputer.agent.config import (
    Config,
    MemoryConfig,
    ModelConfig,
    SessionConfig,
)
from opencomputer.agent.dreaming import (
    DEFAULT_FETCH_LIMIT,
    DreamRunner,
    build_cluster_prompt,
    cluster_entries,
)
from opencomputer.agent.state import SessionDB
from plugin_sdk.core import Message
from plugin_sdk.provider_contract import ProviderResponse, Usage

# ─── helpers ────────────────────────────────────────────────────────


def _make_config(db_path: Path, *, cheap: str | None = None) -> Config:
    """Build a Config pointing at the given DB path. Cheap-route optional."""
    return Config(
        model=ModelConfig(
            provider="anthropic", model="main-model", cheap_model=cheap
        ),
        session=SessionConfig(db_path=db_path),
        memory=MemoryConfig(declarative_path=db_path.parent / "MEMORY.md"),
    )


def _make_provider(text: str = "- consolidated bullet one\n- consolidated bullet two") -> AsyncMock:
    """Build a mock provider whose .complete() returns one happy ProviderResponse."""
    provider = AsyncMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content=text),
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=20),
        )
    )
    return provider


def _seed_episodic(
    db: SessionDB,
    session_id: str,
    rows: Iterable[tuple[str, list[str], list[str]]],
    *,
    base_ts: float | None = None,
) -> None:
    """Insert episodic rows. Each tuple: (summary, tools_used, file_paths).

    Times are spread across one second per row, ascending. Caller may
    pass ``base_ts`` to control the starting timestamp (used to force
    rows into the same / different ISO week buckets).
    """
    base = base_ts if base_ts is not None else time.time()
    for i, (summary, tools, files) in enumerate(rows):
        rid = db.record_episodic(
            session_id=session_id,
            turn_index=i,
            summary=summary,
            tools_used=tools or None,
            file_paths=files or None,
        )
        # Override timestamp to deterministic value for clustering tests.
        with db._txn() as conn:  # noqa: SLF001 — test-only override
            conn.execute(
                "UPDATE episodic_events SET timestamp = ? WHERE id = ?",
                (base + i, rid),
            )


# ─── pure-helper tests ─────────────────────────────────────────────


def test_cluster_entries_empty_returns_empty_list() -> None:
    assert cluster_entries([]) == []


def test_cluster_entries_groups_by_overlap_within_bucket(tmp_path: Path) -> None:
    """Same week + shared keyword → same cluster; otherwise → new cluster."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1", platform="cli", model="m")
    base = 1714003200.0  # Wed 2024-04-25 UTC, well-defined ISO week.
    _seed_episodic(
        db,
        "s-1",
        [
            ("refactor auth helper", ["Edit"], ["src/auth.py"]),
            ("write auth tests", ["Edit"], ["tests/test_auth.py"]),
            ("update README copy", ["Edit"], ["README.md"]),
        ],
        base_ts=base,
    )
    rows = db.list_undreamed_episodic(limit=10)
    clusters = cluster_entries(rows)
    # Two clusters: {auth, auth tests} (share "auth"), {readme}
    assert len(clusters) == 2
    big = max(clusters, key=len)
    small = min(clusters, key=len)
    assert len(big) == 2
    assert len(small) == 1
    assert any("auth" in (e["summary"] or "").lower() for e in big)


def test_cluster_entries_splits_across_iso_weeks(tmp_path: Path) -> None:
    """Even when topics overlap, different ISO weeks → different clusters."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1", platform="cli", model="m")
    week1 = 1714003200.0  # 2024-04-25
    week2 = week1 + 86400 * 14  # +14 days → guaranteed different ISO week
    _seed_episodic(
        db,
        "s-1",
        [("refactor auth helper", ["Edit"], ["src/auth.py"])],
        base_ts=week1,
    )
    _seed_episodic(
        db,
        "s-1",
        [("refactor auth helper round 2", ["Edit"], ["src/auth.py"])],
        base_ts=week2,
    )
    rows = db.list_undreamed_episodic(limit=10)
    clusters = cluster_entries(rows)
    assert len(clusters) == 2
    assert all(len(c) == 1 for c in clusters)


def test_build_cluster_prompt_includes_all_summaries() -> None:
    cluster = [
        {"summary": "alpha", "tools_used": "Edit", "file_paths": "x.py"},
        {"summary": "beta", "tools_used": "Bash", "file_paths": "y.py"},
        {"summary": "gamma", "tools_used": "", "file_paths": ""},
    ]
    text = build_cluster_prompt(cluster)
    assert "Here are 3 related episodic memories" in text
    assert "1. alpha" in text
    assert "2. beta" in text
    assert "3. gamma" in text
    assert "<= 5" in text or "5 short bullets" in text


# ─── DreamRunner integration tests ─────────────────────────────────


def test_dream_runner_noop_on_empty_store(tmp_path: Path) -> None:
    """An empty episodic store → zero-counted report; no provider calls."""
    cfg = _make_config(tmp_path / "s.db")
    db = SessionDB(cfg.session.db_path)
    provider = _make_provider()

    runner = DreamRunner(config=cfg, db=db, provider=provider)
    report = runner.run_once()

    assert report.fetched == 0
    assert report.clusters_total == 0
    assert report.consolidations_written == 0
    provider.complete.assert_not_called()


def test_dream_runner_consolidates_synthetic_corpus(tmp_path: Path) -> None:
    """Seed many entries → multiple consolidations; originals marked dreamed_into."""
    cfg = _make_config(tmp_path / "s.db")
    db = SessionDB(cfg.session.db_path)
    db.create_session("s-1", platform="cli", model="m")

    # 50-entry corpus split into 5 themes × 10 turns each. All in the
    # same ISO week so date bucketing groups them together; topic
    # keywords drive the actual clustering.
    base = 1714003200.0
    themes = [
        ("auth refactor", ["Edit"], ["src/auth.py"]),
        ("router fixes", ["Edit"], ["src/router.py"]),
        ("docs cleanup", ["Edit"], ["README.md"]),
        ("storage migration", ["Bash"], ["scripts/migrate.sh"]),
        ("login flow polish", ["Edit"], ["src/login.py"]),
    ]
    rows: list[tuple[str, list[str], list[str]]] = []
    for theme_name, tools, files in themes:
        for i in range(10):
            rows.append((f"{theme_name} step {i}", tools, files))
    _seed_episodic(db, "s-1", rows, base_ts=base)

    provider = _make_provider("- consolidated theme bullet")
    runner = DreamRunner(config=cfg, db=db, provider=provider, fetch_limit=DEFAULT_FETCH_LIMIT)

    report = runner.run_once()

    # Five clusters expected (one per theme); all should be written.
    assert report.fetched == 50
    assert report.clusters_total == 5
    assert report.consolidations_written == 5
    assert report.clusters_skipped_small == 0
    assert report.clusters_failed == 0
    assert provider.complete.call_count == 5

    # Every original row should now have a non-NULL dreamed_into.
    with db._connect() as conn:  # noqa: SLF001 — test-only inspection
        rows = conn.execute(
            "SELECT id, summary, dreamed_into, turn_index "
            "FROM episodic_events ORDER BY id"
        ).fetchall()
    originals = [r for r in rows if r["turn_index"] >= 0]
    consolidations = [r for r in rows if r["turn_index"] == -1]
    assert len(originals) == 50
    assert len(consolidations) == 5
    assert all(r["dreamed_into"] is not None for r in originals)
    assert all(r["dreamed_into"] is None for r in consolidations)
    assert all(r["summary"] == "- consolidated theme bullet" for r in consolidations)


def test_dream_runner_idempotent_on_rerun(tmp_path: Path) -> None:
    """Second run after a first complete pass → no new consolidations."""
    cfg = _make_config(tmp_path / "s.db")
    db = SessionDB(cfg.session.db_path)
    db.create_session("s-1", platform="cli", model="m")
    base = 1714003200.0
    _seed_episodic(
        db,
        "s-1",
        [
            ("auth refactor a", ["Edit"], ["src/auth.py"]),
            ("auth refactor b", ["Edit"], ["src/auth.py"]),
        ],
        base_ts=base,
    )

    provider = _make_provider()
    runner = DreamRunner(config=cfg, db=db, provider=provider)

    first = runner.run_once()
    assert first.consolidations_written == 1

    # Re-run with no new entries — should be a no-op.
    second = runner.run_once()
    assert second.fetched == 0
    assert second.consolidations_written == 0
    # Provider only called once (during the first run).
    assert provider.complete.call_count == 1

    # Add ONE new undreamed entry — it's a singleton so it should be
    # SKIPPED (under MIN_CLUSTER_SIZE), not consolidated.
    _seed_episodic(
        db,
        "s-1",
        [("auth refactor c", ["Edit"], ["src/auth.py"])],
        base_ts=base + 1000,  # same ISO week
    )
    third = runner.run_once()
    assert third.fetched == 1
    assert third.clusters_total == 1
    assert third.consolidations_written == 0
    assert third.clusters_skipped_small == 1


def test_dream_runner_provider_failure_leaves_originals_intact(tmp_path: Path) -> None:
    """Provider raising on every call → retry once, then skip cluster."""
    cfg = _make_config(tmp_path / "s.db")
    db = SessionDB(cfg.session.db_path)
    db.create_session("s-1", platform="cli", model="m")
    base = 1714003200.0
    _seed_episodic(
        db,
        "s-1",
        [
            ("auth refactor a", ["Edit"], ["src/auth.py"]),
            ("auth refactor b", ["Edit"], ["src/auth.py"]),
        ],
        base_ts=base,
    )

    provider = AsyncMock()
    provider.complete = AsyncMock(side_effect=RuntimeError("provider down"))

    runner = DreamRunner(config=cfg, db=db, provider=provider)
    report = runner.run_once()

    # Cluster failed; report reflects it; provider was retried.
    assert report.clusters_total == 1
    assert report.consolidations_written == 0
    assert report.clusters_failed == 1
    assert provider.complete.call_count == 2  # initial attempt + retry

    # Originals still un-dreamed (so a future pass can retry).
    rows = db.list_undreamed_episodic(limit=10)
    assert len(rows) == 2
    assert all(r["dreamed_into"] is None for r in rows)


def test_dream_runner_session_id_filter_scopes_correctly(tmp_path: Path) -> None:
    """``--session-id`` flag restricts fetch to one session's rows."""
    cfg = _make_config(tmp_path / "s.db")
    db = SessionDB(cfg.session.db_path)
    db.create_session("s-a", platform="cli", model="m")
    db.create_session("s-b", platform="cli", model="m")
    base = 1714003200.0
    _seed_episodic(
        db,
        "s-a",
        [
            ("auth refactor", ["Edit"], ["src/auth.py"]),
            ("auth tests", ["Edit"], ["tests/test_auth.py"]),
        ],
        base_ts=base,
    )
    _seed_episodic(
        db,
        "s-b",
        [
            ("router work", ["Edit"], ["src/router.py"]),
            ("router tests", ["Edit"], ["tests/test_router.py"]),
        ],
        base_ts=base,
    )

    provider = _make_provider()
    runner = DreamRunner(config=cfg, db=db, provider=provider)
    report = runner.run_once(session_id="s-a")

    # Only s-a's two entries got fetched / consolidated.
    assert report.fetched == 2
    assert report.consolidations_written == 1

    # s-b entries remain un-dreamed.
    sb_rows = db.list_undreamed_episodic(session_id="s-b")
    assert len(sb_rows) == 2


def test_dream_runner_uses_cheap_model_when_configured(tmp_path: Path) -> None:
    """When ``cheap_model`` is set, the consolidation call uses it."""
    cfg = _make_config(tmp_path / "s.db", cheap="haiku-mini")
    db = SessionDB(cfg.session.db_path)
    db.create_session("s-1", platform="cli", model="m")
    base = 1714003200.0
    _seed_episodic(
        db,
        "s-1",
        [
            ("auth refactor a", ["Edit"], ["src/auth.py"]),
            ("auth refactor b", ["Edit"], ["src/auth.py"]),
        ],
        base_ts=base,
    )

    provider = _make_provider()
    runner = DreamRunner(config=cfg, db=db, provider=provider)
    runner.run_once()

    assert provider.complete.call_count == 1
    kwargs = provider.complete.await_args.kwargs
    assert kwargs["model"] == "haiku-mini"
    # Sanity: dreaming should pass no tools.
    assert kwargs["tools"] is None


# ─── CLI toggle tests (dream-on / dream-off) ───────────────────────


def test_dream_on_off_round_trip_via_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``memory dream-on --interval hourly`` → load reads enabled=True; off resets."""
    # Point ``$OPENCOMPUTER_HOME`` at an isolated dir so config writes
    # land in tmp_path and don't pollute the dev profile.
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from typer.testing import CliRunner

    from opencomputer.agent.config_store import config_file_path, load_config
    from opencomputer.cli_memory import memory_app

    runner = CliRunner()

    result = runner.invoke(memory_app, ["dream-on", "--interval", "hourly"])
    assert result.exit_code == 0, result.output
    assert "dreaming enabled" in result.output

    cfg = load_config(config_file_path())
    assert cfg.memory.dreaming_enabled is True
    assert cfg.memory.dreaming_interval == "hourly"

    result_off = runner.invoke(memory_app, ["dream-off"])
    assert result_off.exit_code == 0, result_off.output
    cfg2 = load_config(config_file_path())
    assert cfg2.memory.dreaming_enabled is False
    # Interval setting is preserved on dream-off so re-enabling restores it.
    assert cfg2.memory.dreaming_interval == "hourly"


def test_dream_on_rejects_invalid_interval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from typer.testing import CliRunner

    from opencomputer.cli_memory import memory_app

    runner = CliRunner()
    result = runner.invoke(memory_app, ["dream-on", "--interval", "monthly"])
    assert result.exit_code == 1
    assert "invalid interval" in result.output


def test_doctor_reports_dreaming_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``memory doctor`` includes a dreaming row that says 'disabled' by default."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from typer.testing import CliRunner

    from opencomputer.cli_memory import memory_app

    runner = CliRunner()
    result = runner.invoke(memory_app, ["doctor"])
    assert result.exit_code == 0
    assert "dreaming" in result.output
    assert "disabled" in result.output


# ─── schema migration test ─────────────────────────────────────────


def test_schema_v4_adds_dreamed_into_column_and_is_nullable(tmp_path: Path) -> None:
    """Fresh DB built via the v1→v4 migration chain has the column NULLable."""
    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1", platform="cli", model="m")
    rid = db.record_episodic(session_id="s-1", turn_index=0, summary="hello")
    with db._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT id, dreamed_into FROM episodic_events WHERE id = ?", (rid,)
        ).fetchone()
        version = conn.execute("SELECT version FROM schema_version").fetchone()
    assert row["dreamed_into"] is None
    assert int(version[0]) == 8
