"""Tests for the Milestone-2 sandbox cost guard (task T2.8).

Two layers:

* :class:`~opencomputer.cost_guard.sandbox.SandboxCostGuard` — per-second
  rate lookup, per-session spend recording, the configurable session
  cap, and the ``sandbox_cost_guard.json`` round-trip. The guard owns
  its own file (separate from the provider
  :class:`~opencomputer.cost_guard.guard.CostGuard`'s ``cost_guard.json``)
  so one writer per file fully serializes intra-process writes.
* The ``BashTool`` cost hook — a sandboxed run records
  ``duration × rate(backend)`` against the session, costs ``$0`` for a
  free local backend, and a session already over cap is refused before
  the command runs.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from opencomputer.cost_guard import CostGuard, get_default_guard
from opencomputer.cost_guard.guard import _reset_default_guard_for_tests
from opencomputer.cost_guard.sandbox import (
    DEFAULT_E2B_RATE_USD_PER_SECOND,
    DEFAULT_SESSION_CAP_USD,
    SandboxCostGuard,
    _reset_default_sandbox_cost_guard_for_tests,
    get_default_sandbox_cost_guard,
)
from opencomputer.tools.bash import BashTool
from plugin_sdk.core import ToolCall
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.sandbox import SandboxResult


@pytest.fixture
def guard(tmp_path: Path) -> SandboxCostGuard:
    return SandboxCostGuard(storage_path=tmp_path / "sandbox_cost_guard.json")


@pytest.fixture(autouse=True)
def isolate_default_guards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Re-root both module-level guards at a throwaway profile home."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    _reset_default_sandbox_cost_guard_for_tests()
    _reset_default_guard_for_tests()
    yield
    _reset_default_sandbox_cost_guard_for_tests()
    _reset_default_guard_for_tests()


# ─── rates ─────────────────────────────────────────────────────────────


class TestRates:
    def test_e2b_rate_defaults_to_published_figure(
        self, guard: SandboxCostGuard
    ) -> None:
        """A fresh file seeds the E2B rate from the survey's published value."""
        assert guard.rate_for("e2b") == pytest.approx(
            DEFAULT_E2B_RATE_USD_PER_SECOND
        )

    def test_local_backends_are_free(self, guard: SandboxCostGuard) -> None:
        """docker / bwrap / macos / ssh / none have no rate → $0/second."""
        for backend in ("docker", "linux_bwrap", "macos_sandbox_exec", "ssh", "none"):
            assert guard.rate_for(backend) == 0.0

    def test_unknown_backend_is_free(self, guard: SandboxCostGuard) -> None:
        assert guard.rate_for("some-future-backend") == 0.0

    def test_rate_is_case_insensitive(self, guard: SandboxCostGuard) -> None:
        assert guard.rate_for("E2B") == guard.rate_for("e2b")

    def test_set_rate_persists(self, guard: SandboxCostGuard) -> None:
        guard.set_rate("e2b", usd_per_second=0.001)
        assert guard.rate_for("e2b") == pytest.approx(0.001)

    def test_set_rate_rejects_negative(self, guard: SandboxCostGuard) -> None:
        with pytest.raises(ValueError):
            guard.set_rate("e2b", usd_per_second=-0.1)

    def test_set_rate_for_a_local_backend_makes_it_paid(
        self, guard: SandboxCostGuard
    ) -> None:
        """An operator can opt a normally-free backend into metered cost."""
        guard.set_rate("docker", usd_per_second=0.0001)
        assert guard.rate_for("docker") == pytest.approx(0.0001)


# ─── cost computation ──────────────────────────────────────────────────


class TestCostForRun:
    def test_e2b_cost_is_duration_times_rate(
        self, guard: SandboxCostGuard
    ) -> None:
        cost = guard.cost_for_run(backend="e2b", duration_seconds=10.0)
        assert cost == pytest.approx(DEFAULT_E2B_RATE_USD_PER_SECOND * 10.0)

    def test_local_backend_run_costs_zero(self, guard: SandboxCostGuard) -> None:
        assert guard.cost_for_run(backend="docker", duration_seconds=30.0) == 0.0

    def test_zero_duration_costs_zero(self, guard: SandboxCostGuard) -> None:
        assert guard.cost_for_run(backend="e2b", duration_seconds=0.0) == 0.0

    def test_negative_duration_costs_zero(self, guard: SandboxCostGuard) -> None:
        assert guard.cost_for_run(backend="e2b", duration_seconds=-5.0) == 0.0


# ─── per-session spend recording ───────────────────────────────────────


class TestRecordRun:
    def test_record_e2b_run_adds_session_spend(
        self, guard: SandboxCostGuard
    ) -> None:
        cost = guard.record_run("sess-1", backend="e2b", duration_seconds=10.0)
        assert cost == pytest.approx(DEFAULT_E2B_RATE_USD_PER_SECOND * 10.0)
        assert guard.session_spend("sess-1") == pytest.approx(cost)

    def test_record_local_run_records_nothing(
        self, guard: SandboxCostGuard
    ) -> None:
        """A free local backend records $0 — the session stays at zero."""
        cost = guard.record_run("sess-1", backend="docker", duration_seconds=99.0)
        assert cost == 0.0
        assert guard.session_spend("sess-1") == 0.0

    def test_multiple_runs_accumulate(self, guard: SandboxCostGuard) -> None:
        guard.record_run("sess-1", backend="e2b", duration_seconds=5.0)
        guard.record_run("sess-1", backend="e2b", duration_seconds=7.0)
        assert guard.session_spend("sess-1") == pytest.approx(
            DEFAULT_E2B_RATE_USD_PER_SECOND * 12.0
        )

    def test_spend_is_per_session(self, guard: SandboxCostGuard) -> None:
        guard.record_run("sess-1", backend="e2b", duration_seconds=10.0)
        assert guard.session_spend("sess-2") == 0.0

    def test_empty_session_id_records_nothing(
        self, guard: SandboxCostGuard
    ) -> None:
        assert guard.record_run("", backend="e2b", duration_seconds=10.0) == 0.0

    def test_reset_session_clears_one_session(
        self, guard: SandboxCostGuard
    ) -> None:
        guard.record_run("sess-1", backend="e2b", duration_seconds=10.0)
        guard.record_run("sess-2", backend="e2b", duration_seconds=10.0)
        guard.reset_session("sess-1")
        assert guard.session_spend("sess-1") == 0.0
        assert guard.session_spend("sess-2") > 0.0

    def test_reset_all_sessions(self, guard: SandboxCostGuard) -> None:
        guard.record_run("sess-1", backend="e2b", duration_seconds=10.0)
        guard.record_run("sess-2", backend="e2b", duration_seconds=10.0)
        guard.reset_session(None)
        assert guard.session_spend("sess-1") == 0.0
        assert guard.session_spend("sess-2") == 0.0


# ─── the session cap ───────────────────────────────────────────────────


class TestSessionCap:
    def test_default_cap_is_one_dollar(self, guard: SandboxCostGuard) -> None:
        assert guard.session_cap_usd() == pytest.approx(DEFAULT_SESSION_CAP_USD)
        assert DEFAULT_SESSION_CAP_USD == 1.0

    def test_fresh_session_is_within_budget(
        self, guard: SandboxCostGuard
    ) -> None:
        decision = guard.check_session_budget("sess-1")
        assert decision.allowed is True

    def test_session_over_cap_is_refused(self, guard: SandboxCostGuard) -> None:
        """A session whose recorded spend exceeds the cap is refused."""
        guard.set_session_cap(0.01)
        # 0.01 cap, rate ~3.25e-5/s → ~308s to cross. Record well past it.
        guard.record_run("sess-1", backend="e2b", duration_seconds=1000.0)
        decision = guard.check_session_budget("sess-1")
        assert decision.allowed is False
        assert "cap exceeded" in decision.reason
        assert decision.session_spend_usd > decision.session_cap_usd

    def test_projected_cost_can_push_over_cap(
        self, guard: SandboxCostGuard
    ) -> None:
        guard.set_session_cap(1.0)
        decision = guard.check_session_budget(
            "sess-1", projected_cost_usd=2.0
        )
        assert decision.allowed is False

    def test_set_cap_rejects_negative(self, guard: SandboxCostGuard) -> None:
        with pytest.raises(ValueError):
            guard.set_session_cap(-1.0)

    def test_zero_cap_refuses_any_paid_run(
        self, guard: SandboxCostGuard
    ) -> None:
        """A $0 cap means no paid sandboxed run is ever in budget."""
        guard.set_session_cap(0.0)
        guard.record_run("sess-1", backend="e2b", duration_seconds=1.0)
        assert guard.check_session_budget("sess-1").allowed is False


# ─── sandbox_cost_guard.json storage file ──────────────────────────────


class TestStorageFile:
    def test_sandbox_section_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "sandbox_cost_guard.json"
        g1 = SandboxCostGuard(storage_path=path)
        g1.record_run("sess-1", backend="e2b", duration_seconds=10.0)
        # A fresh guard on the same file reads the persisted spend.
        g2 = SandboxCostGuard(storage_path=path)
        assert g2.session_spend("sess-1") > 0.0

    def test_default_guard_uses_its_own_file(self) -> None:
        """The default sandbox guard owns ``sandbox_cost_guard.json`` — its
        own file, distinct from the provider guard's ``cost_guard.json``."""
        sandbox_guard = get_default_sandbox_cost_guard()
        assert sandbox_guard._storage_path.name == "sandbox_cost_guard.json"
        assert (
            sandbox_guard._storage_path
            != get_default_guard()._storage_path
        )

    def test_owns_its_file_separately_from_the_provider_guard(
        self, tmp_path: Path
    ) -> None:
        """The two guards write distinct files — a sandbox write touches
        only ``sandbox_cost_guard.json`` and leaves ``cost_guard.json``
        (the provider guard's data) untouched, and vice versa."""
        provider_path = tmp_path / "cost_guard.json"
        sandbox_path = tmp_path / "sandbox_cost_guard.json"

        provider_guard = CostGuard(storage_path=provider_path)
        provider_guard.set_limit("anthropic", daily=5.0)
        provider_guard.record_usage("anthropic", cost_usd=0.10, operation="chat")

        SandboxCostGuard(storage_path=sandbox_path).record_run(
            "sess-1", backend="e2b", duration_seconds=10.0
        )

        # Each guard's data survived the other's write — they share nothing.
        usage = CostGuard(storage_path=provider_path).current_usage("anthropic")
        assert usage[0].daily_used == pytest.approx(0.10)
        assert usage[0].daily_limit == pytest.approx(5.0)
        assert (
            SandboxCostGuard(storage_path=sandbox_path).session_spend("sess-1")
            > 0.0
        )
        # The sandbox write created its own file and did not touch the
        # provider guard's file shape.
        provider_raw = json.loads(provider_path.read_text())
        assert "sandbox" not in provider_raw

    def test_sandbox_section_lands_in_json(self, tmp_path: Path) -> None:
        path = tmp_path / "sandbox_cost_guard.json"
        SandboxCostGuard(storage_path=path).record_run(
            "sess-1", backend="e2b", duration_seconds=10.0
        )
        raw = json.loads(path.read_text())
        assert "sandbox" in raw
        assert "sess-1" in raw["sandbox"]["sessions"]
        # The guard owns the whole file — no provider-guard keys leak in.
        assert set(raw) == {"sandbox"}


# ─── the BashTool cost hook ────────────────────────────────────────────


class _FakeStrategy:
    """A stub sandbox strategy that returns a caller-supplied result."""

    def __init__(self, name: str, *, duration: float) -> None:
        self.name = name
        self._duration = duration

    async def run(self, argv, *, config, stdin=None, cwd=None):  # noqa: ANN001
        return SandboxResult(
            exit_code=0,
            stdout="ok\n",
            stderr="",
            duration_seconds=self._duration,
            wrapped_command=list(argv),
            strategy_name=self.name,
        )


def _runtime_with_sandbox(session_id: str, strategy: _FakeStrategy) -> RuntimeContext:
    """Build a RuntimeContext carrying a resolved sandbox strategy + session id."""
    from opencomputer.tools.bash import _SANDBOX_STRATEGY_KEY

    return RuntimeContext(
        custom={
            _SANDBOX_STRATEGY_KEY: strategy,
            "session_id": session_id,
        }
    )


@pytest.mark.asyncio
async def test_bash_records_e2b_run_cost(tmp_path: Path) -> None:
    """A sandboxed e2b Bash run records duration × rate against the session."""
    strategy = _FakeStrategy("e2b", duration=20.0)
    BashTool.set_runtime(_runtime_with_sandbox("hook-sess", strategy))
    try:
        result = await BashTool().execute(
            ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})
        )
        assert result.is_error is False
        guard = get_default_sandbox_cost_guard()
        assert guard.session_spend("hook-sess") == pytest.approx(
            DEFAULT_E2B_RATE_USD_PER_SECOND * 20.0
        )
    finally:
        BashTool.set_runtime(RuntimeContext())


@pytest.mark.asyncio
async def test_bash_local_backend_run_records_zero(tmp_path: Path) -> None:
    """A sandboxed run on a free local backend records $0."""
    strategy = _FakeStrategy("docker", duration=45.0)
    BashTool.set_runtime(_runtime_with_sandbox("hook-sess", strategy))
    try:
        result = await BashTool().execute(
            ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})
        )
        assert result.is_error is False
        assert get_default_sandbox_cost_guard().session_spend("hook-sess") == 0.0
    finally:
        BashTool.set_runtime(RuntimeContext())


@pytest.mark.asyncio
async def test_bash_refuses_when_session_over_cap(tmp_path: Path) -> None:
    """A session already over its sandbox cap is refused before the run."""
    # Drive the session over a tiny cap first.
    guard = get_default_sandbox_cost_guard()
    guard.set_session_cap(0.001)
    guard.record_run("over-sess", backend="e2b", duration_seconds=10_000.0)

    strategy = _FakeStrategy("e2b", duration=20.0)
    BashTool.set_runtime(_runtime_with_sandbox("over-sess", strategy))
    try:
        result = await BashTool().execute(
            ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})
        )
        assert result.is_error is True
        assert "cap exceeded" in result.content
        # The refusal must not have run up *more* cost — spend unchanged
        # bar the seed run (no second record from the refused call).
        spend_after = guard.session_spend("over-sess")
        assert spend_after == pytest.approx(
            DEFAULT_E2B_RATE_USD_PER_SECOND * 10_000.0
        )
    finally:
        BashTool.set_runtime(RuntimeContext())


@pytest.mark.asyncio
async def test_bash_over_cap_does_not_block_free_local_backend(
    tmp_path: Path,
) -> None:
    """Being over the (paid) cap must not refuse a free local-backend run.

    The cap is about paid spend — a $0/second local backend can never
    push a session over it, so an over-cap session still runs locally.
    """
    guard = get_default_sandbox_cost_guard()
    guard.set_session_cap(0.001)
    guard.record_run("over-sess", backend="e2b", duration_seconds=10_000.0)

    strategy = _FakeStrategy("docker", duration=5.0)
    BashTool.set_runtime(_runtime_with_sandbox("over-sess", strategy))
    try:
        result = await BashTool().execute(
            ToolCall(id="c1", name="Bash", arguments={"command": "echo hi"})
        )
        assert result.is_error is False  # local backend ran fine
    finally:
        BashTool.set_runtime(RuntimeContext())
