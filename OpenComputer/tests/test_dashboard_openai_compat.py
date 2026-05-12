"""Tests for opencomputer.dashboard.routes.openai_compat — HTTP surface.

Uses ``TestClient`` against the OpenAI-compat router mounted in isolation
so tests stay fast (no full DashboardServer boot). The AgentLoop is
mocked at the function boundary used by the route handler.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencomputer.dashboard.routes.openai_compat import router


def _build_test_app(*, with_token: bool = True) -> FastAPI:
    app = FastAPI()
    app.state.session_token = "test-session-token" if with_token else None
    app.include_router(router)
    return app


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-session-token"}


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def test_health_returns_ok_without_auth() -> None:
    app = _build_test_app()
    client = TestClient(app)
    resp = client.get("/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_models_returns_openai_list_shape() -> None:
    app = _build_test_app()
    client = TestClient(app)

    with patch(
        "opencomputer.cli_model_picker._grouped_models",
        return_value={"anthropic": {"claude-opus-4-7"}, "openai": {"gpt-4o"}},
    ):
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    ids = {m["id"] for m in body["data"]}
    assert "claude-opus-4-7" in ids
    assert "gpt-4o" in ids
    for m in body["data"]:
        assert m["object"] == "model"
        assert "owned_by" in m


def test_models_dedupes_when_two_providers_share_id() -> None:
    app = _build_test_app()
    client = TestClient(app)

    with patch(
        "opencomputer.cli_model_picker._grouped_models",
        return_value={"a": {"shared-model"}, "b": {"shared-model"}},
    ):
        resp = client.get("/v1/models")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert len([m for m in data if m["id"] == "shared-model"]) == 1


def test_models_returns_error_envelope_when_registry_unavailable() -> None:
    app = _build_test_app()
    client = TestClient(app)
    with patch(
        "opencomputer.cli_model_picker._grouped_models",
        side_effect=RuntimeError("registry broken"),
    ):
        resp = client.get("/v1/models")
    # The route catches and surfaces an OpenAI error envelope but still 200
    # (consistent with how OpenAI returns degraded data).
    body = resp.json()
    assert "error" in body
    assert body["error"]["type"] == "server_error"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_chat_completions_requires_bearer_token() -> None:
    app = _build_test_app(with_token=True)
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


def test_chat_completions_rejects_wrong_token() -> None:
    app = _build_test_app(with_token=True)
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer not-the-right-token"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_chat_completions_rejects_empty_body(auth_headers: dict[str, str]) -> None:
    app = _build_test_app()
    client = TestClient(app)
    resp = client.post("/v1/chat/completions", headers=auth_headers, content=b"")
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "empty_body"


def test_chat_completions_rejects_non_json_body(auth_headers: dict[str, str]) -> None:
    app = _build_test_app()
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=b"not json at all",
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "malformed_json"


def test_chat_completions_rejects_non_object_body(auth_headers: dict[str, str]) -> None:
    app = _build_test_app()
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json=["not", "an", "object"],
    )
    assert resp.status_code == 400


def test_chat_completions_rejects_missing_messages(auth_headers: dict[str, str]) -> None:
    app = _build_test_app()
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_request_error"


def test_chat_completions_rejects_empty_messages(auth_headers: dict[str, str]) -> None:
    app = _build_test_app()
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x", "messages": []},
    )
    assert resp.status_code == 400


def test_chat_completions_rejects_no_user_message(auth_headers: dict[str, str]) -> None:
    app = _build_test_app()
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "x",
            "messages": [
                {"role": "system", "content": "be helpful"},
                {"role": "assistant", "content": "hi"},
            ],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "no_user_message"


def test_chat_completions_rejects_empty_final_user(auth_headers: dict[str, str]) -> None:
    app = _build_test_app()
    client = TestClient(app)
    resp = client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "x", "messages": [{"role": "user", "content": "   "}]},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "empty_user_message"


def test_chat_completions_rejects_oversized_body(auth_headers: dict[str, str]) -> None:
    app = _build_test_app()
    client = TestClient(app)
    huge = b"x" * (5 * 1024 * 1024)
    resp = client.post(
        "/v1/chat/completions",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=huge,
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


def test_chat_completions_rejects_unknown_model_when_registry_populated(
    auth_headers: dict[str, str],
) -> None:
    app = _build_test_app()
    client = TestClient(app)
    with patch(
        "opencomputer.cli_model_picker._grouped_models",
        return_value={"prov": {"known-model"}},
    ):
        resp = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "not-a-real-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"


# ---------------------------------------------------------------------------
# Happy path — non-streaming
# ---------------------------------------------------------------------------


def test_chat_completions_non_streaming_returns_openai_shape(
    auth_headers: dict[str, str],
) -> None:
    app = _build_test_app()
    client = TestClient(app)

    async def _fake_completion(**_: Any) -> str:
        return "the model said hello"

    with (
        patch(
            "opencomputer.cli_model_picker._grouped_models",
            return_value={"prov": {"my-model"}},
        ),
        patch(
            "opencomputer.dashboard.routes.openai_compat._run_agent_completion",
            new=AsyncMock(side_effect=_fake_completion),
        ),
    ):
        resp = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "my-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "my-model"
    assert len(body["choices"]) == 1
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["choices"][0]["message"]["content"] == "the model said hello"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "usage" in body


def test_chat_completions_non_streaming_propagates_internal_error(
    auth_headers: dict[str, str],
) -> None:
    app = _build_test_app()
    client = TestClient(app)

    async def _fail(**_: Any) -> str:
        raise RuntimeError("provider down")

    with (
        patch(
            "opencomputer.cli_model_picker._grouped_models",
            return_value={"prov": {"my-model"}},
        ),
        patch(
            "opencomputer.dashboard.routes.openai_compat._run_agent_completion",
            new=AsyncMock(side_effect=_fail),
        ),
    ):
        resp = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "my-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 500
    assert resp.json()["error"]["type"] == "server_error"


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_chat_completions_streaming_emits_openai_sse(
    auth_headers: dict[str, str],
) -> None:
    app = _build_test_app()
    client = TestClient(app)

    async def _fake_completion(
        *, user_message: str, history: list[Any], system_prompt: str | None,
        model: str, oc_session_id: str | None, stream_callback: Any = None,
    ) -> str:
        # Simulate the AgentLoop calling stream_callback with incremental deltas.
        if stream_callback is not None:
            stream_callback("hello ")
            stream_callback("world")
        return "hello world"

    with (
        patch(
            "opencomputer.cli_model_picker._grouped_models",
            return_value={"prov": {"my-model"}},
        ),
        patch(
            "opencomputer.dashboard.routes.openai_compat._run_agent_completion",
            new=_fake_completion,
        ),
    ):
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "my-model",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            chunks: list[str] = []
            for line in resp.iter_lines():
                chunks.append(line)

    # Should have at least one role-opening chunk, two content chunks, a
    # finish chunk, and the [DONE] sentinel. Keepalive (`: keepalive`)
    # lines are allowed mixed in.
    data_lines = [c for c in chunks if c.startswith("data: ")]
    assert any('"role"' in line and '"assistant"' in line for line in data_lines)
    assert any('"hello "' in line for line in data_lines)
    assert any('"world"' in line for line in data_lines)
    assert any('"finish_reason": "stop"' in line or '"finish_reason":"stop"' in line for line in data_lines)
    assert any(line == "data: [DONE]" for line in data_lines)


def test_chat_completions_streaming_in_band_error_chunk(
    auth_headers: dict[str, str],
) -> None:
    app = _build_test_app()
    client = TestClient(app)

    async def _fail(**_: Any) -> str:
        raise RuntimeError("upstream broke")

    with (
        patch(
            "opencomputer.cli_model_picker._grouped_models",
            return_value={"prov": {"my-model"}},
        ),
        patch(
            "opencomputer.dashboard.routes.openai_compat._run_agent_completion",
            new=_fail,
        ),
    ):
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "my-model",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
            },
        ) as resp:
            assert resp.status_code == 200  # in-band error, HTTP is 200
            text = "".join(part for part in resp.iter_text())
    assert "upstream broke" in text
    assert "data: [DONE]" in text


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


def test_chat_completions_multimodal_content_collapses_to_text(
    auth_headers: dict[str, str],
) -> None:
    app = _build_test_app()
    client = TestClient(app)

    captured: dict[str, Any] = {}

    async def _capture(*, user_message: str, **kw: Any) -> str:
        captured["user_message"] = user_message
        return ""

    with (
        patch(
            "opencomputer.cli_model_picker._grouped_models",
            return_value={"prov": {"my-model"}},
        ),
        patch(
            "opencomputer.dashboard.routes.openai_compat._run_agent_completion",
            new=AsyncMock(side_effect=_capture),
        ),
    ):
        resp = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "my-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "describe this:"},
                            {"type": "image_url", "image_url": {"url": "data:..."}},
                        ],
                    },
                ],
            },
        )
    assert resp.status_code == 200
    assert "describe this:" in captured["user_message"]
    assert "[non-text-content: image_url]" in captured["user_message"]


def test_chat_completions_skips_model_check_when_registry_empty(
    auth_headers: dict[str, str],
) -> None:
    """Empty registry → don't 404 every request; let the provider decide."""
    app = _build_test_app()
    client = TestClient(app)

    async def _ok(**_: Any) -> str:
        return "ok"

    with (
        patch(
            "opencomputer.cli_model_picker._grouped_models",
            return_value={},
        ),
        patch(
            "opencomputer.dashboard.routes.openai_compat._run_agent_completion",
            new=AsyncMock(side_effect=_ok),
        ),
    ):
        resp = client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "anything",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert resp.status_code == 200
