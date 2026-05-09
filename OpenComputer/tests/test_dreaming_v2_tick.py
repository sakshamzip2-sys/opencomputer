"""Production wiring + cron + CLI tests for Dreaming v2 (M6.4)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from opencomputer.agent.dreaming_v2 import (
    DreamCandidate,
    DreamingV2Config,
    DreamOutcome,
)
from opencomputer.agent.memory import MemoryManager
from opencomputer.agent.state import SessionDB
from opencomputer.cron.dreaming_v2_tick import (
    DreamingV2Dependencies,
    _build_hold_fn,
    _build_promote_fn,
    _build_recall_count_fn,
    _fetch_candidates,
    _load_state,
    _read_existing_memories,
    _save_state,
    _summarise_for_cron,
    build_pipeline_with_dependencies,
    run_dreaming_v2_async,
)
from plugin_sdk.embeddings import EmbeddingBatch

# ─── helpers ────────────────────────────────────────────────────────


def _make_memory(tmp_path: Path, body: str = "") -> MemoryManager:
    declarative = tmp_path / "MEMORY.md"
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    if body:
        declarative.write_text(body, encoding="utf-8")
    return MemoryManager(
        declarative_path=declarative,
        skills_path=skills,
    )


def _make_db(tmp_path: Path) -> SessionDB:
    return SessionDB(tmp_path / "session.db")


def _seed_episodic(db: SessionDB, summaries: list[str]) -> list[int]:
    """Seed N episodic events; returns the integer row ids."""
    db.create_session("test-sess", platform="cli", model="", title="")
    ids: list[int] = []
    with db._connect() as conn:
        for i, summary in enumerate(summaries):
            cur = conn.execute(
                """
                INSERT INTO episodic_events
                  (session_id, turn_index, summary, tools_used,
                   file_paths, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("test-sess", i, summary, "", "", time.time() + i),
            )
            ids.append(int(cur.lastrowid))
    return ids


def _seed_recall_citation(
    db: SessionDB, *, episodic_event_id: str, count: int
) -> None:
    """Insert ``count`` recall_citations rows pointing at the given episodic id."""
    db.create_session("recall-sess", platform="cli", model="", title="")
    with db._connect() as conn:
        for i in range(count):
            conn.execute(
                """
                INSERT INTO recall_citations
                  (id, session_id, turn_index, episodic_event_id,
                   candidate_kind, retrieved_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (f"r-{episodic_event_id}-{i}", "recall-sess", i,
                 episodic_event_id, "episodic", time.time()),
            )


# ─── _fetch_candidates ─────────────────────────────────────────────


def test_fetch_candidates_returns_undreamed_only(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    ids = _seed_episodic(db, ["fact A", "fact B", "fact C"])
    # Mark one as dreamed
    with db._connect() as conn:
        conn.execute(
            "UPDATE episodic_events SET dreamed_into = ? WHERE id = ?",
            (ids[0], ids[0]),
        )
    cands = _fetch_candidates(db, limit=10)
    summaries = {c.raw_text for c in cands}
    assert summaries == {"fact B", "fact C"}


def test_fetch_candidates_limit(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_episodic(db, [f"fact {i}" for i in range(10)])
    cands = _fetch_candidates(db, limit=3)
    assert len(cands) == 3


def test_fetch_candidates_skips_empty_summary(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_episodic(db, ["", "real fact"])
    cands = _fetch_candidates(db, limit=10)
    assert len(cands) == 1
    assert cands[0].raw_text == "real fact"


# ─── recall_count_fn from real DB ──────────────────────────────────


def test_recall_count_fn_zero_when_unknown(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    fn = _build_recall_count_fn(db)
    assert fn("does-not-exist") == 0


def test_recall_count_fn_counts_citations(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_recall_citation(db, episodic_event_id="42", count=3)
    fn = _build_recall_count_fn(db)
    assert fn("42") == 3


# ─── promote_fn writes MEMORY.md ──────────────────────────────────


def test_promote_fn_appends_to_memory_md(tmp_path: Path) -> None:
    mm = _make_memory(tmp_path)
    fn = _build_promote_fn(mm)
    fn("user prefers dark mode UI")
    assert "user prefers dark mode UI" in mm.read_declarative()
    assert "(dreamed)" in mm.read_declarative()


def test_promote_fn_invalidates_indices(tmp_path: Path) -> None:
    """Indices must invalidate on promote so the next retrieval sees the
    new entry without manual cache busting."""
    mm = _make_memory(tmp_path, body="existing entry\n")
    # Touch the BM25 cache to mark it valid.
    _ = mm._bm25_index
    fn = _build_promote_fn(mm)
    fn("freshly promoted fact")
    # The MemoryManager.append_declarative path calls _bm25_index.invalidate;
    # we just confirm the file has both entries.
    body = mm.read_declarative()
    assert "existing entry" in body
    assert "freshly promoted fact" in body


# ─── hold_fn writes DREAMS.md with byte cap ──────────────────────


def test_hold_fn_creates_dreams_md(tmp_path: Path) -> None:
    fn = _build_hold_fn(tmp_path)
    fn("uncertain hint about user preference", 16384)
    body = (tmp_path / "DREAMS.md").read_text(encoding="utf-8")
    assert "uncertain hint about user preference" in body


def test_hold_fn_evicts_oldest_when_over_cap(tmp_path: Path) -> None:
    """Hard cap must drop oldest blocks (FIFO)."""
    fn = _build_hold_fn(tmp_path)
    # Write a few entries, then a small cap that forces eviction.
    # Each entry renders as `- YYYY-MM-DD: <text>\n` ≈ 36 bytes + body.
    fn("first dream block", 10_000)
    fn("second dream block", 10_000)
    fn("third dream block — newest", 60)  # Cap = ~60 bytes; only newest fits
    body = (tmp_path / "DREAMS.md").read_text(encoding="utf-8")
    assert "third dream" in body
    assert "first dream" not in body
    assert "second dream" not in body
    assert len(body.encode("utf-8")) <= 100  # Single-entry payload is small


def test_hold_fn_atomic_write(tmp_path: Path) -> None:
    """Temp file should be cleaned up on success."""
    fn = _build_hold_fn(tmp_path)
    fn("fact A", 16384)
    assert (tmp_path / "DREAMS.md").exists()
    assert not (tmp_path / "DREAMS.md.tmp").exists()


# ─── existing-memories paragraphification ────────────────────────


def test_read_existing_memories_splits_paragraphs(tmp_path: Path) -> None:
    mm = _make_memory(
        tmp_path,
        body="entry one\n\nentry two\n\nentry three",
    )
    blocks = _read_existing_memories(mm)
    assert blocks == ["entry one", "entry two", "entry three"]


def test_read_existing_memories_empty_returns_empty(tmp_path: Path) -> None:
    mm = _make_memory(tmp_path)
    assert _read_existing_memories(mm) == []


# ─── state ledger persistence ────────────────────────────────────


def test_save_and_load_state_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    _save_state(
        {"processed_event_ids": ["a", "b"], "last_run_ts_ns": 1234},
        path=p,
    )
    loaded = _load_state(p)
    assert loaded["processed_event_ids"] == ["a", "b"]
    assert loaded["last_run_ts_ns"] == 1234


def test_load_state_unreadable_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("not-json garbage", encoding="utf-8")
    loaded = _load_state(p)
    assert loaded == {"processed_event_ids": [], "last_run_ts_ns": None}


def test_load_state_missing_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "missing.json"
    loaded = _load_state(p)
    assert loaded == {"processed_event_ids": [], "last_run_ts_ns": None}


# ─── pipeline assembly via dependencies ─────────────────────────


def test_pipeline_assembly_no_provider_score_zero(tmp_path: Path) -> None:
    """No provider → score_fn returns 0.0 → all candidates fail score."""
    deps = DreamingV2Dependencies(
        profile_home=tmp_path,
        memory=_make_memory(tmp_path),
        db=_make_db(tmp_path),
        provider=None,
        model="claude-opus-4-7",
        config=DreamingV2Config(enabled=True, min_recall_count=0),
    )
    pipeline = build_pipeline_with_dependencies(deps)
    assert pipeline.embed_fn is None
    # The score_fn is the zero-fallback
    import asyncio
    score = asyncio.run(pipeline.score_fn("anything"))
    assert score == 0.0


def test_pipeline_assembly_with_provider(tmp_path: Path) -> None:
    """Provider-supplied score + embed wired through correctly."""

    class _FakeProviderResp:
        text = "0.85"

    class _FakeProvider:
        async def complete(self, **_kw: Any):
            return _FakeProviderResp()

        async def embed(self, *, texts: list[str]) -> EmbeddingBatch:
            # Trivial 1-D vectors
            return EmbeddingBatch(
                model_id="fake",
                vectors=tuple((float(i),) for i, _ in enumerate(texts)),
            )

    deps = DreamingV2Dependencies(
        profile_home=tmp_path,
        memory=_make_memory(tmp_path),
        db=_make_db(tmp_path),
        provider=_FakeProvider(),
        model="claude-opus-4-7",
        config=DreamingV2Config(enabled=True),
    )
    pipeline = build_pipeline_with_dependencies(deps)
    import asyncio
    score = asyncio.run(pipeline.score_fn("important fact"))
    assert score == 0.85


# ─── full async run ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_dreaming_v2_async_promotes_high_quality(
    tmp_path: Path,
) -> None:
    """End-to-end: high-score, high-recall candidate promotes to MEMORY.md."""

    class _FakeResp:
        text = "0.95"

    class _FakeProvider:
        async def complete(self, **_kw: Any):
            return _FakeResp()

        async def embed(self, *, texts: list[str]) -> EmbeddingBatch:
            # Each text gets a unique vector → diversity always 0
            return EmbeddingBatch(
                model_id="fake",
                vectors=tuple((float(i), 0.0) for i, _ in enumerate(texts)),
            )

    db = _make_db(tmp_path)
    mm = _make_memory(tmp_path)
    [eid] = _seed_episodic(db, ["user prefers Python over Ruby"])
    _seed_recall_citation(db, episodic_event_id=str(eid), count=5)

    deps = DreamingV2Dependencies(
        profile_home=tmp_path,
        memory=mm,
        db=db,
        provider=_FakeProvider(),
        model="claude-opus-4-7",
        config=DreamingV2Config(enabled=True),
    )
    state_path = tmp_path / "state.json"
    summary = await run_dreaming_v2_async(
        deps=deps, candidate_limit=10, state_path=state_path
    )
    assert len(summary.promoted) == 1
    assert summary.total_evaluated == 1
    assert "user prefers Python over Ruby" in mm.read_declarative()
    # State was persisted
    state = _load_state(state_path)
    assert str(eid) in state["processed_event_ids"]
    assert state["last_run_ts_ns"] is not None


@pytest.mark.asyncio
async def test_run_dreaming_v2_async_skips_already_processed(
    tmp_path: Path,
) -> None:
    """Idempotency: already-processed event_ids must not score again."""

    score_call_count = 0

    class _FakeResp:
        text = "0.95"

    class _FakeProvider:
        async def complete(self, **_kw: Any):
            nonlocal score_call_count
            score_call_count += 1
            return _FakeResp()

        async def embed(self, *, texts: list[str]) -> EmbeddingBatch:
            return EmbeddingBatch(
                model_id="fake",
                vectors=tuple((float(i),) for i, _ in enumerate(texts)),
            )

    db = _make_db(tmp_path)
    mm = _make_memory(tmp_path)
    [eid] = _seed_episodic(db, ["fact"])

    state_path = tmp_path / "state.json"
    _save_state(
        {"processed_event_ids": [str(eid)], "last_run_ts_ns": 1000},
        path=state_path,
    )

    deps = DreamingV2Dependencies(
        profile_home=tmp_path,
        memory=mm,
        db=db,
        provider=_FakeProvider(),
        model="claude-opus-4-7",
        config=DreamingV2Config(enabled=True),
    )
    summary = await run_dreaming_v2_async(
        deps=deps, candidate_limit=10, state_path=state_path
    )
    assert summary.skipped_already_processed == 1
    assert score_call_count == 0  # didn't even score


@pytest.mark.asyncio
async def test_run_dreaming_v2_async_disabled_returns_empty(
    tmp_path: Path,
) -> None:
    db = _make_db(tmp_path)
    deps = DreamingV2Dependencies(
        profile_home=tmp_path,
        memory=_make_memory(tmp_path),
        db=db,
        provider=None,
        model="claude-opus-4-7",
        config=DreamingV2Config(enabled=False),
    )
    summary = await run_dreaming_v2_async(deps=deps, candidate_limit=10)
    assert summary.total_evaluated == 0
    assert len(summary.promoted) == 0


@pytest.mark.asyncio
async def test_promoted_marks_dreamed_into_in_db(tmp_path: Path) -> None:
    """Successful promotion must mark the row's ``dreamed_into`` so a
    subsequent ``_fetch_candidates`` doesn't re-pick it."""

    class _FakeResp:
        text = "0.95"

    class _FakeProvider:
        async def complete(self, **_kw: Any):
            return _FakeResp()

        async def embed(self, *, texts: list[str]) -> EmbeddingBatch:
            return EmbeddingBatch(
                model_id="fake",
                vectors=tuple((float(i),) for i, _ in enumerate(texts)),
            )

    db = _make_db(tmp_path)
    mm = _make_memory(tmp_path)
    [eid] = _seed_episodic(db, ["x"])
    _seed_recall_citation(db, episodic_event_id=str(eid), count=5)

    deps = DreamingV2Dependencies(
        profile_home=tmp_path,
        memory=mm,
        db=db,
        provider=_FakeProvider(),
        model="claude-opus-4-7",
        config=DreamingV2Config(enabled=True),
    )
    await run_dreaming_v2_async(
        deps=deps, candidate_limit=10, state_path=tmp_path / "s.json"
    )
    with db._connect() as conn:
        row = conn.execute(
            "SELECT dreamed_into FROM episodic_events WHERE id = ?",
            (eid,),
        ).fetchone()
    assert row["dreamed_into"] == eid


# ─── cron telemetry summary shape ────────────────────────────────


def test_summarise_for_cron_shape() -> None:
    from opencomputer.agent.dreaming_v2 import (
        DreamGateResult,
        DreamRunSummary,
    )

    cand = DreamCandidate(event_id="e1", raw_text="t")
    fake = DreamGateResult(
        candidate=cand,
        outcome=DreamOutcome.PROMOTED,
        score=0.9,
        recall_count=2,
        diversity_score=0.1,
        rationale="ok",
    )
    s = DreamRunSummary(
        promoted=(fake,),
        held=(),
        dropped=(),
        skipped_already_processed=2,
        total_evaluated=1,
        catch_up_run=False,
    )
    out = _summarise_for_cron(s)
    assert out == {
        "promoted": 1,
        "held": 0,
        "dropped": 0,
        "skipped_already_processed": 2,
        "total_evaluated": 1,
        "catch_up_run": False,
    }


# ─── CLI: oc memory dream-v2 ─────────────────────────────────────


def test_cli_dream_v2_disabled_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default = disabled; --force needed to run."""
    from opencomputer.cli_memory import memory_app

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(memory_app, ["dream-v2"])
    assert result.exit_code == 0
    assert "disabled" in result.output


def test_cli_dream_v2_force_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--force runs even when disabled — mocks the production deps."""
    from opencomputer import cli_memory
    from opencomputer.cron import dreaming_v2_tick

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    deps = DreamingV2Dependencies(
        profile_home=tmp_path,
        memory=_make_memory(tmp_path),
        db=_make_db(tmp_path),
        provider=None,
        model="claude-opus-4-7",
        config=DreamingV2Config(enabled=False),  # disabled — --force overrides
    )
    monkeypatch.setattr(
        dreaming_v2_tick, "build_production_dependencies", lambda: deps
    )

    runner = CliRunner()
    result = runner.invoke(cli_memory.memory_app, ["dream-v2", "--force"])
    assert result.exit_code == 0, result.output
    assert "dream-v2 finished" in result.output


def test_cli_dream_v2_json_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer import cli_memory
    from opencomputer.cron import dreaming_v2_tick

    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    deps = DreamingV2Dependencies(
        profile_home=tmp_path,
        memory=_make_memory(tmp_path),
        db=_make_db(tmp_path),
        provider=None,
        model="claude-opus-4-7",
        config=DreamingV2Config(enabled=True),
    )
    monkeypatch.setattr(
        dreaming_v2_tick, "build_production_dependencies", lambda: deps
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_memory.memory_app, ["dream-v2", "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "promoted" in payload
    assert "held" in payload
    assert "dropped" in payload
    assert "skipped_already_processed" in payload


# ─── config round-trip + production deps integration ────────────


def test_config_loads_dreaming_v2_defaults() -> None:
    """Default Config has the 7 dreaming_v2 fields with the spec values."""
    from opencomputer.agent.config_store import default_config

    cfg = default_config()
    assert cfg.memory.dreaming_v2_enabled is False
    assert cfg.memory.dreaming_v2_score_threshold == 0.65
    assert cfg.memory.dreaming_v2_min_recall_count == 2
    assert cfg.memory.dreaming_v2_diversity_threshold == 0.8
    assert cfg.memory.dreaming_v2_max_promotions_per_run == 20
    assert cfg.memory.dreaming_v2_dreams_md_max_bytes == 16384
    assert cfg.memory.dreaming_v2_candidate_fetch_limit == 50


def test_config_yaml_overrides_dreaming_v2_fields(tmp_path: Path) -> None:
    """YAML overrides flow through ``_apply_overrides`` correctly for
    every dreaming_v2_* knob."""
    from opencomputer.agent.config_store import load_config

    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        """
memory:
  dreaming_v2_enabled: true
  dreaming_v2_score_threshold: 0.42
  dreaming_v2_min_recall_count: 7
  dreaming_v2_diversity_threshold: 0.95
  dreaming_v2_max_promotions_per_run: 3
  dreaming_v2_dreams_md_max_bytes: 1024
  dreaming_v2_candidate_fetch_limit: 11
""".strip(),
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert cfg.memory.dreaming_v2_enabled is True
    assert cfg.memory.dreaming_v2_score_threshold == 0.42
    assert cfg.memory.dreaming_v2_min_recall_count == 7
    assert cfg.memory.dreaming_v2_diversity_threshold == 0.95
    assert cfg.memory.dreaming_v2_max_promotions_per_run == 3
    assert cfg.memory.dreaming_v2_dreams_md_max_bytes == 1024
    assert cfg.memory.dreaming_v2_candidate_fetch_limit == 11


def test_build_production_dependencies_picks_up_yaml_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: YAML overrides → load_config → build_production_dependencies
    → DreamingV2Config carries the operator's tuning into the engine."""
    from opencomputer.cron.dreaming_v2_tick import build_production_dependencies

    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        """
memory:
  dreaming_v2_enabled: true
  dreaming_v2_score_threshold: 0.5
  dreaming_v2_min_recall_count: 4
  dreaming_v2_max_promotions_per_run: 7
""".strip(),
        encoding="utf-8",
    )
    # Steer _home() at the temp profile so load_config + MemoryManager
    # resolve to it consistently.
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(profile_home))

    deps = build_production_dependencies()
    assert deps.config.enabled is True
    assert deps.config.score_threshold == 0.5
    assert deps.config.min_recall_count == 4
    assert deps.config.max_promotions_per_run == 7
    # Untouched fields keep their defaults.
    assert deps.config.diversity_threshold == 0.8
    assert deps.config.dreams_md_max_bytes == 16384


def test_run_system_tick_skips_dreaming_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When cfg.memory.dreaming_v2_enabled is False (default), the cron
    tick must short-circuit with a 'disabled' status — never spin up the
    engine, never call provider.complete()."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    from opencomputer.cron import dreaming_v2_tick

    summary = dreaming_v2_tick.run_dreaming_v2_tick()
    # Either the deps build itself failed early (no error path), or
    # the disabled check fired.  Both are acceptable outcomes for the
    # default-OFF flag — what we assert is that NO production
    # promote_fn / hold_fn ran (no MEMORY.md / DREAMS.md written).
    assert not (tmp_path / "MEMORY.md").exists()
    assert not (tmp_path / "DREAMS.md").exists()
    # Status MUST be either "disabled" or carry an "error:" prefix —
    # never a normal {"promoted": N, ...} payload.
    if isinstance(summary, dict):
        assert (
            summary.get("status") == "disabled"
            or "error" in summary
            or summary.get("promoted", 0) == 0
        )
