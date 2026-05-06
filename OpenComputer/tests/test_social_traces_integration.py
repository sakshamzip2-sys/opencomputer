"""Phase 11 — full prefetch + emission cycle against the local-file backend.

Pins the contract that, given a real ``LocalFileTraceNetworkClient``
(no stubs), a freshly-registered plugin can:

1. Read a planted ``inbox/`` TraceCard via the BEFORE_TASK hook and
   inject it as a ``modified_message`` into the agent's HookDecision.
2. Wire a subscriber whose post-task path lands a distilled TraceCard
   in ``outbox/`` on a real ``SessionEndEvent``.
3. Round-trip both directions on the same profile_home — the inbox
   the hook reads from and the outbox the subscriber writes to are
   the same on-disk layout the operator inspects via
   ``oc traces {inbox,outbox}``.

Also covers the small uncovered branches in ``plugin.py``:

* ``wire_subscriber()``'s ``_active_subscriber.stop()`` exception-swallow
  path (lines 149-150).
* ``stop_subscriber()``'s exception-swallow path (lines 189-190).
* ``_config_factory`` — both the no-config-file and malformed-yaml paths.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

# Alias bootstrap (mirrors phase9_wiring + cli_traces). Loads
# ``extensions.social_traces.*`` from the hyphenated dir without
# polluting sys.path.

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

    # client/ subpackage — the integration test exercises the real
    # local-file backend, so make_client must be importable.
    client_dir = _ST_DIR / "client"
    if "extensions.social_traces.client" not in sys.modules:
        client_init = client_dir / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "extensions.social_traces.client",
            str(client_init),
            submodule_search_locations=[str(client_dir)],
        )
        assert spec is not None and spec.loader is not None
        client_pkg = importlib.util.module_from_spec(spec)
        sys.modules["extensions.social_traces.client"] = client_pkg
        client_pkg.__package__ = "extensions.social_traces.client"
        spec.loader.exec_module(client_pkg)
        parent.client = client_pkg
    for sub in ("local_file",):
        full_name = f"extensions.social_traces.client.{sub}"
        if full_name in sys.modules:
            continue
        init = client_dir / f"{sub}.py"
        spec = importlib.util.spec_from_file_location(full_name, str(init))
        assert spec is not None and spec.loader is not None
        sub_mod = importlib.util.module_from_spec(spec)
        sub_mod.__package__ = "extensions.social_traces.client"
        sys.modules[full_name] = sub_mod
        spec.loader.exec_module(sub_mod)


_ensure_alias()

from extensions.social_traces import plugin as st_plugin  # noqa: E402
from extensions.social_traces import session_state as bridge  # noqa: E402
from extensions.social_traces import state as st_state  # noqa: E402
from extensions.social_traces.client import make_client  # noqa: E402

from plugin_sdk.core import Message  # noqa: E402
from plugin_sdk.hooks import HookContext, HookEvent  # noqa: E402
from plugin_sdk.ingestion import SessionEndEvent  # noqa: E402
from plugin_sdk.provider_contract import (  # noqa: E402
    BaseProvider,
    ProviderResponse,
    Usage,
)
from plugin_sdk.runtime_context import RuntimeContext  # noqa: E402
from plugin_sdk.traces import TraceCard, TraceMeta, TraceStep  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_state():
    bridge.reset_for_testing()
    st_plugin.stop_subscriber()
    yield
    st_plugin.stop_subscriber()
    bridge.reset_for_testing()


def _make_runtime_ctx(profile_home: Path) -> RuntimeContext:
    """Plant ``profile_home`` into ``runtime.custom`` so the prefetch
    handler finds it without falling back to env vars."""
    return RuntimeContext(custom={"profile_home": str(profile_home)})


def _plant_inbox_card(profile_home: Path, *, intent: str, tags: tuple[str, ...]) -> TraceCard:
    """Drop a TraceCard JSON into ``<profile_home>/traces/inbox/`` so a
    BEFORE_TASK query can find it."""
    inbox = profile_home / "traces" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    card = TraceCard(
        schema_version="1.0",
        intent=intent,
        meta=TraceMeta(
            tags=tags,
            outcome="success",
            token_cost=1500,
            loop_count=4,
            harness_version="opencomputer/integration-test",
            submitter_hash="0" * 32,
        ),
        steps=(
            TraceStep(
                tool_name="Read",
                arguments_summary="Read README.md",
                result_summary="Read 200 lines",
                duration_ms=42,
            ),
        ),
        distilled_insight=(
            "When asked about homelab setup, prefer reading the README "
            "before exploring the file tree. Saves several search calls."
        ),
        created_at="2026-05-07T00:00:00Z",
        id="trace-inbox-001",
        status="approved",
        score=0.91,
    )
    (inbox / "trace-inbox-001.json").write_text(
        json.dumps(asdict(card), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return card


# ─── 1. Full prefetch cycle against the real local-file backend ─────


async def test_before_task_hook_finds_planted_inbox_card_via_real_client(
    tmp_path: Path,
) -> None:
    """End-to-end prefetch: planted inbox card → real
    LocalFileTraceNetworkClient.query → real on_before_task →
    HookDecision.modified_message contains the distilled insight."""
    st_state.set_enabled(tmp_path, True)
    planted = _plant_inbox_card(
        tmp_path,
        intent="set up homelab dashboard",
        tags=("homelab", "dashboard"),
    )

    # Sanity — confirm the real local-file client can already see it.
    client = make_client(backend="local", profile_home=tmp_path, endpoint=None)
    sanity = await client.query(
        intent="homelab dashboard", tags=("homelab",), limit=3, timeout_s=1.0,
    )
    assert any(t.id == planted.id for t in sanity.traces), (
        "local-file backend must surface the planted card before the hook is even fired"
    )

    # Drive on_before_task with a HookContext that points at our profile.
    from extensions.social_traces import prefetch as st_prefetch

    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="session-int-1",
        message=Message(role="user", content="help me set up my homelab dashboard"),
        runtime=_make_runtime_ctx(tmp_path),
    )
    decision = await st_prefetch.on_before_task(ctx)

    # Hit path: prefetch returns decision="rewrite" with the injection
    # body in modified_message (per ``on_before_task``'s post-2026-04 wire
    # contract). Miss path returns decision="pass" with empty body —
    # would mean the planted card didn't match, which is a real bug.
    assert decision.decision == "rewrite", (
        f"planted card should produce a rewrite hit, got {decision.decision!r}"
    )
    assert "homelab" in decision.modified_message.lower(), (
        f"expected planted insight to be injected, got: {decision.modified_message!r}"
    )

    # Bridge must record which trace was used so the post-task subscriber
    # can run the novelty judge against it.
    assert bridge.peek_trace_used("session-int-1") == planted.id


# ─── 2. Full emission cycle: subscriber → real local-file outbox ────


class _FakeProvider(BaseProvider):
    async def complete(self, **_kw):
        return ProviderResponse(
            message=Message(role="assistant", content='{"novel": true}'),
            stop_reason="end_turn",
            usage=Usage(input_tokens=1, output_tokens=1),
        )

    async def stream_complete(self, **_kw):  # pragma: no cover
        yield


class _FakeCostGuard:
    def check_budget(self, *_a, **_kw):
        return True

    def record_usage(self, *_a, **_kw):
        return None


async def test_session_end_emits_trace_to_real_local_file_outbox(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end emission: SessionEndEvent → distill stub → real
    LocalFileTraceNetworkClient.submit → outbox JSON file on disk."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("session-int-2", None)

    # Stub distill_session so we don't depend on a real LLM. The
    # integration angle being tested is "wired subscriber + real
    # local-file backend", not the distillation prompts (covered
    # elsewhere).
    expected_card = TraceCard(
        schema_version="1.0",
        intent="emit a homelab dashboard trace",
        meta=TraceMeta(
            tags=("homelab",),
            outcome="success",
            token_cost=2000,
            loop_count=5,
            harness_version="opencomputer/integration-test",
            submitter_hash="f" * 32,
        ),
        steps=(
            TraceStep(
                tool_name="Edit",
                arguments_summary="Edit grafana.yaml",
                result_summary="Wrote dashboard config",
                duration_ms=120,
            ),
        ),
        distilled_insight="Configure Grafana datasource before adding panels.",
        created_at="2026-05-07T00:01:00Z",
    )

    async def _stub_distill(*, session_id, **_kw):
        return expected_card

    from extensions.social_traces import distiller as st_distiller
    from extensions.social_traces import subscriber as st_sub

    monkeypatch.setattr(st_distiller, "distill_session", _stub_distill)

    # Build subscriber against the SAME profile_home + REAL local-file
    # client, no stubs on the network surface.
    from extensions.social_traces.config import SocialTracesConfig

    real_client = make_client(backend="local", profile_home=tmp_path, endpoint=None)

    class _Bus:
        def subscribe(self, _e, _h):
            class _Sub:
                def unsubscribe(self):
                    return None

            return _Sub()

    sub = st_sub.TraceEmissionSubscriber(
        bus=_Bus(),
        profile_home_factory=lambda: tmp_path,
        client_factory=lambda _ph, _cfg: real_client,
        config_factory=lambda _ph: SocialTracesConfig(),
        provider=_FakeProvider(),
        cost_guard=_FakeCostGuard(),
    )

    event = SessionEndEvent(
        session_id="session-int-2", turn_count=4, duration_seconds=12.0,
    )
    await sub._run_pipeline(event, tmp_path)

    # Real outbox now has a JSON file matching the expected card.
    outbox = tmp_path / "traces" / "outbox"
    written = sorted(outbox.glob("*.json")) if outbox.exists() else []
    assert len(written) == 1, (
        f"expected exactly one trace in outbox, found: {[p.name for p in written]}"
    )
    raw = json.loads(written[0].read_text())
    assert raw["intent"] == expected_card.intent
    assert raw["distilled_insight"] == expected_card.distilled_insight


# ─── 3. plugin.register attaches BEFORE_TASK hook (acceptance smoke) ─


def test_register_attaches_before_task_hook_to_real_engine() -> None:
    """A real ``HookEngine`` instance picks up the registered handler
    and ``BEFORE_TASK`` is the only event registered."""
    from opencomputer.hooks.engine import HookEngine

    engine = HookEngine()

    class _StubAPI:
        def register_hook(self, spec):
            engine.register(spec)

    st_plugin.register(_StubAPI())

    assert len(engine._ordered_specs(HookEvent.BEFORE_TASK)) == 1
    # No subscriber side-effect from register() — wire_subscriber owns lifecycle.
    assert st_plugin.get_active_subscriber() is None


# ─── 4. Targeted coverage for plugin.py exception-swallow paths ─────


def test_wire_subscriber_swallows_prior_stop_exception(monkeypatch) -> None:
    """If the prior subscriber's ``stop()`` raises, ``wire_subscriber``
    must log + continue, not propagate (lines 149-150)."""
    sub1 = st_plugin.wire_subscriber(
        provider=_FakeProvider(), cost_guard=_FakeCostGuard()
    )

    def _raising_stop():
        raise RuntimeError("simulated subscriber.stop() failure")

    monkeypatch.setattr(sub1, "stop", _raising_stop)

    # Must not raise — the new subscriber takes over regardless.
    sub2 = st_plugin.wire_subscriber(
        provider=_FakeProvider(), cost_guard=_FakeCostGuard()
    )
    assert sub2 is not sub1
    assert st_plugin.get_active_subscriber() is sub2


def test_stop_subscriber_swallows_subscriber_stop_exception(monkeypatch) -> None:
    """``stop_subscriber()`` clears the singleton even when the inner
    ``stop()`` call raises (lines 189-190)."""
    sub1 = st_plugin.wire_subscriber(
        provider=_FakeProvider(), cost_guard=_FakeCostGuard()
    )

    def _raising_stop():
        raise RuntimeError("simulated subscriber.stop() failure on shutdown")

    monkeypatch.setattr(sub1, "stop", _raising_stop)

    st_plugin.stop_subscriber()  # must not raise
    assert st_plugin.get_active_subscriber() is None


# ─── 5. Targeted coverage for _config_factory paths ─────────────────


def test_config_factory_returns_defaults_when_no_config_yaml(tmp_path: Path) -> None:
    """``_config_factory`` short-circuits to defaults when ``config.yaml``
    is missing (line 73 short-circuit)."""
    cfg = st_plugin._config_factory(tmp_path)
    # SocialTracesConfig() default backend is "local" per design.
    assert cfg.backend == "local"


def test_config_factory_returns_defaults_on_malformed_yaml(tmp_path: Path) -> None:
    """``_config_factory`` swallows YAMLError and returns defaults
    (line 76-77 except branch)."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(": : not yaml :::", encoding="utf-8")

    cfg = st_plugin._config_factory(tmp_path)
    assert cfg.backend == "local"


def test_config_factory_reads_real_yaml(tmp_path: Path) -> None:
    """When a well-formed ``config.yaml`` declares a custom endpoint
    under ``social_traces:``, the factory threads it through
    ``from_config_dict`` so the subscriber sees the live values."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "social_traces:\n"
        "  backend: local\n"
        "  endpoint: https://example.invalid/openhub\n",
        encoding="utf-8",
    )

    cfg = st_plugin._config_factory(tmp_path)
    assert cfg.backend == "local"
    assert cfg.endpoint == "https://example.invalid/openhub"


# ─── 6. _client_factory exercises make_client end-to-end ────────────


def test_client_factory_constructs_local_file_client(tmp_path: Path) -> None:
    """``_client_factory`` resolves the configured backend to a real
    ``LocalFileTraceNetworkClient`` instance (lines 86-92)."""
    from extensions.social_traces.client.local_file import LocalFileTraceNetworkClient
    from extensions.social_traces.config import SocialTracesConfig

    cfg = SocialTracesConfig()  # default backend="local"
    client = st_plugin._client_factory(tmp_path, cfg)

    assert isinstance(client, LocalFileTraceNetworkClient)


# ─── 7. state.resolve_profile_home — every fallback path ────────────


def test_resolve_profile_home_uses_opencomputer_profile_home_env(
    monkeypatch, tmp_path: Path,
) -> None:
    """The most-explicit env override wins."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    assert st_state.resolve_profile_home() == tmp_path


def test_resolve_profile_home_falls_back_to_opencomputer_home(
    monkeypatch, tmp_path: Path,
) -> None:
    """When ``OPENCOMPUTER_PROFILE_HOME`` is unset, ``OPENCOMPUTER_HOME`` is
    consulted before defaulting to ``~/.opencomputer/``."""
    monkeypatch.delenv("OPENCOMPUTER_PROFILE_HOME", raising=False)
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))

    # Force the ContextVar lookup to return None so it falls through
    # to the OPENCOMPUTER_HOME branch.
    from plugin_sdk import profile_context

    token = profile_context.current_profile_home.set(None)
    try:
        assert st_state.resolve_profile_home() == tmp_path
    finally:
        profile_context.current_profile_home.reset(token)


def test_resolve_profile_home_default_is_dot_opencomputer(monkeypatch) -> None:
    """With no env vars and no ContextVar, the resolver defaults to
    ``~/.opencomputer/``."""
    monkeypatch.delenv("OPENCOMPUTER_PROFILE_HOME", raising=False)
    monkeypatch.delenv("OPENCOMPUTER_HOME", raising=False)

    from plugin_sdk import profile_context

    token = profile_context.current_profile_home.set(None)
    try:
        result = st_state.resolve_profile_home()
    finally:
        profile_context.current_profile_home.reset(token)

    assert result == Path.home() / ".opencomputer"


def test_resolve_profile_home_uses_profile_context_var(
    monkeypatch, tmp_path: Path,
) -> None:
    """When ``OPENCOMPUTER_PROFILE_HOME`` is unset but the ContextVar
    is populated (gateway/CLI request-scope wiring), the ContextVar
    value is returned."""
    monkeypatch.delenv("OPENCOMPUTER_PROFILE_HOME", raising=False)

    from plugin_sdk import profile_context

    token = profile_context.current_profile_home.set(tmp_path)
    try:
        assert st_state.resolve_profile_home() == tmp_path
    finally:
        profile_context.current_profile_home.reset(token)


# ─── 8. state.write_heartbeat OSError path ──────────────────────────


def test_write_heartbeat_swallows_oserror(monkeypatch, tmp_path: Path) -> None:
    """``write_heartbeat`` must never raise even when the underlying
    write fails (e.g. read-only filesystem) — line 126-127."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()

    real_write_text = Path.write_text

    def _fail_write(self, *_a, **_kw):
        # Only fail for the heartbeat path; let other Path.write_text
        # calls pass through so the test setup itself isn't broken.
        if self.name == "heartbeat":
            raise OSError("simulated read-only filesystem")
        return real_write_text(self, *_a, **_kw)

    monkeypatch.setattr(Path, "write_text", _fail_write)

    # Must not raise — best-effort contract.
    st_state.write_heartbeat(profile_home)
