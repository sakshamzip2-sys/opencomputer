"""Phase 4 tests for the real prefetch hook (replaces Phase 2 stub).

Coverage focus (matching ``docs/plans/social-traces-plugin.md`` §10
Phase 4 acceptance):

* ``tag_extractor.extract_tags_from_message`` produces sensible tag
  tuples — order-stable, deduped, stopword-filtered.
* ``prefetch.build_query`` round-trips a user message into intent +
  tags.
* ``prefetch.select_best_trace`` honours the relevance threshold —
  even the top trace skips injection if it doesn't clear the bar.
* ``prefetch.format_injection`` renders a TraceCard as the
  ``<trace>`` block we want the model to see.
* ``prefetch.on_before_task`` integration: with a seeded inbox, a
  matching user message produces a HookDecision that injects the
  trace AND sets ``runtime.custom["trace_used"]``. With a
  non-matching message, no injection.
* End-to-end through ``AgentLoop.run_conversation``: seed inbox, run
  agent with a fake provider, verify the ``<trace>`` block lands as
  a system-reminder user message.

The integration test is the load-bearing one — it proves the full
pre-task lookup path works locally without OpenHub existing.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from plugin_sdk.core import Message as _Message
from plugin_sdk.hooks import (
    HookContext,
    HookDecision,
    HookEvent,
    HookSpec,
)
from plugin_sdk.provider_contract import BaseProvider, ProviderResponse, Usage
from plugin_sdk.runtime_context import RuntimeContext
from plugin_sdk.traces import TraceCard, TraceMeta, TraceStep

# ─── alias bootstrap ────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    if "extensions.social_traces.tag_extractor" in sys.modules:
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
    for sub in ("state", "identity", "config", "tag_extractor", "prefetch", "subscriber"):
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

from extensions.social_traces import prefetch as st_prefetch  # noqa: E402
from extensions.social_traces import state as st_state  # noqa: E402
from extensions.social_traces.client.local_file import (  # noqa: E402
    INBOX_DIRNAME,
    trace_card_to_dict,
)
from extensions.social_traces.tag_extractor import (  # noqa: E402
    extract_tags_from_message,
)


# ─── helpers ──────────────────────────────────────────────────────────


def _make_card(
    *,
    intent: str,
    tags: tuple[str, ...],
    insight: str = "use the rsync --checksum flag for clock-skewed LANs.",
    trace_id: str = "trace-1",
    outcome: str = "success",
) -> TraceCard:
    return TraceCard(
        schema_version="v1",
        intent=intent,
        meta=TraceMeta(
            tags=tags,
            outcome=outcome,  # type: ignore[arg-type]
            token_cost=600,
            loop_count=2,
            harness_version="opencomputer/0.1.0",
            submitter_hash="0" * 64,
        ),
        steps=(
            TraceStep(
                tool_name="Bash",
                arguments_summary="rsync -avh --checksum src/ dst/",
                result_summary="0 errors",
                duration_ms=1500,
            ),
            TraceStep(
                tool_name="Read",
                arguments_summary="dst/manifest.txt",
                result_summary="42 entries",
                duration_ms=12,
            ),
        ),
        distilled_insight=insight,
        created_at="2026-05-05T12:00:00Z",
        id=trace_id,
    )


def _seed_inbox(profile_home: Path, card: TraceCard, *, name: str | None = None) -> Path:
    inbox = profile_home / "traces" / INBOX_DIRNAME
    inbox.mkdir(parents=True, exist_ok=True)
    stem = name or card.id or "seed"
    path = inbox / f"{stem}.json"
    path.write_text(json.dumps(trace_card_to_dict(card), indent=2), encoding="utf-8")
    return path


# ─── tag_extractor ───────────────────────────────────────────────────


def test_extract_tags_basic_message():
    tags = extract_tags_from_message("sync files between two computers on LAN")
    # Lowercased; "sync" filtered (3 chars), "between" stopword,
    # "two" filtered (3 chars), "lan" filtered (3 chars).
    assert "files" in tags
    assert "computers" in tags


def test_extract_tags_drops_stopwords_and_short_words():
    tags = extract_tags_from_message("Please help me with the homelab setup")
    assert "please" not in tags  # stopword
    assert "help" not in tags  # stopword
    assert "the" not in tags  # short
    assert "homelab" in tags
    assert "setup" in tags


def test_extract_tags_dedupes():
    tags = extract_tags_from_message("rsync rsync rsync once more rsync")
    assert tags.count("rsync") == 1


def test_extract_tags_strips_non_alphanumeric():
    """Punctuation, hyphens, apostrophes — all become spaces."""
    tags = extract_tags_from_message("LAN-based file/sync, on a homelab.")
    assert "homelab" in tags
    # "LAN" too short, "based" passes (5 chars, not stopword).
    assert "based" in tags


def test_extract_tags_empty_input():
    assert extract_tags_from_message("") == ()
    assert extract_tags_from_message("   ") == ()


def test_extract_tags_respects_max():
    long_text = "alpha beta gamma delta epsilon zeta theta lambda kappa rho"
    tags = extract_tags_from_message(long_text, max_tags=3)
    assert len(tags) == 3


def test_extract_tags_preserves_first_occurrence_order():
    tags = extract_tags_from_message("filesync homelab cluster filesync")
    # filesync appears first, then homelab, then cluster. filesync's
    # second occurrence is deduped.
    assert tags == ("filesync", "homelab", "cluster")


# ─── build_query ─────────────────────────────────────────────────────


def test_build_query_intent_is_user_message():
    intent, tags = st_prefetch.build_query("sync files on homelab")
    assert intent == "sync files on homelab"
    assert "homelab" in tags
    assert "files" in tags


def test_build_query_truncates_long_intent():
    long_msg = "x" * 1000
    intent, _ = st_prefetch.build_query(long_msg)
    assert len(intent) <= 500


def test_build_query_empty_input():
    intent, tags = st_prefetch.build_query("")
    assert intent == ""
    assert tags == ()


# ─── select_best_trace ───────────────────────────────────────────────


def test_select_best_trace_returns_top_when_threshold_cleared():
    import dataclasses

    a = dataclasses.replace(
        _make_card(intent="a", tags=("homelab",), trace_id="a"), score=2.0
    )
    b = dataclasses.replace(
        _make_card(intent="b", tags=("homelab",), trace_id="b"), score=1.5
    )
    chosen = st_prefetch.select_best_trace((a, b), threshold=1.0)
    assert chosen is a


def test_select_best_trace_returns_none_when_threshold_not_cleared():
    import dataclasses

    a = dataclasses.replace(
        _make_card(intent="a", tags=("homelab",), trace_id="a"), score=0.4
    )
    chosen = st_prefetch.select_best_trace((a,), threshold=0.6)
    assert chosen is None


def test_select_best_trace_empty_tuple():
    chosen = st_prefetch.select_best_trace((), threshold=0.0)
    assert chosen is None


def test_select_best_trace_treats_unscored_as_zero():
    """A trace without a server-stamped score must not slip past the
    threshold gate. Treat unscored as 0."""
    a = _make_card(intent="a", tags=("homelab",), trace_id="a")
    # No score stamp — None.
    chosen = st_prefetch.select_best_trace((a,), threshold=0.5)
    assert chosen is None


# ─── format_injection ────────────────────────────────────────────────


def test_format_injection_contains_trace_tag_and_metadata():
    card = _make_card(
        intent="sync files on LAN",
        tags=("homelab", "filesync", "lan"),
        trace_id="t-1",
    )
    body = st_prefetch.format_injection(card)
    assert "<trace " in body
    assert "</trace>" in body
    assert 'intent="sync files on LAN"' in body
    assert 'outcome="success"' in body
    assert "homelab" in body
    assert "filesync" in body
    assert "Insight: " in body


def test_format_injection_includes_steps_marked_as_reference():
    card = _make_card(intent="x", tags=("a",))
    body = st_prefetch.format_injection(card)
    assert "Steps used (reference only):" in body
    assert "Bash:" in body
    assert "rsync -avh" in body


def test_format_injection_truncates_oversize_intent():
    card = _make_card(intent="z" * 500, tags=("a",))
    body = st_prefetch.format_injection(card)
    # ``intent="..."`` line must not be longer than ~210 chars (200 +
    # surrounding markers + ellipsis).
    intent_line = next(line for line in body.splitlines() if line.startswith("<trace"))
    assert len(intent_line) < 350


def test_format_injection_handles_empty_steps():
    card = TraceCard(
        schema_version="v1",
        intent="no-steps trace",
        meta=TraceMeta(
            tags=("a",),
            outcome="success",
            token_cost=0,
            loop_count=0,
            harness_version="x",
            submitter_hash="0" * 64,
        ),
        steps=(),
        distilled_insight="just a thought",
        created_at="2026-05-05T12:00:00Z",
    )
    body = st_prefetch.format_injection(card)
    # No "Steps used" section when there are no steps.
    assert "Steps used" not in body
    assert "</trace>" in body


# ─── on_before_task — integration with the real local-file backend ──


async def test_on_before_task_disabled_returns_pass(tmp_path: Path):
    """Disabled flag → pass, no work, no heartbeat."""
    runtime = RuntimeContext(custom={"profile_home": str(tmp_path)})
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="sid",
        runtime=runtime,
        message=_Message(role="user", content="anything"),
    )
    decision = await st_prefetch.on_before_task(ctx)
    assert decision.decision == "pass"
    assert st_state.read_heartbeat(tmp_path) == 0.0


async def test_on_before_task_no_match_returns_pass(tmp_path: Path):
    """Enabled but inbox empty → pass, trace_used=None, heartbeat written."""
    st_state.set_enabled(tmp_path, True)
    custom: dict = {"profile_home": str(tmp_path)}
    runtime = RuntimeContext(custom=custom)
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="sid",
        runtime=runtime,
        message=_Message(role="user", content="some homelab task"),
    )
    decision = await st_prefetch.on_before_task(ctx)
    assert decision.decision == "pass"
    assert custom["trace_used"] is None
    assert st_state.read_heartbeat(tmp_path) > 0.0


async def test_on_before_task_match_returns_rewrite_with_trace_block(tmp_path: Path):
    """Seed a trace whose tags overlap with the user message — the
    handler must return decision=rewrite with the trace formatted as
    modified_message AND set runtime.custom['trace_used'] to the
    trace id."""
    st_state.set_enabled(tmp_path, True)
    _seed_inbox(
        tmp_path,
        _make_card(
            intent="sync files between two homelab boxes",
            tags=("homelab", "filesync"),
            trace_id="seeded-1",
        ),
    )

    custom: dict = {"profile_home": str(tmp_path)}
    runtime = RuntimeContext(custom=custom)
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="sid",
        runtime=runtime,
        message=_Message(
            role="user", content="i need to sync homelab boxes for filesync"
        ),
    )
    decision = await st_prefetch.on_before_task(ctx)

    assert decision.decision == "rewrite"
    assert "<trace " in decision.modified_message
    assert "homelab" in decision.modified_message
    assert "rsync" in decision.modified_message  # from the seeded steps
    assert custom["trace_used"] == "seeded-1"


async def test_on_before_task_threshold_blocks_weak_match(tmp_path: Path):
    """A trace that comes back with a low score must not get injected.

    Set the relevance threshold high in config.yaml and verify the
    handler skips even though a candidate was returned.
    """
    st_state.set_enabled(tmp_path, True)
    # High threshold (10) — far above what score_trace ever produces.
    (tmp_path / "config.yaml").write_text(
        "social_traces:\n"
        "  query:\n"
        "    relevance_threshold: 10.0\n",
        encoding="utf-8",
    )
    _seed_inbox(
        tmp_path,
        _make_card(
            intent="weak match",
            tags=("homelab",),
            trace_id="weak-1",
        ),
    )

    custom: dict = {"profile_home": str(tmp_path)}
    runtime = RuntimeContext(custom=custom)
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="sid",
        runtime=runtime,
        message=_Message(role="user", content="homelab work"),
    )
    decision = await st_prefetch.on_before_task(ctx)

    assert decision.decision == "pass"
    assert custom["trace_used"] is None


async def test_on_before_task_empty_user_message_returns_pass(tmp_path: Path):
    """A blank user message (slash-command-only path can deliver this)
    → no work, return pass."""
    st_state.set_enabled(tmp_path, True)
    runtime = RuntimeContext(custom={"profile_home": str(tmp_path)})
    ctx = HookContext(
        event=HookEvent.BEFORE_TASK,
        session_id="sid",
        runtime=runtime,
        message=_Message(role="user", content="   "),
    )
    decision = await st_prefetch.on_before_task(ctx)
    assert decision.decision == "pass"


# ─── end-to-end integration through AgentLoop.run_conversation ──────


class _FakeProvider(BaseProvider):
    async def complete(
        self, *, model, messages, system=None, tools=None,
        max_tokens=None, temperature=None, **kw,
    ):
        return ProviderResponse(
            message=_Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=Usage(input_tokens=5, output_tokens=2),
        )

    async def stream_complete(self, **kw):  # pragma: no cover — unused
        yield  # type: ignore[misc]


def _build_loop(tmp_path: Path):
    from opencomputer.agent.config import Config, SessionConfig
    from opencomputer.agent.loop import AgentLoop
    from opencomputer.agent.state import SessionDB

    db_path = tmp_path / "sessions.db"
    cfg = Config(session=SessionConfig(db_path=db_path))
    return AgentLoop(
        provider=_FakeProvider(),
        config=cfg,
        db=SessionDB(db_path),
        compaction_disabled=True,
        episodic_disabled=True,
        reviewer_disabled=True,
    )


async def test_end_to_end_seeded_trace_lands_in_messages(tmp_path: Path):
    """The big one: with a seeded inbox + the real prefetch hook
    registered + the loop wired, running a conversation must produce
    a system-reminder user message containing the trace block, AND
    persist it to the DB so a resumed session sees it.

    This is the load-bearing demo for the local mid-task lookup —
    if this test ever fails, the headline feature is broken.
    """
    from opencomputer.hooks.engine import engine as global_engine
    from plugin_sdk.runtime_context import RuntimeContext as _RC

    profile_home = tmp_path / "profile"
    profile_home.mkdir()

    st_state.set_enabled(profile_home, True)
    _seed_inbox(
        profile_home,
        _make_card(
            intent="sync files between two homelab machines on LAN",
            tags=("homelab", "filesync", "rsync"),
            trace_id="end-to-end-1",
            insight="rsync --checksum is more reliable than --update on LAN.",
        ),
    )

    global_engine.unregister_all(HookEvent.BEFORE_TASK)
    global_engine.register(
        HookSpec(
            event=HookEvent.BEFORE_TASK,
            handler=st_prefetch.on_before_task,
            fire_and_forget=False,
            priority=20,
        )
    )

    try:
        runtime = _RC(custom={"profile_home": str(profile_home)})
        loop = _build_loop(tmp_path)
        result = await loop.run_conversation(
            "i want to sync files between my two homelab machines via rsync",
            session_id="end-to-end-sid",
            runtime=runtime,
        )

        # User-side messages: original prompt + injected system-reminder
        # (in that order).
        user_messages = [m for m in result.messages if m.role == "user"]
        assert len(user_messages) >= 2
        assert "homelab" in user_messages[0].content
        reminder = user_messages[1].content
        assert "<system-reminder>" in reminder
        assert "<trace " in reminder
        assert "rsync --checksum" in reminder  # from distilled_insight
        assert "end-to-end-1" not in reminder  # internal id never echoed

        # NOTE on trace_used: the prefetch handler sets
        # runtime.custom["trace_used"] = "end-to-end-1" on the loop's
        # INTERNAL RuntimeContext (the one created by the loop's
        # ``replace(self._runtime, custom={...})`` at loop.py:~775).
        # That mutation does NOT propagate back to the test's local
        # ``runtime`` variable above — they're different dicts after
        # the loop's replace() call. The unit test
        # test_on_before_task_match_returns_rewrite_with_trace_block
        # verifies the flag is set correctly via a direct call.
        #
        # This is a real architectural finding for Phase 5 — the
        # SessionEndEvent published at session-end strips the runtime
        # entirely, so the post-task subscriber can't read trace_used
        # from runtime.custom. Phase 5 will need a different bridge:
        # module-level dict keyed by session_id, SessionDB metadata
        # column, or a new trace_used field on SessionEndEvent.

        # Persist check — resumed session sees the same context.
        from_db = loop.db.get_messages("end-to-end-sid")
        db_user_contents = [m.content for m in from_db if m.role == "user"]
        assert any("<system-reminder>" in c and "<trace " in c for c in db_user_contents)
    finally:
        global_engine.unregister_all(HookEvent.BEFORE_TASK)


async def test_end_to_end_no_match_no_injection(tmp_path: Path):
    """Mirror test: when the inbox has no matching trace, the agent
    completes normally with no injection and trace_used stays None."""
    from opencomputer.hooks.engine import engine as global_engine
    from plugin_sdk.runtime_context import RuntimeContext as _RC

    profile_home = tmp_path / "profile"
    profile_home.mkdir()

    st_state.set_enabled(profile_home, True)
    _seed_inbox(
        profile_home,
        _make_card(
            intent="provision EC2 instance",
            tags=("aws", "ec2", "cloud"),
            trace_id="cloud-trace",
        ),
    )

    global_engine.unregister_all(HookEvent.BEFORE_TASK)
    global_engine.register(
        HookSpec(
            event=HookEvent.BEFORE_TASK,
            handler=st_prefetch.on_before_task,
            fire_and_forget=False,
        )
    )

    try:
        runtime = _RC(custom={"profile_home": str(profile_home)})
        loop = _build_loop(tmp_path)
        result = await loop.run_conversation(
            "help me debug my homelab nas",
            session_id="no-match-sid",
            runtime=runtime,
        )

        user_messages = [m for m in result.messages if m.role == "user"]
        # Only the original message — no system-reminder injected.
        assert len(user_messages) == 1
        assert not any("<trace " in (m.content or "") for m in result.messages)
        assert runtime.custom.get("trace_used") is None
    finally:
        global_engine.unregister_all(HookEvent.BEFORE_TASK)
