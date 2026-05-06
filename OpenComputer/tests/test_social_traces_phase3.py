"""Phase 3 tests for the local-file trace network backend.

Coverage focus (matching ``docs/plans/social-traces-plugin.md`` §10
Phase 3 acceptance):

* ``LocalFileTraceNetworkClient.query`` returns top-K matches by tag
  overlap; empty result when nothing matches.
* ``submit`` writes a TraceCard JSON to the outbox; round-trips
  through the canonical encoder.
* ``health`` returns True for a writable profile home, False when
  the directory tree can't be created.
* Soft timeout: a slow filesystem operation past ``timeout_s``
  surfaces as an empty result, never an exception.
* Inbox helpers (used by CLI): list / show / add / remove round-trip
  cleanly.
* Malformed JSON in inbox is skipped, not crashed-on.
* Factory selects the right backend for ``backend="local"``; raises
  ``NotImplementedError`` for ``backend="http"`` (Phase 9).

Tests run without OpenHub existing — pure local filesystem.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from plugin_sdk.traces import (
    QueryResult,
    SubmitReceipt,
    TraceCard,
    TraceMeta,
    TraceNetworkClient,
    TraceStep,
)

# ─── alias bootstrap (test-time, mirrors cli_traces._ensure_alias) ──

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    if "extensions.social_traces.client.local_file" in sys.modules:
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
    for sub in ("state", "identity", "config", "prefetch", "subscriber"):
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

    # client subpackage
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
        parent.client = client_pkg
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

from extensions.social_traces.client import make_client  # noqa: E402
from extensions.social_traces.client.local_file import (  # noqa: E402
    INBOX_DIRNAME,
    OUTBOX_DIRNAME,
    LocalFileTraceNetworkClient,
    score_trace,
    trace_card_from_dict,
    trace_card_to_dict,
)

# ─── helpers ──────────────────────────────────────────────────────────


def _make_card(
    *,
    intent: str,
    tags: tuple[str, ...] = ("homelab", "filesync"),
    outcome: str = "success",
    insight: str = "use rsync --checksum",
    trace_id: str | None = None,
) -> TraceCard:
    return TraceCard(
        schema_version="v1",
        intent=intent,
        meta=TraceMeta(
            tags=tags,
            outcome=outcome,  # type: ignore[arg-type]
            token_cost=500,
            loop_count=2,
            harness_version="opencomputer/0.1.0",
            submitter_hash="0" * 64,
        ),
        steps=(
            TraceStep(
                tool_name="Bash",
                arguments_summary="rsync ...",
                result_summary="0 errors",
                duration_ms=100,
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


# ─── factory ──────────────────────────────────────────────────────────


def test_factory_returns_local_for_local_backend(tmp_path: Path):
    client = make_client(backend="local", profile_home=tmp_path)
    assert isinstance(client, LocalFileTraceNetworkClient)
    assert isinstance(client, TraceNetworkClient)


def test_factory_http_returns_http_client(tmp_path: Path):
    """Phase 9.B wired the http path. The factory now returns an
    ``HttpTraceNetworkClient`` when called with ``backend='http'``;
    Phase 3's NotImplementedError is gone."""
    from extensions.social_traces.client.http import HttpTraceNetworkClient

    client = make_client(
        backend="http",
        profile_home=tmp_path,
        endpoint="http://localhost:8000",
    )
    assert isinstance(client, HttpTraceNetworkClient)
    assert isinstance(client, TraceNetworkClient)


def test_factory_http_requires_endpoint(tmp_path: Path):
    """``backend=http`` without an endpoint is a config error — fail
    loudly rather than silently fall back to localhost."""
    with pytest.raises(ValueError):
        make_client(backend="http", profile_home=tmp_path, endpoint="")


def test_factory_rejects_unknown_backend(tmp_path: Path):
    with pytest.raises(ValueError):
        make_client(backend="bogus", profile_home=tmp_path)


# ─── query ────────────────────────────────────────────────────────────


async def test_query_empty_inbox_returns_empty(tmp_path: Path):
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    result = await client.query("anything", ())
    assert isinstance(result, QueryResult)
    assert result.traces == ()


async def test_query_returns_card_with_tag_overlap(tmp_path: Path):
    """A trace whose tags overlap with the query must come back."""
    seeded = _make_card(
        intent="sync files on LAN",
        tags=("homelab", "filesync"),
        trace_id="t-1",
    )
    _seed_inbox(tmp_path, seeded)

    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    result = await client.query("sync stuff", ("homelab",))

    assert len(result.traces) == 1
    assert result.traces[0].id == "t-1"
    assert result.served_from == "network"


async def test_query_skips_traces_with_no_overlap(tmp_path: Path):
    """A trace with disjoint tags AND no intent-word overlap must not
    come back. (Score = 0 → filtered.)"""
    _seed_inbox(
        tmp_path,
        _make_card(
            intent="provision EC2 instances",
            tags=("aws", "cloud"),
            trace_id="t-aws",
        ),
    )

    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    result = await client.query("local file sync", ("homelab",))
    assert result.traces == ()


async def test_query_top_k_ordering_by_score(tmp_path: Path):
    """Higher tag-overlap traces rank above lower ones; success outcome
    weights up; top-K honours the limit."""
    # 2 tag matches + success
    _seed_inbox(
        tmp_path,
        _make_card(
            intent="rsync between two boxes on LAN",
            tags=("homelab", "filesync"),
            outcome="success",
            trace_id="best",
        ),
        name="best",
    )
    # 1 tag match + partial outcome
    _seed_inbox(
        tmp_path,
        _make_card(
            intent="cron tickle",
            tags=("homelab",),
            outcome="partial",
            trace_id="middle",
        ),
        name="middle",
    )
    # 1 tag match + failed outcome
    _seed_inbox(
        tmp_path,
        _make_card(
            intent="hardware test",
            tags=("homelab",),
            outcome="failed",
            trace_id="worst",
        ),
        name="worst",
    )

    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    result = await client.query("homelab work", ("homelab", "filesync"), limit=2)

    ids = [c.id for c in result.traces]
    assert len(ids) == 2
    assert ids[0] == "best"
    # "middle" must rank above "worst" — partial outweighs failed.
    assert ids[1] == "middle"


async def test_query_skips_malformed_inbox_files(tmp_path: Path):
    """A corrupt JSON file in inbox/ must not crash the query — the
    bad file is logged and skipped, the good one comes back."""
    inbox = tmp_path / "traces" / INBOX_DIRNAME
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "broken.json").write_text("not-json at all", encoding="utf-8")
    _seed_inbox(
        tmp_path,
        _make_card(
            intent="something",
            tags=("homelab",),
            trace_id="good",
        ),
    )

    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    result = await client.query("anything", ("homelab",))
    assert [c.id for c in result.traces] == ["good"]


async def test_query_skips_trace_missing_required_fields(tmp_path: Path):
    """A JSON file that parses but isn't a TraceCard (missing
    'intent', say) must be skipped, not crashed-on."""
    inbox = tmp_path / "traces" / INBOX_DIRNAME
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "incomplete.json").write_text(
        json.dumps({"schema_version": "v1"}), encoding="utf-8"
    )

    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    result = await client.query("anything", ("homelab",))
    assert result.traces == ()


# ─── submit ───────────────────────────────────────────────────────────


async def test_submit_writes_to_outbox(tmp_path: Path):
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    card = _make_card(intent="brand new task")

    receipt = await client.submit(card)
    assert receipt.accepted is True
    assert receipt.queue_id is not None

    outbox = tmp_path / "traces" / OUTBOX_DIRNAME
    files = list(outbox.glob("*.json"))
    assert len(files) == 1

    # File contents round-trip back to a TraceCard.
    raw = json.loads(files[0].read_text(encoding="utf-8"))
    restored = trace_card_from_dict(raw)
    assert restored.intent == "brand new task"
    # Server-side fields are stamped on submit so the on-disk shape
    # matches what OpenHub would return.
    assert restored.id == receipt.queue_id
    assert restored.status == "pending"


async def test_submit_returns_failed_receipt_on_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A filesystem failure must surface as ``accepted=False`` with a
    reason — never raise. The outbox-retry path on the agent side
    depends on this."""
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)

    def _boom(self, card):  # noqa: ANN001 — internal patch
        raise OSError("disk full")

    monkeypatch.setattr(LocalFileTraceNetworkClient, "_submit_sync", _boom)
    receipt = await client.submit(_make_card(intent="x"))
    assert receipt.accepted is False
    assert "disk full" in receipt.reason


# ─── health ───────────────────────────────────────────────────────────


async def test_health_writable_dir_returns_true(tmp_path: Path):
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    assert await client.health() is True


async def test_health_unwritable_returns_false(tmp_path: Path, monkeypatch):
    """A health check that can't write must return False, never raise."""
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)

    def _boom(self):  # noqa: ANN001
        raise OSError("read-only filesystem")

    monkeypatch.setattr(LocalFileTraceNetworkClient, "_health_sync", _boom)
    assert await client.health() is False


# ─── soft timeout ─────────────────────────────────────────────────────


async def test_query_soft_timeout_returns_empty(tmp_path: Path, monkeypatch):
    """A query that takes longer than ``timeout_s`` must return an
    empty result, NOT raise — agent must never paralyse on slow IO."""
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)

    def _slow(self, intent, tags, limit):  # noqa: ANN001
        # Sleep longer than the test's 0.05s timeout. We sleep
        # synchronously inside to_thread, so the wrapper's wait_for
        # cancels the wait — the thread keeps running but we don't
        # block the test on it.
        import time as _t
        _t.sleep(0.5)
        return QueryResult()

    monkeypatch.setattr(LocalFileTraceNetworkClient, "_query_sync", _slow)
    result = await client.query("x", (), timeout_s=0.05)
    assert result.traces == ()


async def test_health_soft_timeout_returns_false(tmp_path: Path, monkeypatch):
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)

    def _slow(self):  # noqa: ANN001
        import time as _t
        _t.sleep(0.5)
        return True

    monkeypatch.setattr(LocalFileTraceNetworkClient, "_health_sync", _slow)
    assert await client.health(timeout_s=0.05) is False


# ─── inbox-management helpers (used by CLI) ──────────────────────────


def test_list_inbox_empty_when_dir_missing(tmp_path: Path):
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    assert client.list_inbox() == []


def test_add_then_list_inbox(tmp_path: Path):
    """add_to_inbox copies a JSON file in; list_inbox sees it."""
    src = tmp_path / "src.json"
    card = _make_card(intent="alpha", trace_id="alpha-1")
    src.write_text(json.dumps(trace_card_to_dict(card), indent=2), encoding="utf-8")

    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    dest = client.add_to_inbox(src)
    assert dest.exists()
    assert dest.parent.name == INBOX_DIRNAME

    items = client.list_inbox()
    assert len(items) == 1
    stem, restored = items[0]
    assert restored.intent == "alpha"
    assert restored.id == "alpha-1"


def test_add_to_inbox_rejects_malformed_file(tmp_path: Path):
    """add_to_inbox validates by reconstruction. A file that isn't a
    TraceCard must raise so the CLI surfaces a clear error."""
    src = tmp_path / "src.json"
    src.write_text("not-json", encoding="utf-8")

    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    with pytest.raises(json.JSONDecodeError):
        client.add_to_inbox(src)


def test_show_inbox_by_id_or_stem(tmp_path: Path):
    """show_inbox resolves either by filename stem or ``id`` field."""
    card = _make_card(intent="beta", trace_id="my-id")
    _seed_inbox(tmp_path, card, name="filename-stem")

    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    by_stem = client.show_inbox("filename-stem")
    by_id = client.show_inbox("my-id")
    assert by_stem is not None and by_stem.intent == "beta"
    assert by_id is not None and by_id.intent == "beta"


def test_show_inbox_returns_none_for_unknown(tmp_path: Path):
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    assert client.show_inbox("ghost") is None


def test_remove_from_inbox(tmp_path: Path):
    _seed_inbox(tmp_path, _make_card(intent="x", trace_id="x"))
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    assert client.remove_from_inbox("x") is True
    assert client.list_inbox() == []
    assert client.remove_from_inbox("x") is False  # already gone


def test_list_outbox_after_submit(tmp_path: Path):
    client = LocalFileTraceNetworkClient(profile_home=tmp_path)
    card = _make_card(intent="submission test")

    receipt = asyncio.run(client.submit(card))
    assert receipt.accepted is True

    items = client.list_outbox()
    assert len(items) == 1
    _stem, restored = items[0]
    assert restored.intent == "submission test"
    assert restored.status == "pending"


# ─── score_trace (the dev-stub ranking function) ─────────────────────


def test_score_trace_zero_with_no_overlap():
    """No tag or word overlap → score is 0 regardless of outcome.

    This is the rule that prevents every success trace from matching
    every query — outcome is a tiebreaker, not a qualifier.
    """
    card = _make_card(
        intent="totally unrelated",
        tags=("aws",),
        outcome="success",
    )
    assert score_trace(card, intent="something else", tags=("homelab",)) == 0.0


def test_score_trace_higher_with_more_tag_overlap():
    a = _make_card(intent="x", tags=("homelab", "filesync"), outcome="success")
    b = _make_card(intent="x", tags=("homelab",), outcome="success")
    sa = score_trace(a, intent="x", tags=("homelab", "filesync"))
    sb = score_trace(b, intent="x", tags=("homelab", "filesync"))
    assert sa > sb


def test_score_trace_outcome_weight_orders_correctly():
    s = _make_card(intent="x", tags=("homelab",), outcome="success")
    p = _make_card(intent="x", tags=("homelab",), outcome="partial")
    f = _make_card(intent="x", tags=("homelab",), outcome="failed")
    score_s = score_trace(s, intent="x", tags=("homelab",))
    score_p = score_trace(p, intent="x", tags=("homelab",))
    score_f = score_trace(f, intent="x", tags=("homelab",))
    assert score_s > score_p > score_f


# ─── canonical encoder round-trip (sanity check) ─────────────────────


def test_trace_card_dict_round_trip():
    """``trace_card_to_dict`` ∘ ``trace_card_from_dict`` is the identity
    on the wire format. Both halves of the system depend on this.
    Mirrors tests/plugin_sdk/test_traces.py round-trip but uses the
    backend's helpers (which are what the CLI actually calls)."""
    card = _make_card(intent="r", tags=("a", "b"), trace_id="rid")
    restored = trace_card_from_dict(trace_card_to_dict(card))
    assert restored == card


def test_trace_card_dataclass_asdict_matches_helper():
    """Sanity: our helper agrees with stdlib dataclasses.asdict so a
    consumer that uses dataclasses.asdict will produce equivalent
    JSON. Catches accidental drift if someone customizes the helper
    later without updating callers."""
    card = _make_card(intent="r")
    assert trace_card_to_dict(card) == dataclasses.asdict(card)
