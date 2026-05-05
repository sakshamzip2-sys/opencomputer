"""Phase 6 tests for the real novelty judge LLM call.

Coverage focus (matching ``docs/plans/social-traces-plugin.md`` §10
Phase 6 acceptance):

* JSON parser tolerates markdown fences + prose around the JSON.
* Provider returning ``is_novel=true`` produces an emit-continuing
  verdict; ``is_novel=false`` (or any failure) produces a silent
  verdict.
* No-provider degradation: returns ``is_novel=False`` without
  attempting any LLM call.
* Cost-guard pre-flight denies → judge skips the provider call;
  cost-guard ``record_usage`` is called after a successful response.
* Provider raises → silent (``reason=provider-error``).
* Subscriber threads provider + cost_guard from constructor through
  the judge call; pulls the trace card body from the bridge.
* Subscriber reads transcript from SessionDB at session-end time.
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
    if "extensions.social_traces.novelty_judge" in sys.modules:
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


_ensure_alias()

from extensions.social_traces import novelty_judge as nj  # noqa: E402
from extensions.social_traces import session_state as bridge  # noqa: E402
from extensions.social_traces import state as st_state  # noqa: E402
from extensions.social_traces import subscriber as st_sub  # noqa: E402
from extensions.social_traces.config import SocialTracesConfig  # noqa: E402
from plugin_sdk.core import Message  # noqa: E402
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
    TraceMeta,
    TraceNetworkClient,
    TraceStep,
)


# ─── helpers ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_bridge():
    bridge.reset_for_testing()
    yield
    bridge.reset_for_testing()


def _make_card(intent: str = "x", insight: str = "y", trace_id: str = "t1") -> TraceCard:
    return TraceCard(
        schema_version="v1",
        intent=intent,
        meta=TraceMeta(
            tags=("a",),
            outcome="success",
            token_cost=10,
            loop_count=1,
            harness_version="0",
            submitter_hash="0" * 64,
        ),
        steps=(),
        distilled_insight=insight,
        created_at="2026-05-05T12:00:00Z",
        id=trace_id,
    )


class _FakeProvider(BaseProvider):
    """Provider that returns a configurable assistant response."""

    def __init__(self, *, content: str = '{"novel": false, "confidence": 80}'):
        self.content = content
        self.calls: list[dict] = []

    async def complete(self, **kw):
        self.calls.append(kw)
        return ProviderResponse(
            message=Message(role="assistant", content=self.content),
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=20),
        )

    async def stream_complete(self, **kw):  # pragma: no cover — unused
        yield


class _FakeCostGuard:
    """Records check_budget + record_usage interactions."""

    def __init__(self, *, allowed: bool = True):
        self.allowed = allowed
        self.checks: list[dict] = []
        self.usages: list[dict] = []

    def check_budget(self, provider, projected_cost_usd):
        self.checks.append(
            {"provider": provider, "projected": projected_cost_usd}
        )
        return self.allowed

    def record_usage(self, provider, *, cost_usd, operation=None):
        self.usages.append(
            {"provider": provider, "cost_usd": cost_usd, "operation": operation}
        )


# ─── parser tolerance ────────────────────────────────────────────────


def test_parser_accepts_bare_json():
    text = '{"novel": true, "confidence": 75, "reason": "improved"}'
    verdict = nj._parse_judge_response(text)
    assert verdict is not None
    assert verdict.is_novel is True
    assert verdict.confidence == 75
    assert verdict.reason == "improved"


def test_parser_strips_markdown_fences():
    text = '```json\n{"novel": false, "confidence": 50, "reason": "no"}\n```'
    verdict = nj._parse_judge_response(text)
    assert verdict is not None
    assert verdict.is_novel is False
    assert verdict.confidence == 50


def test_parser_handles_surrounding_prose():
    text = (
        "Sure, here's my judgement:\n"
        '{"novel": true, "confidence": 90, "reason": "edge case"}'
        "\nLet me know if you want more detail."
    )
    verdict = nj._parse_judge_response(text)
    assert verdict is not None and verdict.is_novel is True


def test_parser_rejects_no_json():
    assert nj._parse_judge_response("") is None
    assert nj._parse_judge_response("just words, no braces") is None


def test_parser_rejects_malformed_json():
    assert nj._parse_judge_response("{not: valid json,}") is None


def test_parser_clamps_out_of_range_confidence():
    # >100 clamps down
    v = nj._parse_judge_response('{"novel": true, "confidence": 999}')
    assert v is not None and v.confidence == 100
    # negative clamps up
    v = nj._parse_judge_response('{"novel": true, "confidence": -5}')
    assert v is not None and v.confidence == 0


def test_parser_defaults_missing_fields():
    """A minimal response with just ``novel`` should still parse —
    confidence defaults to 0, reason defaults to empty."""
    v = nj._parse_judge_response('{"novel": true}')
    assert v is not None
    assert v.is_novel is True
    assert v.confidence == 0
    assert v.reason == ""


# ─── public judge API ────────────────────────────────────────────────


async def test_judge_no_provider_returns_not_novel():
    verdict = await nj.judge_session_novelty(
        user_message="x",
        transcript="x",
        used_trace_intent="x",
        used_trace_insight="x",
        provider=None,
    )
    assert verdict.is_novel is False
    assert verdict.reason == "no-provider"


async def test_judge_provider_says_novel():
    provider = _FakeProvider(
        content='{"novel": true, "confidence": 85, "reason": "edge case"}'
    )
    verdict = await nj.judge_session_novelty(
        user_message="sync",
        transcript="[user] sync\n[assistant] done",
        used_trace_intent="trace intent",
        used_trace_insight="trace insight",
        provider=provider,
    )
    assert verdict.is_novel is True
    assert verdict.confidence == 85
    assert verdict.reason == "edge case"
    # Provider was called once.
    assert len(provider.calls) == 1


async def test_judge_provider_says_not_novel():
    provider = _FakeProvider(content='{"novel": false, "confidence": 90}')
    verdict = await nj.judge_session_novelty(
        user_message="x", transcript="x",
        used_trace_intent="x", used_trace_insight="x",
        provider=provider,
    )
    assert verdict.is_novel is False


async def test_judge_cost_guard_denies_skips_provider():
    provider = _FakeProvider(content='{"novel": true}')
    guard = _FakeCostGuard(allowed=False)
    verdict = await nj.judge_session_novelty(
        user_message="x", transcript="x",
        used_trace_intent="x", used_trace_insight="x",
        provider=provider,
        cost_guard=guard,
    )
    assert verdict.is_novel is False
    assert verdict.reason == "budget-denied"
    # Guard was checked, but provider was NOT called.
    assert len(guard.checks) == 1
    assert provider.calls == []


async def test_judge_cost_guard_records_usage_after_call():
    provider = _FakeProvider(content='{"novel": true, "confidence": 80}')
    guard = _FakeCostGuard(allowed=True)
    verdict = await nj.judge_session_novelty(
        user_message="x", transcript="x",
        used_trace_intent="x", used_trace_insight="x",
        provider=provider,
        cost_guard=guard,
    )
    assert verdict.is_novel is True
    # check_budget was called; record_usage was called after.
    assert len(guard.checks) == 1
    assert len(guard.usages) == 1
    assert guard.usages[0]["provider"] == "anthropic"


async def test_judge_provider_raises_returns_not_novel():
    class _Boom(BaseProvider):
        async def complete(self, **kw):
            raise RuntimeError("provider down")

        async def stream_complete(self, **kw):  # pragma: no cover
            yield

    verdict = await nj.judge_session_novelty(
        user_message="x", transcript="x",
        used_trace_intent="x", used_trace_insight="x",
        provider=_Boom(),
    )
    assert verdict.is_novel is False
    assert verdict.reason == "provider-error"


async def test_judge_parse_failure_returns_not_novel():
    """Provider returns garbage that doesn't contain JSON → judge
    falls open to is_novel=False."""
    provider = _FakeProvider(content="I think it's pretty cool tbh")
    verdict = await nj.judge_session_novelty(
        user_message="x", transcript="x",
        used_trace_intent="x", used_trace_insight="x",
        provider=provider,
    )
    assert verdict.is_novel is False
    assert verdict.reason == "parse-failure"


async def test_judge_truncates_long_transcript():
    """Transcript over 4000 chars must be truncated before being sent
    to the provider — keeps Haiku context bounded."""
    provider = _FakeProvider(content='{"novel": false, "confidence": 50}')
    long_transcript = "x" * 10000
    await nj.judge_session_novelty(
        user_message="x",
        transcript=long_transcript,
        used_trace_intent="x", used_trace_insight="x",
        provider=provider,
    )
    # Inspect the user message that went to the provider.
    assert len(provider.calls) == 1
    sent_messages = provider.calls[0]["messages"]
    user_content = sent_messages[0].content
    assert "[truncated]" in user_content
    # The truncated transcript must be present but shorter than the
    # input.
    assert long_transcript not in user_content


def test_budget_allows_helper_accepts_bool_or_decision():
    assert nj._budget_allows(True) is True
    assert nj._budget_allows(False) is False

    class _Mock:
        def __init__(self, allowed):
            self.allowed = allowed

    assert nj._budget_allows(_Mock(True)) is True
    assert nj._budget_allows(_Mock(False)) is False


# ─── subscriber wires provider + cost_guard ──────────────────────────


class _RecordingClient(TraceNetworkClient):
    def __init__(self):
        self.submitted: list[TraceCard] = []
        self.next_receipt = SubmitReceipt(accepted=True, queue_id="r-1")

    async def query(self, intent, tags, *, limit=3, timeout_s=1.0):
        return QueryResult()

    async def submit(self, card):
        self.submitted.append(card)
        return self.next_receipt

    async def health(self, *, timeout_s=1.0):
        return True


def _build_sub(
    *,
    profile_home: Path,
    provider=None,
    cost_guard=None,
    client=None,
) -> st_sub.TraceEmissionSubscriber:
    cfg = SocialTracesConfig()
    captured_client = client or _RecordingClient()

    class _Bus:
        def subscribe(self, _evt, _h):  # pragma: no cover — unused here
            class _Sub:
                def unsubscribe(self): ...
            return _Sub()

    return st_sub.TraceEmissionSubscriber(
        bus=_Bus(),
        profile_home_factory=lambda: profile_home,
        client_factory=lambda _ph, _cfg: captured_client,
        config_factory=lambda _ph: cfg,
        provider=provider,
        cost_guard=cost_guard,
    )


async def test_subscriber_passes_provider_to_judge(tmp_path: Path, monkeypatch):
    """Verify the subscriber threads its constructor-injected provider
    through to the novelty judge — including reading the trace card
    from the bridge and the transcript from SessionDB."""
    st_state.set_enabled(tmp_path, True)
    card = _make_card(intent="ti", insight="tin", trace_id="tid")
    bridge.set_trace_used("sid", "tid", trace_card=card)

    captured: dict[str, Any] = {}

    async def _fake_judge(**kw):
        captured.update(kw)
        return nj.NoveltyVerdict(is_novel=False)

    monkeypatch.setattr(nj, "judge_session_novelty", _fake_judge)

    provider = _FakeProvider()
    guard = _FakeCostGuard()
    sub = _build_sub(
        profile_home=tmp_path, provider=provider, cost_guard=guard,
    )
    await sub._run_pipeline(SessionEndEvent(session_id="sid"), tmp_path)

    # Judge was called with the trace card body + the constructor-
    # injected provider + cost_guard.
    assert captured["used_trace_intent"] == "ti"
    assert captured["used_trace_insight"] == "tin"
    assert captured["provider"] is provider
    assert captured["cost_guard"] is guard


async def test_subscriber_reads_session_transcript_from_db(
    tmp_path: Path, monkeypatch
):
    """When the subscriber's _read_session_for_judge runs, it pulls
    the user message + transcript from SessionDB. Seed a real
    SessionDB row and verify the judge sees the right inputs."""
    st_state.set_enabled(tmp_path, True)
    card = _make_card(trace_id="tid")
    bridge.set_trace_used("sid-tx", "tid", trace_card=card)

    # Seed SessionDB with a small conversation. The DB path is
    # <profile_home>/sessions.db (matches subscriber's lookup).
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "sessions.db")
    db.create_session("sid-tx", platform="cli", model="x")
    db.append_message(
        "sid-tx", Message(role="user", content="please help me sync files")
    )
    db.append_message(
        "sid-tx", Message(role="assistant", content="sure, run rsync")
    )
    db.append_message(
        "sid-tx",
        Message(
            role="user",
            content="<system-reminder>injected trace</system-reminder>",
        ),
    )

    captured: dict[str, Any] = {}

    async def _fake_judge(**kw):
        captured.update(kw)
        return nj.NoveltyVerdict(is_novel=False)

    monkeypatch.setattr(nj, "judge_session_novelty", _fake_judge)

    sub = _build_sub(profile_home=tmp_path, provider=_FakeProvider())
    await sub._run_pipeline(SessionEndEvent(session_id="sid-tx"), tmp_path)

    assert captured["user_message"] == "please help me sync files"
    # Assistant message in transcript
    assert "[assistant]" in captured["transcript"]
    assert "sure, run rsync" in captured["transcript"]
    # System-reminder (our own injection) is filtered out.
    assert "<system-reminder>" not in captured["transcript"]


async def test_subscriber_judge_novel_continues_to_distill(
    tmp_path: Path, monkeypatch
):
    """When the real judge says is_novel=True, the subscriber proceeds
    to distill (the existing Phase 5 stub returns None, so no
    submission lands — but the call chain is exercised)."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", "tid", trace_card=_make_card())

    async def _judge_says_novel(**kw):
        return nj.NoveltyVerdict(is_novel=True, confidence=90, reason="x")

    distill_calls = []

    async def _distill(**kw):
        distill_calls.append(kw["session_id"])
        return None

    monkeypatch.setattr(nj, "judge_session_novelty", _judge_says_novel)
    from extensions.social_traces import distiller as st_distiller

    monkeypatch.setattr(st_distiller, "distill_session", _distill)

    sub = _build_sub(profile_home=tmp_path, provider=_FakeProvider())
    await sub._run_pipeline(SessionEndEvent(session_id="sid"), tmp_path)

    assert distill_calls == ["sid"]


async def test_subscriber_no_provider_judge_short_circuits(
    tmp_path: Path, monkeypatch
):
    """Without a provider, the judge degrades to is_novel=False — the
    subscriber treats that as silent (no distill)."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid", "tid", trace_card=_make_card())

    distill_calls = []

    async def _distill(**kw):
        distill_calls.append(kw["session_id"])
        return None

    from extensions.social_traces import distiller as st_distiller

    monkeypatch.setattr(st_distiller, "distill_session", _distill)

    # provider=None → degrades
    sub = _build_sub(profile_home=tmp_path, provider=None)
    await sub._run_pipeline(SessionEndEvent(session_id="sid"), tmp_path)

    # Distiller never invoked because the trace-was-used branch went
    # silent (is_novel=False).
    assert distill_calls == []


# ─── prefetch writes the card to the bridge ─────────────────────────


async def test_prefetch_match_writes_card_to_bridge(tmp_path: Path):
    """Phase 6 contract: prefetch writes the full TraceCard to the
    bridge so the post-task judge has access without re-querying."""
    from plugin_sdk.hooks import HookContext, HookEvent
    from plugin_sdk.runtime_context import RuntimeContext

    from extensions.social_traces import prefetch as st_prefetch
    from extensions.social_traces.client.local_file import trace_card_to_dict

    st_state.set_enabled(tmp_path, True)
    inbox = tmp_path / "traces" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    import json

    seeded = TraceCard(
        schema_version="v1",
        intent="sync homelab files",
        meta=TraceMeta(
            tags=("homelab", "filesync"),
            outcome="success",
            token_cost=200,
            loop_count=1,
            harness_version="0.1",
            submitter_hash="0" * 64,
        ),
        steps=(
            TraceStep(
                tool_name="Bash",
                arguments_summary="rsync ...",
                result_summary="ok",
                duration_ms=10,
            ),
        ),
        distilled_insight="rsync --checksum is reliable on LAN",
        created_at="2026-05-05T12:00:00Z",
        id="bridge-card-1",
    )
    (inbox / "bridge-card-1.json").write_text(
        json.dumps(trace_card_to_dict(seeded)), encoding="utf-8"
    )

    runtime = RuntimeContext(custom={"profile_home": str(tmp_path)})
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="bridge-sid",
        runtime=runtime,
        message=Message(role="user", content="i need homelab filesync help"),
    )

    await st_prefetch.on_before_task(ctx)

    # Bridge entry should now have BOTH the id AND the card body.
    entry = bridge.pop_session("bridge-sid")
    assert entry is not None
    assert entry.trace_used == "bridge-card-1"
    assert entry.trace_card is not None
    assert entry.trace_card.intent == "sync homelab files"
    assert entry.trace_card.distilled_insight == "rsync --checksum is reliable on LAN"
