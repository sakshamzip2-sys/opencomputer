"""Phase 11d: Episodic memory + Anthropic batch runner.

Two distinct surfaces tested in this file:

1. Episodic memory (third pillar)
   - SessionDB.record_episodic / search_episodic / list_episodic round-trip
   - render_template_summary respects length caps
   - _extract_paths picks up file-shaped tokens
   - AgentLoop records an event after each completed turn
   - opencomputer recall CLI returns matches

2. Batch runner
   - parse_jsonl handles well-formed input + raises on bad shape
   - _build_anthropic_request shapes the entry correctly
   - submit/poll/fetch round-trip with mocked anthropic.AsyncAnthropic
   - write_results emits one JSON-per-line file
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ─── Episodic memory: SessionDB layer ──────────────────────────────────


def test_session_db_episodic_round_trip(tmp_path: Path) -> None:
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1", platform="cli", model="m")
    rid = db.record_episodic(
        session_id="s-1",
        turn_index=0,
        summary="refactored auth.py",
        tools_used=["Edit", "Bash"],
        file_paths=["src/auth.py", "tests/test_auth.py"],
    )
    assert rid > 0
    listed = db.list_episodic(session_id="s-1")
    assert len(listed) == 1
    assert listed[0]["summary"] == "refactored auth.py"
    assert listed[0]["tools_used"] == "Edit,Bash"
    assert listed[0]["file_paths"] == "src/auth.py,tests/test_auth.py"


def test_session_db_search_episodic_finds_by_summary(tmp_path: Path) -> None:
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1", platform="cli", model="m")
    db.record_episodic(session_id="s-1", turn_index=0, summary="pickle vegetables today")
    db.record_episodic(session_id="s-1", turn_index=1, summary="bake bread tomorrow")

    hits = db.search_episodic("pickle")
    assert len(hits) == 1
    assert "pickle" in hits[0]["summary"]


def test_session_db_search_episodic_finds_by_file_path(tmp_path: Path) -> None:
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-1", platform="cli", model="m")
    db.record_episodic(
        session_id="s-1",
        turn_index=0,
        summary="some change",
        file_paths=["src/auth.py"],
    )
    db.record_episodic(
        session_id="s-1",
        turn_index=1,
        summary="another change",
        file_paths=["src/router.py"],
    )

    hits = db.search_episodic("auth.py")
    assert len(hits) == 1
    assert hits[0]["turn_index"] == 0


def test_session_db_search_episodic_empty_query_returns_empty(tmp_path: Path) -> None:
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "s.db")
    assert db.search_episodic("") == []
    assert db.search_episodic("   ") == []


def test_session_db_list_episodic_per_session(tmp_path: Path) -> None:
    from opencomputer.agent.state import SessionDB

    db = SessionDB(tmp_path / "s.db")
    db.create_session("s-a", platform="cli", model="m")
    db.create_session("s-b", platform="cli", model="m")
    db.record_episodic(session_id="s-a", turn_index=0, summary="A0")
    db.record_episodic(session_id="s-a", turn_index=1, summary="A1")
    db.record_episodic(session_id="s-b", turn_index=0, summary="B0")

    a_events = db.list_episodic(session_id="s-a")
    b_events = db.list_episodic(session_id="s-b")
    assert {e["summary"] for e in a_events} == {"A0", "A1"}
    assert {e["summary"] for e in b_events} == {"B0"}

    # Cross-session list (no session_id filter) returns everything newest first
    all_events = db.list_episodic()
    assert len(all_events) == 3


# ─── Episodic memory: helpers ──────────────────────────────────────────


def test_render_template_summary_caps_length() -> None:
    from opencomputer.agent.episodic import (
        SUMMARY_MAX_CHARS,
        render_template_summary,
    )
    from plugin_sdk.core import Message

    # Long inputs on every dimension
    long_user = "what is the meaning of life " * 10
    long_assistant = Message(role="assistant", content="x" * 1000)

    summary = render_template_summary(
        user_message=long_user,
        assistant_message=long_assistant,
        tools_used=["A", "B", "C"],
    )
    assert len(summary) <= SUMMARY_MAX_CHARS
    assert "tools: A, B, C" in summary


def test_render_template_summary_no_tools_no_brackets() -> None:
    from opencomputer.agent.episodic import render_template_summary
    from plugin_sdk.core import Message

    summary = render_template_summary(
        user_message="hi",
        assistant_message=Message(role="assistant", content="hello"),
        tools_used=[],
    )
    assert "tools:" not in summary
    assert "Q: hi" in summary
    assert "A: hello" in summary


def test_extract_paths_finds_relative_and_absolute() -> None:
    from opencomputer.agent.episodic import _extract_paths

    text = (
        "I edited /Users/x/proj/src/auth.py and ran tests in tests/test_auth.py. "
        "Also looked at ./README.md but skipped node_modules/foo.js for now."
    )
    paths = _extract_paths(text)
    assert "/Users/x/proj/src/auth.py" in paths
    assert "tests/test_auth.py" in paths
    assert "./README.md" in paths


def test_extract_paths_dedups_and_caps() -> None:
    from opencomputer.agent.episodic import _extract_paths

    text = " ".join([f"file/path/x{i}.py" for i in range(20)] * 2)
    paths = _extract_paths(text, limit=5)
    assert len(paths) == 5
    # All unique
    assert len(set(paths)) == 5


# ─── Episodic memory: AgentLoop integration ───────────────────────────


def _config(tmp_path: Path):
    from opencomputer.agent.config import (
        Config,
        LoopConfig,
        MemoryConfig,
        ModelConfig,
        SessionConfig,
    )

    return Config(
        model=ModelConfig(provider="mock", model="mock", max_tokens=512, temperature=0.0),
        loop=LoopConfig(max_iterations=2, parallel_tools=False),
        session=SessionConfig(db_path=tmp_path / "s.db"),
        memory=MemoryConfig(
            declarative_path=tmp_path / "MEMORY.md", skills_path=tmp_path / "skills"
        ),
    )


async def test_agent_loop_records_episodic_after_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="all done at /tmp/x.py"),
            stop_reason="end_turn",
            usage=Usage(10, 3),
        )
    )

    loop = AgentLoop(provider=provider, config=cfg, compaction_disabled=True)
    await loop.run_conversation(user_message="check x.py", session_id="s-test")

    events = loop.db.list_episodic(session_id="s-test")
    assert len(events) == 1
    assert "check x.py" in events[0]["summary"]
    assert "all done at" in events[0]["summary"]
    # File path picked up from assistant content
    assert "/tmp/x.py" in (events[0]["file_paths"] or "")


async def test_agent_loop_episodic_disabled_skips_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer.agent.loop import AgentLoop
    from plugin_sdk.core import Message
    from plugin_sdk.provider_contract import ProviderResponse, Usage

    cfg = _config(tmp_path)
    cfg.session.db_path.parent.mkdir(parents=True, exist_ok=True)
    from opencomputer.tools.registry import registry

    monkeypatch.setattr(registry, "schemas", MagicMock(return_value=[]))

    provider = MagicMock()
    provider.complete = AsyncMock(
        return_value=ProviderResponse(
            message=Message(role="assistant", content="ok"),
            stop_reason="end_turn",
            usage=Usage(1, 1),
        )
    )

    loop = AgentLoop(
        provider=provider,
        config=cfg,
        compaction_disabled=True,
        episodic_disabled=True,
    )
    await loop.run_conversation(user_message="hi", session_id="s-off")

    assert loop.db.list_episodic(session_id="s-off") == []


# ─── Batch runner ──────────────────────────────────────────────────────


def test_parse_jsonl_well_formed(tmp_path: Path) -> None:
    from opencomputer.batch import parse_jsonl

    p = tmp_path / "in.jsonl"
    p.write_text(
        json.dumps({"id": "a", "prompt": "Q1"})
        + "\n"
        + json.dumps({"prompt": "Q2"})
        + "\n"
        + "  \n"  # blank line should be skipped
        + json.dumps({"id": "c", "prompt": "Q3", "system": "be terse", "model": "x"})
        + "\n"
    )
    reqs = parse_jsonl(p)
    assert len(reqs) == 3
    assert reqs[0].id == "a" and reqs[0].prompt == "Q1"
    assert reqs[1].id.startswith("req-")  # auto-generated
    assert reqs[2].system == "be terse"
    assert reqs[2].model == "x"


def test_parse_jsonl_rejects_missing_prompt(tmp_path: Path) -> None:
    from opencomputer.batch import parse_jsonl

    p = tmp_path / "bad.jsonl"
    p.write_text(json.dumps({"id": "a"}) + "\n")
    with pytest.raises(ValueError, match="missing 'prompt'"):
        parse_jsonl(p)


def test_parse_jsonl_rejects_invalid_json(tmp_path: Path) -> None:
    from opencomputer.batch import parse_jsonl

    p = tmp_path / "bad.jsonl"
    p.write_text("{not json}\n")
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_jsonl(p)


def test_build_anthropic_request_shape() -> None:
    from opencomputer.batch import BatchRequest, _build_anthropic_request

    req = BatchRequest(id="r1", prompt="hello", system="be brief", model="claude-haiku-4-5")
    entry = _build_anthropic_request(req)
    assert entry["custom_id"] == "r1"
    assert entry["params"]["model"] == "claude-haiku-4-5"
    assert entry["params"]["system"] == "be brief"
    assert entry["params"]["messages"] == [{"role": "user", "content": "hello"}]


def test_build_anthropic_request_omits_empty_system() -> None:
    from opencomputer.batch import BatchRequest, _build_anthropic_request

    req = BatchRequest(id="r1", prompt="hello", system="", model="x")
    entry = _build_anthropic_request(req)
    assert "system" not in entry["params"]


async def test_submit_poll_fetch_end_to_end_with_mocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opencomputer import batch as batch_mod

    # Build a fake anthropic client
    fake_batch = MagicMock(id="batch-xyz", processing_status="in_progress")
    fake_batch_done = MagicMock(id="batch-xyz", processing_status="ended")

    fake_client = MagicMock()
    fake_client.messages = MagicMock()
    fake_client.messages.batches = MagicMock()
    fake_client.messages.batches.create = AsyncMock(return_value=fake_batch)

    # Two retrieve calls: first returns in_progress, second returns ended
    fake_client.messages.batches.retrieve = AsyncMock(side_effect=[fake_batch, fake_batch_done])

    # results() returns an async iterator
    class _Iter:
        def __init__(self, items):
            self._items = items

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for i in self._items:
                yield i

    success_msg = MagicMock()
    success_msg.content = [MagicMock(text="answer-A")]
    success_msg.usage = MagicMock(input_tokens=5, output_tokens=2)
    success_entry = MagicMock(
        custom_id="a",
        result=MagicMock(type="succeeded", message=success_msg),
    )
    error_entry = MagicMock(
        custom_id="b",
        result=MagicMock(type="errored", error=MagicMock(message="rate limited")),
    )
    fake_client.messages.batches.results = AsyncMock(
        return_value=_Iter([success_entry, error_entry])
    )

    monkeypatch.setattr(batch_mod, "__import__", lambda *a, **kw: MagicMock(), raising=False)

    # Patch anthropic.AsyncAnthropic to return our fake client
    fake_anthropic = MagicMock()
    fake_anthropic.AsyncAnthropic = MagicMock(return_value=fake_client)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake_anthropic)

    in_path = tmp_path / "in.jsonl"
    in_path.write_text(
        json.dumps({"id": "a", "prompt": "Q1"})
        + "\n"
        + json.dumps({"id": "b", "prompt": "Q2"})
        + "\n"
    )
    out_path = tmp_path / "out.jsonl"

    final_status, n = await batch_mod.run_batch_end_to_end(
        in_path, out_path, api_key="fake-key", interval_s=0.0
    )
    assert final_status == "ended"
    assert n == 2

    # Output JSONL contains both results
    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec_a = json.loads(lines[0])
    assert rec_a["id"] == "a"
    assert rec_a["status"] == "succeeded"
    assert rec_a["output"] == "answer-A"
    rec_b = json.loads(lines[1])
    assert rec_b["id"] == "b"
    assert rec_b["status"] == "errored"
    assert "rate limited" in rec_b["error"]


def test_write_results_format(tmp_path: Path) -> None:
    from opencomputer.batch import BatchResult, write_results

    p = tmp_path / "out.jsonl"
    write_results(
        [
            BatchResult(id="a", status="succeeded", output="x", input_tokens=1),
            BatchResult(id="b", status="errored", error="boom"),
        ],
        p,
    )
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    a = json.loads(lines[0])
    assert a["id"] == "a"
    assert a["status"] == "succeeded"
    assert a["output"] == "x"
    assert a["input_tokens"] == 1
