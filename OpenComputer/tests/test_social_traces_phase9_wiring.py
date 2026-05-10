"""Phase 9 production-wiring tests.

Validates the changes that make the post-task subscriber actually
fire LLM calls in production (vs. tests that wire provider directly):

* ``plugin.register()`` now registers ONLY the BEFORE_TASK hook —
  the auto-start of a degraded subscriber is gone.
* ``wire_subscriber()`` is the canonical entry point gateway + CLI
  call. Idempotent: a prior subscriber is stopped before a new one
  is created.
* ``stop_subscriber()`` cleans up; idempotent.
* ``is_session_worth_distilling()`` heuristic gate filters trivial
  sessions before paying any LLM cost.
* The full pipeline runs (or doesn't) through wire_subscriber +
  stop_subscriber lifecycle correctly.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

# Alias bootstrap (mirrors earlier-phase tests)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    if "extensions.social_traces.plugin" in sys.modules:
        return
    if "extensions" not in sys.modules:
        ext_pkg = types.ModuleType("extensions")
        ext_pkg.__path__ = [str(_EXT_DIR)]
        ext_pkg.__package__ = "extensions"
        sys.modules["extensions"] = ext_pkg
    if "extensions.social_traces" not in sys.modules:
        mod = types.ModuleType("extensions.social_traces")
        mod.__path__ = [str(_ST_DIR)]
        mod.__package__ = "extensions.social_traces"
        sys.modules["extensions.social_traces"] = mod
        sys.modules["extensions"].social_traces = mod  # type: ignore[attr-defined]
    parent = sys.modules["extensions.social_traces"]
    for sub in (
        "state",
        "identity",
        "config",
        "session_state",
        "tag_extractor",
        "redactor",
        "novelty_judge",
        "distiller",
        "prefetch",
        "subscriber",
        "plugin",
    ):
        full_name = f"extensions.social_traces.{sub}"
        if full_name in sys.modules:
            setattr(parent, sub, sys.modules[full_name])
            continue
        init = _ST_DIR / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.social_traces"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)
        setattr(parent, sub, sub_mod)


_ensure_alias()

from extensions.social_traces import plugin as st_plugin  # noqa: E402
from extensions.social_traces import session_state as bridge  # noqa: E402
from extensions.social_traces import state as st_state  # noqa: E402
from extensions.social_traces import subscriber as st_sub  # noqa: E402

from plugin_sdk.core import Message  # noqa: E402
from plugin_sdk.hooks import HookEvent  # noqa: E402
from plugin_sdk.ingestion import SessionEndEvent  # noqa: E402
from plugin_sdk.provider_contract import (  # noqa: E402
    BaseProvider,
    ProviderResponse,
    Usage,
)
from plugin_sdk.traces import (  # noqa: E402
    QueryResult,
    SubmitReceipt,
    TraceCard,
    TraceNetworkClient,
)


@pytest.fixture(autouse=True)
def _isolate_state():
    """Reset bridge + active-subscriber so cross-test leakage can't
    mask production-wiring bugs."""
    bridge.reset_for_testing()
    st_plugin.stop_subscriber()
    yield
    st_plugin.stop_subscriber()
    bridge.reset_for_testing()


class _FakeProvider(BaseProvider):
    async def complete(self, **kw):
        return ProviderResponse(
            message=Message(role="assistant", content='{"novel": false}'),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(self, **kw):  # pragma: no cover
        yield


class _FakeCostGuard:
    def check_budget(self, *_a, **_kw):
        return True

    def record_usage(self, *_a, **_kw):
        return None


# ─── plugin.register: BEFORE_TASK hook only ──────────────────────────


def test_register_only_attaches_before_task_hook():
    """After register(), the BEFORE_TASK hook is registered but no
    subscriber is auto-started — production wiring has to call
    wire_subscriber explicitly."""
    from opencomputer.hooks.engine import HookEngine

    engine = HookEngine()

    class _StubAPI:
        def register_hook(self, spec):
            engine.register(spec)

    st_plugin.register(_StubAPI())

    # BEFORE_TASK was registered.
    assert len(engine._ordered_specs(HookEvent.BEFORE_TASK)) == 1
    # No subscriber auto-started.
    assert st_plugin.get_active_subscriber() is None


# ─── wire_subscriber: the canonical entry point ──────────────────────


def test_wire_subscriber_starts_and_stores_singleton():
    """A first call to wire_subscriber constructs + starts the
    subscriber and stashes it for stop_subscriber to find."""
    sub = st_plugin.wire_subscriber(
        provider=_FakeProvider(),
        cost_guard=_FakeCostGuard(),
        harness_version="opencomputer/test",
    )
    assert sub is not None
    assert st_plugin.get_active_subscriber() is sub
    # Stop it so the autouse fixture's cleanup is a no-op.
    st_plugin.stop_subscriber()
    assert st_plugin.get_active_subscriber() is None


def test_wire_subscriber_is_idempotent_replaces_prior():
    """Two consecutive wire_subscriber calls: the first subscriber
    is stopped, the second takes its place. Mirrors how config
    changes / restarts should work without leaving zombie
    subscribers attached to the bus."""
    sub1 = st_plugin.wire_subscriber(
        provider=_FakeProvider(),
        cost_guard=_FakeCostGuard(),
    )
    sub2 = st_plugin.wire_subscriber(
        provider=_FakeProvider(),
        cost_guard=_FakeCostGuard(),
    )
    assert sub1 is not sub2
    assert st_plugin.get_active_subscriber() is sub2


def test_stop_subscriber_idempotent():
    """stop_subscriber on an empty state is a no-op (doesn't raise);
    after wire + stop the singleton is None."""
    st_plugin.stop_subscriber()  # nothing wired — must be a no-op
    assert st_plugin.get_active_subscriber() is None

    st_plugin.wire_subscriber(provider=_FakeProvider(), cost_guard=_FakeCostGuard())
    st_plugin.stop_subscriber()
    assert st_plugin.get_active_subscriber() is None
    # Calling again must still be a no-op.
    st_plugin.stop_subscriber()


# ─── heuristic gate ──────────────────────────────────────────────────


def test_session_worth_distilling_passes_real_session():
    """A normal session with real turns + duration must pass the
    gate."""
    e = SessionEndEvent(
        session_id="x", turn_count=3, duration_seconds=12.0,
    )
    assert st_sub.is_session_worth_distilling(e) is True


def test_session_worth_distilling_rejects_zero_turns():
    e = SessionEndEvent(session_id="x", turn_count=0, duration_seconds=20.0)
    assert st_sub.is_session_worth_distilling(e) is False


def test_session_worth_distilling_rejects_one_turn():
    """One-turn sessions = user asked, agent answered, no tools
    used. Nothing procedural to share — skip."""
    e = SessionEndEvent(session_id="x", turn_count=1, duration_seconds=20.0)
    assert st_sub.is_session_worth_distilling(e) is False


def test_session_worth_distilling_rejects_short_duration():
    """Sub-3-second sessions = cancellation, tool guard abort,
    error path. Skip."""
    e = SessionEndEvent(session_id="x", turn_count=5, duration_seconds=0.5)
    assert st_sub.is_session_worth_distilling(e) is False


def test_session_worth_distilling_passes_failed_session():
    """A session with had_errors=True is still worth distilling —
    failure-mode traces are valuable per the HANDOVER edge-case
    rule. Don't filter on outcome here."""
    e = SessionEndEvent(
        session_id="x",
        turn_count=4,
        duration_seconds=15.0,
        had_errors=True,
    )
    assert st_sub.is_session_worth_distilling(e) is True


# ─── subscriber pipeline applies the gate ────────────────────────────


class _RecordingClient(TraceNetworkClient):
    def __init__(self):
        self.submits: list[TraceCard] = []

    async def query(self, intent, tags, *, limit=3, timeout_s=1.0):
        return QueryResult()

    async def submit(self, card):
        self.submits.append(card)
        return SubmitReceipt(accepted=True, queue_id="r-1")

    async def health(self, *, timeout_s=1.0):
        return True


def _build_sub_for_pipeline_tests(profile_home: Path, client) -> st_sub.TraceEmissionSubscriber:
    from extensions.social_traces.config import SocialTracesConfig

    cfg = SocialTracesConfig()

    class _Bus:
        def subscribe(self, _e, _h):
            class _Sub:
                def unsubscribe(self): ...
            return _Sub()

    return st_sub.TraceEmissionSubscriber(
        bus=_Bus(),
        profile_home_factory=lambda: profile_home,
        client_factory=lambda _ph, _cfg: client,
        config_factory=lambda _ph: cfg,
        provider=_FakeProvider(),
        cost_guard=_FakeCostGuard(),
    )


async def test_pipeline_skips_trivial_session_before_distill(
    tmp_path: Path, monkeypatch
):
    """A trivial session (turn_count=1) must not reach the distiller
    even though everything else is wired correctly."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", None)

    distill_calls: list[str] = []

    async def _distill(*, session_id, **_kw):
        distill_calls.append(session_id)
        return None

    from extensions.social_traces import distiller as st_distiller

    monkeypatch.setattr(st_distiller, "distill_session", _distill)

    client = _RecordingClient()
    sub = _build_sub_for_pipeline_tests(tmp_path, client)
    trivial_event = SessionEndEvent(
        session_id="sid", turn_count=1, duration_seconds=2.0,
    )
    await sub._run_pipeline(trivial_event, tmp_path)

    assert distill_calls == []
    assert client.submits == []


async def test_pipeline_runs_for_normal_session(tmp_path: Path, monkeypatch):
    """A normal session passes the gate and reaches the distiller."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", None)

    distill_calls: list[str] = []

    async def _distill(*, session_id, **_kw):
        distill_calls.append(session_id)
        return None

    from extensions.social_traces import distiller as st_distiller

    monkeypatch.setattr(st_distiller, "distill_session", _distill)

    client = _RecordingClient()
    sub = _build_sub_for_pipeline_tests(tmp_path, client)
    real_event = SessionEndEvent(
        session_id="sid", turn_count=4, duration_seconds=15.0,
    )
    await sub._run_pipeline(real_event, tmp_path)

    assert distill_calls == ["sid"]
