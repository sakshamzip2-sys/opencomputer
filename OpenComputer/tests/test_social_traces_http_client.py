"""Phase 9.B tests: HttpTraceNetworkClient.

Drives the client via :class:`httpx.MockTransport` so we exercise the
serialize / send / parse / error-isolate paths without needing a
running OpenHub. Each test pins one behaviour (success, network
failure, malformed JSON, non-2xx, etc.).
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

# Alias bootstrap (mirrors other phase test files) so we can
# ``import extensions.social_traces`` from a hyphenated dir.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_EXT_DIR = _PROJECT_ROOT / "extensions"
_ST_DIR = _EXT_DIR / "social-traces"


def _ensure_alias() -> None:
    if "extensions.social_traces.client.http" in sys.modules:
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

    client_dir = _ST_DIR / "client"
    if "extensions.social_traces.client" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "extensions.social_traces.client",
            str(client_dir / "__init__.py"),
            submodule_search_locations=[str(client_dir)],
        )
        assert spec is not None and spec.loader is not None
        client_pkg = importlib.util.module_from_spec(spec)
        sys.modules["extensions.social_traces.client"] = client_pkg
        client_pkg.__package__ = "extensions.social_traces.client"
        # Submodules first so the package's own __init__ can re-export.
        for sub in ("local_file", "http"):
            full = f"extensions.social_traces.client.{sub}"
            if full in sys.modules:
                continue
            init = client_dir / f"{sub}.py"
            if not init.exists():
                continue
            sub_spec = importlib.util.spec_from_file_location(full, str(init))
            assert sub_spec is not None and sub_spec.loader is not None
            sub_mod = importlib.util.module_from_spec(sub_spec)
            sub_mod.__package__ = "extensions.social_traces.client"
            sys.modules[full] = sub_mod
            sub_spec.loader.exec_module(sub_mod)
        spec.loader.exec_module(client_pkg)
        parent.client = client_pkg


_ensure_alias()

from extensions.social_traces.client import make_client  # noqa: E402
from extensions.social_traces.client.http import (  # noqa: E402
    HttpTraceNetworkClient,
    _trace_card_from_wire,
    _trace_card_to_wire,
)

from plugin_sdk.traces import (  # noqa: E402
    QueryResult,
    SubmitReceipt,
    TraceCard,
    TraceMeta,
    TraceStep,
)


def _make_card(
    *,
    intent: str = "test intent that is long enough for the validator",
    tags: tuple[str, ...] = ("homelab", "test"),
    outcome: str = "success",
    submitter_hash: str = "a" * 64,
    score: float | None = None,
    status: str | None = None,
    trace_id: str | None = None,
) -> TraceCard:
    return TraceCard(
        schema_version="v1",
        intent=intent,
        meta=TraceMeta(
            tags=tags,
            outcome=outcome,
            token_cost=100,
            loop_count=3,
            harness_version="opencomputer/test",
            submitter_hash=submitter_hash,
        ),
        steps=(
            TraceStep(
                tool_name="Bash",
                arguments_summary="x",
                result_summary="y",
                duration_ms=10,
            ),
        ),
        distilled_insight="A reasonable insight string for testing.",
        created_at="2026-05-06T00:00:00Z",
        id=trace_id,
        status=status,
        score=score,
    )


# ─── factory ─────────────────────────────────────────────────────────


def test_factory_http_returns_http_client(tmp_path: Path):
    client = make_client(
        backend="http", profile_home=tmp_path, endpoint="http://example.test:8000"
    )
    assert isinstance(client, HttpTraceNetworkClient)


def test_factory_http_requires_endpoint(tmp_path: Path):
    with pytest.raises(ValueError):
        make_client(backend="http", profile_home=tmp_path, endpoint="")


def test_factory_http_strips_trailing_slash(tmp_path: Path):
    client = make_client(
        backend="http", profile_home=tmp_path, endpoint="http://example.test:8000/"
    )
    assert client._endpoint == "http://example.test:8000"


def test_factory_unknown_backend_raises(tmp_path: Path):
    with pytest.raises(ValueError):
        make_client(backend="bogus", profile_home=tmp_path, endpoint=None)


# ─── serialization round-trip ────────────────────────────────────────


def test_to_wire_strips_server_assigned_fields():
    """``id``, ``status``, ``score`` must NOT be sent on submit —
    OpenHub stamps them, and its Pydantic model rejects unknown
    inputs (``extra='forbid'``)."""
    card = _make_card(score=0.5, status="approved", trace_id="abc")
    raw = _trace_card_to_wire(card)
    assert "id" not in raw
    assert "status" not in raw
    assert "score" not in raw
    assert raw["intent"] == card.intent
    # ``dataclasses.asdict`` preserves tuples; httpx serializes them
    # to JSON arrays at send time. Compare values, not the container
    # type.
    assert tuple(raw["meta"]["tags"]) == card.meta.tags


def test_from_wire_round_trips():
    """Server response → TraceCard → can be re-serialized."""
    card = _make_card(score=2.5, status="approved", trace_id="xyz-1")
    payload = _trace_card_to_wire(card)
    payload["id"] = "xyz-1"
    payload["status"] = "approved"
    payload["score"] = 2.5
    rebuilt = _trace_card_from_wire(payload)
    assert rebuilt.id == "xyz-1"
    assert rebuilt.status == "approved"
    assert rebuilt.score == 2.5
    assert rebuilt.intent == card.intent
    assert rebuilt.meta == card.meta
    assert rebuilt.steps == card.steps


# ─── helpers for MockTransport scenarios ─────────────────────────────


def _client_with_handler(handler) -> HttpTraceNetworkClient:
    transport = httpx.MockTransport(handler)
    return HttpTraceNetworkClient(
        endpoint="http://test.local", transport=transport,
    )


# ─── query ───────────────────────────────────────────────────────────


async def test_query_happy_path_returns_traces():
    card = _make_card(score=1.5, status="approved", trace_id="srv-1")
    body_card = _trace_card_to_wire(card)
    body_card.update({"id": "srv-1", "status": "approved", "score": 1.5})

    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = req.content.decode()
        return httpx.Response(
            200,
            json={
                "traces": [body_card],
                "query_id": "q-42",
                "served_from": "network",
            },
        )

    client = _client_with_handler(handler)
    result = await client.query("a homelab task", ("homelab",), limit=3)

    assert "/v1/traces/query" in captured["url"]
    assert result.query_id == "q-42"
    assert result.served_from == "network"
    assert len(result.traces) == 1
    assert result.traces[0].id == "srv-1"
    assert result.traces[0].score == 1.5
    assert result.traces[0].meta.tags == card.meta.tags


async def test_query_network_error_returns_empty():
    """``httpx.ConnectError`` (server down) must NOT raise — return
    empty so the agent's pre-task path falls through to explore."""

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated server down")

    client = _client_with_handler(handler)
    result = await client.query("anything", ("x",))
    assert result == QueryResult()


async def test_query_5xx_returns_empty():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    client = _client_with_handler(handler)
    result = await client.query("anything", ("x",))
    assert result == QueryResult()


async def test_query_malformed_json_returns_empty():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"this is not json")

    client = _client_with_handler(handler)
    result = await client.query("anything", ("x",))
    assert result == QueryResult()


async def test_query_skips_malformed_trace_in_response():
    """One bad entry in the traces array shouldn't poison the rest."""
    good = _trace_card_to_wire(_make_card())
    good.update({"id": "g-1", "status": "approved", "score": 1.0})
    bad = {"intent": "missing required fields"}

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"traces": [bad, good], "query_id": "q", "served_from": "network"},
        )

    client = _client_with_handler(handler)
    result = await client.query("any", ("homelab",))
    assert len(result.traces) == 1
    assert result.traces[0].id == "g-1"


async def test_query_empty_traces_array():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"traces": [], "query_id": "q-0", "served_from": "network"},
        )

    client = _client_with_handler(handler)
    result = await client.query("any", ("none-match",))
    assert result.traces == ()
    assert result.query_id == "q-0"


async def test_query_sends_correct_body():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json

        captured.update(_json.loads(req.content))
        return httpx.Response(
            200, json={"traces": [], "query_id": "", "served_from": "network"},
        )

    client = _client_with_handler(handler)
    await client.query("look up X", ("a", "b", "c"), limit=7)
    assert captured == {"intent": "look up X", "tags": ["a", "b", "c"], "limit": 7}


# ─── submit ──────────────────────────────────────────────────────────


async def test_submit_happy_path_returns_accepted():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        import json as _json

        captured["body"] = _json.loads(req.content)
        return httpx.Response(
            200,
            json={"accepted": True, "queue_id": "q-99", "reason": ""},
        )

    client = _client_with_handler(handler)
    receipt = await client.submit(_make_card())

    assert receipt.accepted is True
    assert receipt.queue_id == "q-99"
    # Server-assigned fields must NOT be in the submitted body.
    for k in ("id", "status", "score"):
        assert k not in captured["body"]


async def test_submit_413_returns_dropped_receipt():
    """Payload too large = real protocol error; don't queue for retry."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"detail": "too big"})

    client = _client_with_handler(handler)
    receipt = await client.submit(_make_card())
    assert receipt.accepted is False
    assert "413" in receipt.reason


async def test_submit_5xx_returns_unaccepted_for_retry():
    """5xx is transient — caller can outbox-retry."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client_with_handler(handler)
    receipt = await client.submit(_make_card())
    assert receipt.accepted is False
    assert "503" in receipt.reason


async def test_submit_network_error_returns_unaccepted():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server down")

    client = _client_with_handler(handler)
    receipt = await client.submit(_make_card())
    assert receipt.accepted is False
    assert "ConnectError" in receipt.reason


async def test_submit_validation_failure_returns_accepted_false():
    """Server soft-fails validation as 200 + accepted=False (per
    OpenHub Phase 2 contract). Receipt forwards that verbatim."""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "accepted": False,
                "queue_id": None,
                "reason": "validation failed: tags must have 1-10 entries",
            },
        )

    client = _client_with_handler(handler)
    receipt = await client.submit(_make_card())
    assert receipt.accepted is False
    assert "validation" in receipt.reason


async def test_submit_malformed_response_returns_unaccepted():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    client = _client_with_handler(handler)
    receipt = await client.submit(_make_card())
    assert receipt.accepted is False


# ─── health ──────────────────────────────────────────────────────────


async def test_health_200_returns_true():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "0.1.0", "api": "v1"})

    client = _client_with_handler(handler)
    assert await client.health() is True


async def test_health_5xx_returns_false():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _client_with_handler(handler)
    assert await client.health() is False


async def test_health_network_error_returns_false():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = _client_with_handler(handler)
    assert await client.health() is False


async def test_health_does_not_raise_on_timeout():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client = _client_with_handler(handler)
    assert await client.health() is False


# ─── User-Agent header ───────────────────────────────────────────────


async def test_request_includes_user_agent():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["ua"] = req.headers.get("user-agent")
        return httpx.Response(200, json={"status": "ok", "version": "x", "api": "v1"})

    client = _client_with_handler(handler)
    await client.health()
    assert captured["ua"] is not None
    assert "opencomputer-social-traces" in captured["ua"]
