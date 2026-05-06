"""Tests for the post-Phase-9.A dogfood fixes:

1. Subscriber pipeline concurrency cap (semaphore).
2. ``rotate_agent_id`` regenerates the on-disk id deterministically.
3. ``oc traces rotate-id`` CLI round-trip.
4. ``oc traces dry-run --no-llm`` structural path.
5. Status command surfaces wiring + configured-provider diagnostics.

These guard against accidental regressions in things that aren't on
the happy path of the existing phase tests.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

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


from extensions.social_traces import identity as st_identity  # noqa: E402
from extensions.social_traces import plugin as st_plugin  # noqa: E402
from extensions.social_traces import session_state as bridge  # noqa: E402
from extensions.social_traces import state as st_state  # noqa: E402
from extensions.social_traces import subscriber as st_sub  # noqa: E402

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
    TraceNetworkClient,
)


@pytest.fixture(autouse=True)
def _isolate_state():
    bridge.reset_for_testing()
    st_plugin.stop_subscriber()
    yield
    st_plugin.stop_subscriber()
    bridge.reset_for_testing()


# ─── rotate_agent_id ─────────────────────────────────────────────────


def test_rotate_agent_id_creates_when_missing(tmp_path: Path):
    """No prior id → old_id empty, new_id present and persisted."""
    old, new = st_identity.rotate_agent_id(tmp_path)
    assert old == ""
    assert len(new) == st_identity.AGENT_ID_BYTES * 2  # hex-encoded
    assert st_identity.agent_id_path(tmp_path).read_text(encoding="utf-8").strip() == new


def test_rotate_agent_id_replaces_existing(tmp_path: Path):
    """Prior id present → old_id returned, new_id different + persisted."""
    first = st_identity.get_or_create_agent_id(tmp_path)
    old, new = st_identity.rotate_agent_id(tmp_path)
    assert old == first
    assert new != first
    assert st_identity.agent_id_path(tmp_path).read_text(encoding="utf-8").strip() == new


def test_rotate_agent_id_then_get_returns_new(tmp_path: Path):
    """After rotate, ``get_or_create_agent_id`` returns the new id (not regenerates)."""
    st_identity.get_or_create_agent_id(tmp_path)
    _, new = st_identity.rotate_agent_id(tmp_path)
    assert st_identity.get_or_create_agent_id(tmp_path) == new


# ─── CLI: rotate-id, dry-run, status ─────────────────────────────────


@pytest.fixture
def cli_runner_with_profile(tmp_path: Path, monkeypatch):
    """CliRunner that sees ``tmp_path`` as the active profile home."""
    monkeypatch.setenv("OPENCOMPUTER_PROFILE_HOME", str(tmp_path))
    from opencomputer.cli_traces import app as traces_app

    return CliRunner(), traces_app, tmp_path


def test_cli_rotate_id_creates_when_missing(cli_runner_with_profile):
    runner, app, profile_home = cli_runner_with_profile
    result = runner.invoke(app, ["rotate-id", "--yes"])
    assert result.exit_code == 0, result.output
    assert "generated new agent_id" in result.output
    assert st_identity.agent_id_path(profile_home).exists()


def test_cli_rotate_id_replaces_existing(cli_runner_with_profile):
    runner, app, profile_home = cli_runner_with_profile
    first = st_identity.get_or_create_agent_id(profile_home)
    result = runner.invoke(app, ["rotate-id", "--yes"])
    assert result.exit_code == 0, result.output
    assert "rotated" in result.output
    new = st_identity.agent_id_path(profile_home).read_text(encoding="utf-8").strip()
    assert new != first


def test_cli_rotate_id_aborts_on_no_confirm(cli_runner_with_profile):
    """Without ``--yes`` and rejecting the prompt, exits with code 1
    and leaves the existing id alone."""
    runner, app, profile_home = cli_runner_with_profile
    first = st_identity.get_or_create_agent_id(profile_home)
    result = runner.invoke(app, ["rotate-id"], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.output
    assert st_identity.agent_id_path(profile_home).read_text(encoding="utf-8").strip() == first


def test_cli_status_shows_subscriber_unwired_when_no_process(cli_runner_with_profile):
    """A fresh CLI invocation has no wired subscriber; status must
    say so explicitly so the operator isn't left wondering."""
    runner, app, profile_home = cli_runner_with_profile
    st_state.set_enabled(profile_home, True)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "subscriber: not wired" in result.output


def test_cli_dry_run_no_llm_reports_redactions(cli_runner_with_profile):
    """The structure-only dry-run path reads SessionDB, runs redactor,
    prints a summary. No provider needed."""
    runner, app, profile_home = cli_runner_with_profile

    # Seed a session with a message that contains a redactable
    # string (a fake API key).
    from opencomputer.agent.state import SessionDB

    db = SessionDB(profile_home / "sessions.db")
    db.create_session("sid-test", platform="test")
    db.append_message(
        "sid-test",
        Message(role="user", content="my key is sk-FAKEKEY1234567890ABCDEFGHIJ and please dont leak"),
    )
    db.append_message(
        "sid-test",
        Message(role="assistant", content="okay i wont"),
    )

    result = runner.invoke(app, ["dry-run", "sid-test", "--no-llm"])
    assert result.exit_code == 0, result.output
    assert "messages: 2" in result.output
    assert "messages with redactions:" in result.output


def test_cli_dry_run_no_llm_handles_missing_session(cli_runner_with_profile):
    """Asking to dry-run a session that doesn't exist returns a clean
    informational message, not a crash."""
    runner, app, _profile_home = cli_runner_with_profile
    # Need a SessionDB to exist for the path to even open it. Create
    # an empty db.
    from opencomputer.agent.state import SessionDB

    SessionDB(_profile_home / "sessions.db")  # creates schema

    result = runner.invoke(app, ["dry-run", "nonexistent-sid", "--no-llm"])
    # get_messages returns [] for unknown id, so we hit the "no messages"
    # path — exit code 0 with explanatory output.
    assert result.exit_code == 0, result.output
    assert "no messages" in result.output


def test_cli_audit_redactor_writes_report(cli_runner_with_profile):
    """audit-redactor sweeps recent sessions and writes a report file."""
    runner, app, profile_home = cli_runner_with_profile

    from opencomputer.agent.state import SessionDB

    db = SessionDB(profile_home / "sessions.db")
    db.create_session("sid-audit", platform="test")
    db.append_message(
        "sid-audit",
        Message(role="user", content="ping me at user@example.com — totally legit"),
    )

    out = profile_home / "audit-out.txt"
    result = runner.invoke(app, ["audit-redactor", "-n", "5", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "session=sid-audit" in body
    # The email should appear in BEFORE and be redacted in AFTER.
    assert "user@example.com" in body
    assert "<redacted-pii>" in body


# ─── subscriber semaphore (concurrency cap) ──────────────────────────


class _FakeProvider(BaseProvider):
    async def complete(self, **_kw):
        return ProviderResponse(
            message=Message(role="assistant", content='{"novel": false}'),
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


def _build_sub(profile_home: Path):
    from extensions.social_traces.config import SocialTracesConfig

    cfg = SocialTracesConfig()

    class _Bus:
        def subscribe(self, _e, _h):
            class _S:
                def unsubscribe(self): ...
            return _S()

    return st_sub.TraceEmissionSubscriber(
        bus=_Bus(),
        profile_home_factory=lambda: profile_home,
        client_factory=lambda _ph, _cfg: _RecordingClient(),
        config_factory=lambda _ph: cfg,
        provider=_FakeProvider(),
        cost_guard=_FakeCostGuard(),
    )


def test_pipeline_semaphore_caps_concurrency(tmp_path: Path, monkeypatch):
    """Even when many session_end pipelines fire at once, no more
    than ``_MAX_CONCURRENT_PIPELINES`` distillers run simultaneously.

    We block inside ``distill_session`` until released, observe the
    peak in-flight count, and assert the cap held.
    """
    st_state.set_enabled(tmp_path, True)

    from extensions.social_traces import distiller as st_distiller

    in_flight = 0
    peak = 0
    gate = asyncio.Event()

    async def _slow_distill(*, session_id, **_kw):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            # Wait until the test releases us. This forces concurrent
            # callers to pile up at the semaphore — without the cap,
            # peak would equal the number of pipelines launched.
            await gate.wait()
        finally:
            in_flight -= 1
        return None

    monkeypatch.setattr(st_distiller, "distill_session", _slow_distill)

    sub = _build_sub(tmp_path)

    async def _drive():
        # Tower of N pipelines. Each pre-populates the bridge so the
        # "untracked session" early-return doesn't bypass distill.
        n = 5
        for i in range(n):
            bridge.set_trace_used(f"sid-{i}", None)

        events = [
            SessionEndEvent(
                session_id=f"sid-{i}", turn_count=4, duration_seconds=10.0
            )
            for i in range(n)
        ]
        tasks = [
            asyncio.create_task(sub._run_pipeline(e, tmp_path)) for e in events
        ]

        # Give the runtime a moment to schedule + park N pipelines.
        await asyncio.sleep(0.1)
        # Now release: every distiller returns None, pipelines drain.
        gate.set()
        await asyncio.gather(*tasks)

    asyncio.run(_drive())
    # 5 pipelines launched; cap is 2 → peak must be ≤ 2.
    assert peak <= st_sub._MAX_CONCURRENT_PIPELINES, (
        f"semaphore failed to cap concurrency: peak={peak}"
    )
    assert peak >= 1  # sanity — something ran


def test_pipeline_run_pipeline_still_callable_directly(tmp_path: Path, monkeypatch):
    """Direct ``await sub._run_pipeline(...)`` — the path used by
    every prior phase test — still works after the semaphore split."""
    st_state.set_enabled(tmp_path, True)
    bridge.set_trace_used("sid-direct", None)

    from extensions.social_traces import distiller as st_distiller

    calls: list[str] = []

    async def _distill(*, session_id, **_kw):
        calls.append(session_id)
        return None

    monkeypatch.setattr(st_distiller, "distill_session", _distill)

    sub = _build_sub(tmp_path)
    event = SessionEndEvent(session_id="sid-direct", turn_count=3, duration_seconds=10.0)
    asyncio.run(sub._run_pipeline(event, tmp_path))
    assert calls == ["sid-direct"]
