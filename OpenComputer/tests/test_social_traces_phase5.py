"""Phase 5 tests for the post-task SessionEndEvent subscriber.

Coverage focus (matching ``docs/plans/social-traces-plugin.md`` §10
Phase 5 acceptance):

* ``session_state`` bridge: set/peek/pop round-trip; LRU eviction
  past the cap; thread-safety smoke check.
* Prefetch handler also writes the bridge (in addition to
  ``runtime.custom``) — closes the gap from the Phase 4 finding.
* Subscriber lifecycle: start/stop idempotent against a stub bus.
* Decision tree:
  - disabled flag → no work
  - untracked session → silent
  - trace_used set + judge says not-novel → silent
  - trace_used set + judge says novel → continues to distill
  - no trace used → continues to distill
  - distiller returns None (Phase 5 stub) → silent (no submit)
  - distiller returns a TraceCard (Phase 7 simulated) → submit called
* End-to-end through ``AgentLoop.run_conversation``: full pre-task
  lookup + session_end event + bridge cleanup.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from plugin_sdk.ingestion import SessionEndEvent
from plugin_sdk.traces import (
    QueryResult,
    SubmitReceipt,
    TraceCard,
    TraceMeta,
    TraceNetworkClient,
    TraceStep,
)

# ─── alias bootstrap (mirrors cli_traces._ensure_alias) ──────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    if "extensions.social_traces.session_state" in sys.modules:
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
        "novelty_judge",
        "distiller",
        "prefetch",
        "subscriber",
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

    client_dir = _ST_DIR / "client"
    client_init_path = client_dir / "__init__.py"
    if "extensions.social_traces.client" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "extensions.social_traces.client",
            str(client_init_path),
            submodule_search_locations=[str(client_dir)],
        )
        assert spec is not None and spec.loader is not None
        client_pkg = importlib.util.module_from_spec(spec)
        sys.modules["extensions.social_traces.client"] = client_pkg
        spec.loader.exec_module(client_pkg)
        setattr(parent, "client", client_pkg)
    for sub in ("local_file",):
        full_name = f"extensions.social_traces.client.{sub}"
        if full_name in sys.modules:
            continue
        init = client_dir / f"{sub}.py"
        if not init.exists():
            continue
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        if spec is None or spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.social_traces.client"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)


_ensure_alias()

from extensions.social_traces import distiller as st_distiller  # noqa: E402
from extensions.social_traces import novelty_judge as st_novelty  # noqa: E402
from extensions.social_traces import prefetch as st_prefetch  # noqa: E402
from extensions.social_traces import session_state as bridge  # noqa: E402
from extensions.social_traces import state as st_state  # noqa: E402
from extensions.social_traces import subscriber as st_sub  # noqa: E402
from extensions.social_traces.config import SocialTracesConfig  # noqa: E402


# ─── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_bridge():
    """Always reset the module-level bridge between tests so cross-
    test leakage can't mask bugs."""
    bridge.reset_for_testing()
    yield
    bridge.reset_for_testing()


def _make_card(
    *,
    intent: str = "test",
    tags: tuple[str, ...] = ("test",),
    insight: str = "x",
    trace_id: str = "t1",
) -> TraceCard:
    return TraceCard(
        schema_version="v1",
        intent=intent,
        meta=TraceMeta(
            tags=tags,
            outcome="success",
            token_cost=100,
            loop_count=1,
            harness_version="opencomputer/0.1.0",
            submitter_hash="0" * 64,
        ),
        steps=(),
        distilled_insight=insight,
        created_at="2026-05-05T12:00:00Z",
        id=trace_id,
    )


# ─── session_state bridge ─────────────────────────────────────────────


def test_bridge_unknown_session_returns_none():
    assert bridge.peek_trace_used("nope") is None
    assert bridge.session_known("nope") is False
    assert bridge.hit_count("nope") == 0
    assert bridge.pop_session("nope") is None


def test_bridge_set_then_peek():
    bridge.set_trace_used("sid-1", "trace-abc")
    assert bridge.peek_trace_used("sid-1") == "trace-abc"
    assert bridge.session_known("sid-1") is True
    assert bridge.hit_count("sid-1") == 1


def test_bridge_set_none_distinguishable_from_unknown():
    """``set_trace_used(sid, None)`` records 'BEFORE_TASK fired but no
    trace cleared the bar' — distinct from 'BEFORE_TASK never fired'.
    The decision tree depends on this distinction."""
    bridge.set_trace_used("sid-1", None)
    assert bridge.peek_trace_used("sid-1") is None
    assert bridge.session_known("sid-1") is True  # it IS known
    assert bridge.hit_count("sid-1") == 0  # no hits


def test_bridge_pop_clears_entry():
    bridge.set_trace_used("sid-1", "trace-x")
    entry = bridge.pop_session("sid-1")
    assert entry is not None
    assert entry.trace_used == "trace-x"
    assert entry.hit_count == 1
    # Subsequent pop returns None (state is gone).
    assert bridge.pop_session("sid-1") is None
    assert bridge.session_known("sid-1") is False


def test_bridge_repeated_set_increments_hit_count_only_for_non_none():
    """Multiple BEFORE_TASK fires for the same session id: latest
    trace_used wins; hit_count counts non-None updates only."""
    bridge.set_trace_used("sid-1", "trace-a")
    bridge.set_trace_used("sid-1", None)  # checked, found nothing
    bridge.set_trace_used("sid-1", "trace-b")
    assert bridge.peek_trace_used("sid-1") == "trace-b"
    assert bridge.hit_count("sid-1") == 2


def test_bridge_lru_eviction_past_cap():
    """Past the soft cap, oldest entries are evicted to keep memory
    bounded."""
    bridge.reset_for_testing(max_tracked=3)
    try:
        bridge.set_trace_used("a", "x")
        bridge.set_trace_used("b", "x")
        bridge.set_trace_used("c", "x")
        bridge.set_trace_used("d", "x")  # forces eviction of "a"
        assert bridge.session_known("a") is False
        assert bridge.session_known("b") is True
        assert bridge.session_known("c") is True
        assert bridge.session_known("d") is True
    finally:
        bridge.reset_for_testing()  # restore default cap


def test_bridge_thread_safety_smoke():
    """Concurrent writes from multiple threads must not corrupt the
    state. Smoke test — hits the lock path under contention."""
    def _writer(prefix: str):
        for i in range(50):
            bridge.set_trace_used(f"{prefix}-{i}", f"trace-{i}")

    threads = [threading.Thread(target=_writer, args=(f"p{j}",)) for j in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bridge.tracked_session_count() == 4 * 50


# ─── prefetch writes the bridge ──────────────────────────────────────


async def test_prefetch_match_writes_bridge(tmp_path: Path):
    """When the prefetch path injects a trace, the bridge entry must
    be set so the post-task subscriber can read it."""
    from plugin_sdk.core import Message as _M
    from plugin_sdk.hooks import HookContext, HookEvent
    from plugin_sdk.runtime_context import RuntimeContext

    # Seed inbox + enable
    st_state.set_enabled(tmp_path, True)
    inbox = tmp_path / "traces" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    import json

    from extensions.social_traces.client.local_file import trace_card_to_dict

    seeded = _make_card(
        intent="sync homelab files",
        tags=("homelab", "filesync"),
        trace_id="seeded-1",
    )
    (inbox / "seeded-1.json").write_text(
        json.dumps(trace_card_to_dict(seeded)), encoding="utf-8"
    )

    runtime = RuntimeContext(custom={"profile_home": str(tmp_path)})
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="prefetch-sid",
        runtime=runtime,
        message=_M(role="user", content="sync homelab files via filesync"),
    )

    decision = await st_prefetch.on_before_task(ctx)
    assert decision.decision == "rewrite"

    # Bridge must reflect the hit.
    assert bridge.peek_trace_used("prefetch-sid") == "seeded-1"
    assert bridge.hit_count("prefetch-sid") == 1


async def test_prefetch_no_match_writes_bridge_with_none(tmp_path: Path):
    """No matching trace → bridge entry exists but trace_used is None.
    Subscriber's session_known()=True, hit_count()=0 — emit candidate."""
    from plugin_sdk.core import Message as _M
    from plugin_sdk.hooks import HookContext, HookEvent
    from plugin_sdk.runtime_context import RuntimeContext

    st_state.set_enabled(tmp_path, True)
    runtime = RuntimeContext(custom={"profile_home": str(tmp_path)})
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="empty-sid",
        runtime=runtime,
        message=_M(role="user", content="something nobody has solved"),
    )

    await st_prefetch.on_before_task(ctx)
    assert bridge.session_known("empty-sid") is True
    assert bridge.peek_trace_used("empty-sid") is None
    assert bridge.hit_count("empty-sid") == 0


# ─── subscriber lifecycle ─────────────────────────────────────────────


class _StubBus:
    """Minimal duck-typed bus for subscriber tests."""

    def __init__(self):
        self.subscribed: list[tuple[str, Any]] = []
        self.unsubscribed = 0

    def subscribe(self, event_type: str, handler):
        self.subscribed.append((event_type, handler))
        outer = self

        class _Sub:
            def unsubscribe(self):
                outer.unsubscribed += 1

        return _Sub()


def _build_subscriber(
    *,
    bus,
    profile_home: Path,
    config: SocialTracesConfig | None = None,
    client: TraceNetworkClient | None = None,
) -> st_sub.TraceEmissionSubscriber:
    cfg = config or SocialTracesConfig()
    captured_client = client

    def _client_factory(_ph: Path, _cfg: SocialTracesConfig):
        return captured_client

    return st_sub.TraceEmissionSubscriber(
        bus=bus,
        profile_home_factory=lambda: profile_home,
        client_factory=_client_factory,
        config_factory=lambda _ph: cfg,
    )


def test_subscriber_start_subscribes_to_session_end(tmp_path: Path):
    bus = _StubBus()
    sub = _build_subscriber(bus=bus, profile_home=tmp_path)
    sub.start()
    assert len(bus.subscribed) == 1
    assert bus.subscribed[0][0] == "session_end"


def test_subscriber_start_idempotent(tmp_path: Path):
    bus = _StubBus()
    sub = _build_subscriber(bus=bus, profile_home=tmp_path)
    sub.start()
    sub.start()  # second call should be a no-op
    assert len(bus.subscribed) == 1


def test_subscriber_stop_unsubscribes(tmp_path: Path):
    bus = _StubBus()
    sub = _build_subscriber(bus=bus, profile_home=tmp_path)
    sub.start()
    sub.stop()
    assert bus.unsubscribed == 1
    sub.stop()  # idempotent
    assert bus.unsubscribed == 1


# ─── subscriber decision tree ────────────────────────────────────────


class _RecordingClient(TraceNetworkClient):
    """Captures every submit() call so tests can assert on the body."""

    def __init__(self):
        self.submitted: list[TraceCard] = []
        self.next_receipt = SubmitReceipt(accepted=True, queue_id="rec-1")

    async def query(self, intent, tags, *, limit=3, timeout_s=1.0):
        return QueryResult()

    async def submit(self, card):
        self.submitted.append(card)
        return self.next_receipt

    async def health(self, *, timeout_s=1.0):
        return True


async def test_subscriber_disabled_flag_skips_pipeline(tmp_path: Path):
    """When the on-disk flag is off, the pipeline must not run — the
    fire-and-forget task is never spawned."""
    bus = _StubBus()
    client = _RecordingClient()
    sub = _build_subscriber(bus=bus, profile_home=tmp_path, client=client)
    # Note: NOT calling st_state.set_enabled — flag is off.

    # Bridge entry exists but pipeline must skip on disabled flag.
    bridge.set_trace_used("sid", None)

    await sub._handle_event(SessionEndEvent(session_id="sid", turn_count=3, duration_seconds=10.0))
    # Allow any inadvertent fire-and-forget tasks to settle.
    await asyncio.sleep(0.05)

    assert client.submitted == []
    # AND the bridge must be popped even when the flag is off — daemon
    # mustn't leak state from disabled windows.
    assert bridge.session_known("sid") is False


async def test_subscriber_untracked_session_skips(tmp_path: Path):
    """BEFORE_TASK never fired for this session (no bridge entry) → no
    distill/submit work."""
    st_state.set_enabled(tmp_path, True)
    client = _RecordingClient()
    sub = _build_subscriber(bus=_StubBus(), profile_home=tmp_path, client=client)

    await sub._run_pipeline(SessionEndEvent(session_id="ghost", turn_count=3, duration_seconds=10.0), tmp_path)
    assert client.submitted == []


async def test_subscriber_trace_used_judge_not_novel_silent(
    tmp_path: Path, monkeypatch
):
    """trace_used set + novelty judge says not-novel → no submission.
    This is the rule (d) silent-emit branch."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", "trace-a")

    # Phase 5 stub already returns is_novel=False — but pin it
    # explicitly so this test still passes once Phase 6 swaps the
    # body for the real LLM call.
    async def _judge(**kw):
        return st_novelty.NoveltyVerdict(is_novel=False)

    monkeypatch.setattr(st_novelty, "judge_session_novelty", _judge)

    client = _RecordingClient()
    sub = _build_subscriber(bus=_StubBus(), profile_home=tmp_path, client=client)
    await sub._run_pipeline(SessionEndEvent(session_id="sid", turn_count=3, duration_seconds=10.0), tmp_path)

    assert client.submitted == []


async def test_subscriber_trace_used_judge_novel_continues_to_distill(
    tmp_path: Path, monkeypatch
):
    """trace_used set + judge says novel → distill is called. Distiller
    Phase 5 stub returns None so no submission lands, but the call
    chain is exercised."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", "trace-a")

    async def _judge(**kw):
        return st_novelty.NoveltyVerdict(is_novel=True, reason="found-edge-case")

    distill_calls: list[str] = []

    async def _distill(*, session_id, profile_home, submitter_hash, **_kw):
        distill_calls.append(session_id)
        return None  # Phase 5 stub behavior

    monkeypatch.setattr(st_novelty, "judge_session_novelty", _judge)
    monkeypatch.setattr(st_distiller, "distill_session", _distill)

    client = _RecordingClient()
    sub = _build_subscriber(bus=_StubBus(), profile_home=tmp_path, client=client)
    await sub._run_pipeline(SessionEndEvent(session_id="sid", turn_count=3, duration_seconds=10.0), tmp_path)

    assert distill_calls == ["sid"]
    assert client.submitted == []  # distiller stub returned None


async def test_subscriber_no_trace_used_skips_judge_and_distills(
    tmp_path: Path, monkeypatch
):
    """No trace used (BEFORE_TASK ran but inbox was empty) → judge
    NOT called; distill called directly. Rule (d) binary-emit branch."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", None)  # known session, no hits

    judge_calls = []

    async def _judge(**kw):
        judge_calls.append(1)
        return st_novelty.NoveltyVerdict(is_novel=True)

    distill_calls: list[str] = []

    async def _distill(*, session_id, profile_home, submitter_hash, **_kw):
        distill_calls.append(session_id)
        return None

    monkeypatch.setattr(st_novelty, "judge_session_novelty", _judge)
    monkeypatch.setattr(st_distiller, "distill_session", _distill)

    client = _RecordingClient()
    sub = _build_subscriber(bus=_StubBus(), profile_home=tmp_path, client=client)
    await sub._run_pipeline(SessionEndEvent(session_id="sid", turn_count=3, duration_seconds=10.0), tmp_path)

    assert judge_calls == []  # judge never called on no-trace path
    assert distill_calls == ["sid"]


async def test_subscriber_distill_proposal_triggers_submit(
    tmp_path: Path, monkeypatch
):
    """When the distiller returns a real TraceCard (Phase 7 behavior
    simulated here), the subscriber must submit it and log the
    receipt."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", None)

    proposal = _make_card(intent="distilled", trace_id="proposal-1")

    async def _distill(*, session_id, profile_home, submitter_hash, **_kw):
        return proposal

    monkeypatch.setattr(st_distiller, "distill_session", _distill)

    client = _RecordingClient()
    sub = _build_subscriber(bus=_StubBus(), profile_home=tmp_path, client=client)
    await sub._run_pipeline(SessionEndEvent(session_id="sid", turn_count=3, duration_seconds=10.0), tmp_path)

    assert len(client.submitted) == 1
    assert client.submitted[0].intent == "distilled"


async def test_subscriber_submit_rejected_doesnt_raise(
    tmp_path: Path, monkeypatch
):
    """A rejected submission must be logged and tolerated — Phase 9
    will queue it for retry. Phase 5 just validates the exception
    isolation."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", None)

    async def _distill(*, session_id, profile_home, submitter_hash, **_kw):
        return _make_card(intent="rejected", trace_id="r-1")

    monkeypatch.setattr(st_distiller, "distill_session", _distill)

    client = _RecordingClient()
    client.next_receipt = SubmitReceipt(accepted=False, reason="rate limit")

    sub = _build_subscriber(bus=_StubBus(), profile_home=tmp_path, client=client)
    # Should not raise.
    await sub._run_pipeline(SessionEndEvent(session_id="sid", turn_count=3, duration_seconds=10.0), tmp_path)
    assert len(client.submitted) == 1


async def test_subscriber_distiller_raises_isolated(tmp_path: Path, monkeypatch):
    """Distiller exceptions must not propagate — fire-and-forget
    contract."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", None)

    async def _boom(**kw):
        raise RuntimeError("distiller exploded")

    monkeypatch.setattr(st_distiller, "distill_session", _boom)

    client = _RecordingClient()
    sub = _build_subscriber(bus=_StubBus(), profile_home=tmp_path, client=client)
    # Should not raise.
    await sub._run_pipeline(SessionEndEvent(session_id="sid", turn_count=3, duration_seconds=10.0), tmp_path)
    assert client.submitted == []


async def test_subscriber_pops_bridge_even_after_silent_emit(tmp_path: Path):
    """Critical memory-leak guard: every pipeline path must pop the
    bridge entry, even the silent-skip ones, so a daemon doesn't
    accumulate state forever."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", "trace-x")
    assert bridge.session_known("sid") is True

    sub = _build_subscriber(bus=_StubBus(), profile_home=tmp_path)
    await sub._run_pipeline(SessionEndEvent(session_id="sid", turn_count=3, duration_seconds=10.0), tmp_path)
    # Whatever the path, the bridge must be empty afterward.
    assert bridge.session_known("sid") is False


async def test_subscriber_judge_disabled_via_config_silent(
    tmp_path: Path, monkeypatch
):
    """When ``novelty_judge.enabled=False``, trace-used sessions
    collapse to silent emit (rule d → rule a) — judge isn't even
    called."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", "trace-a")

    judge_calls = []

    async def _judge(**kw):
        judge_calls.append(1)
        return st_novelty.NoveltyVerdict(is_novel=True)

    monkeypatch.setattr(st_novelty, "judge_session_novelty", _judge)

    cfg = dataclasses.replace(
        SocialTracesConfig(),
        novelty_judge=dataclasses.replace(
            SocialTracesConfig().novelty_judge, enabled=False
        ),
    )
    client = _RecordingClient()
    sub = _build_subscriber(
        bus=_StubBus(), profile_home=tmp_path, config=cfg, client=client
    )
    await sub._run_pipeline(SessionEndEvent(session_id="sid", turn_count=3, duration_seconds=10.0), tmp_path)

    assert judge_calls == []
    assert client.submitted == []
