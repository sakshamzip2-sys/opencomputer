"""Tests for opencomputer/auth/google_code_assist.py — Cloud Code Assist preflight.

Covers project_id resolution: env-first → loadCodeAssist discovery →
onboardUser fallback. The actual inference adapter is tested separately.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def test_project_context_dataclass():
    from opencomputer.auth.google_code_assist import ProjectContext

    ctx = ProjectContext(project_id="proj-x", tier_id="standard-tier", source="env")
    assert ctx.project_id == "proj-x"
    assert ctx.source == "env"


def test_resolve_project_context_uses_configured_id_first(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_GEMINI_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    from opencomputer.auth.google_code_assist import resolve_project_context

    # Should NOT make any HTTP calls when caller passes configured_project_id
    with patch("httpx.post") as posted:
        ctx = resolve_project_context(
            access_token="at-x",
            configured_project_id="proj-from-config",
        )
    posted.assert_not_called()
    assert ctx.project_id == "proj-from-config"
    assert ctx.source == "config"


def test_resolve_project_context_uses_env_when_no_configured(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_GEMINI_PROJECT_ID", "proj-from-env")
    from opencomputer.auth.google_code_assist import resolve_project_context

    with patch("httpx.post") as posted:
        ctx = resolve_project_context(access_token="at-x")
    posted.assert_not_called()
    assert ctx.project_id == "proj-from-env"
    assert ctx.source == "env"


def test_resolve_project_context_falls_back_to_google_cloud_project_env(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_GEMINI_PROJECT_ID", raising=False)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "alt-env-proj")
    from opencomputer.auth.google_code_assist import resolve_project_context

    ctx = resolve_project_context(access_token="at-x")
    assert ctx.project_id == "alt-env-proj"
    assert ctx.source == "env"


def test_resolve_project_context_calls_load_code_assist_when_no_env(monkeypatch):
    """If no env var set, loadCodeAssist is called and its result used."""
    monkeypatch.delenv("OPENCOMPUTER_GEMINI_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)
    from opencomputer.auth.google_code_assist import resolve_project_context

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "currentTier": {"id": "free-tier"},
        "cloudaicompanionProject": "auto-discovered-proj",
    }
    with patch("httpx.post", return_value=mock_response):
        ctx = resolve_project_context(access_token="at-x")

    assert ctx.project_id == "auto-discovered-proj"
    assert ctx.tier_id == "free-tier"
    assert ctx.source == "discovered"


def test_resolve_project_context_onboards_when_no_tier(monkeypatch):
    """If loadCodeAssist returns no tier, onboard_user is called."""
    monkeypatch.delenv("OPENCOMPUTER_GEMINI_PROJECT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT_ID", raising=False)
    from opencomputer.auth.google_code_assist import resolve_project_context

    load_resp = MagicMock()
    load_resp.status_code = 200
    load_resp.json.return_value = {}  # No tier, no project
    onboard_resp = MagicMock()
    onboard_resp.status_code = 200
    onboard_resp.json.return_value = {
        "done": True,
        "response": {"cloudaicompanionProject": "newly-onboarded-proj"},
    }
    with patch("httpx.post", side_effect=[load_resp, onboard_resp]):
        ctx = resolve_project_context(access_token="at-x")

    assert ctx.project_id == "newly-onboarded-proj"
    assert ctx.tier_id == "free-tier"
    assert ctx.source == "onboarded"


def test_load_code_assist_post_shape():
    """loadCodeAssist POSTs the right body + headers."""
    from opencomputer.auth.google_code_assist import load_code_assist

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["body"] = kwargs.get("json", {})
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "currentTier": {"id": "standard-tier"},
            "cloudaicompanionProject": "x",
        }
        return resp

    with patch("httpx.post", side_effect=fake_post):
        load_code_assist(access_token="at-test")

    assert captured["url"].endswith("/v1internal:loadCodeAssist")
    assert captured["headers"]["Authorization"] == "Bearer at-test"
    assert captured["headers"]["Content-Type"] == "application/json"
    # metadata.duetProject should be empty when no project_id supplied
    assert captured["body"]["metadata"]["duetProject"] == ""


def test_onboard_user_requires_project_id_for_paid_tier():
    from opencomputer.auth.google_code_assist import (
        ProjectIdRequiredError,
        onboard_user,
    )

    with pytest.raises(ProjectIdRequiredError):
        onboard_user(access_token="at-x", tier_id="standard-tier", project_id="")


def test_onboard_user_does_not_require_project_id_for_free_tier():
    """Free tier auto-assigns a managed project — no project_id needed."""
    from opencomputer.auth.google_code_assist import onboard_user

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"done": True, "response": {}}

    with patch("httpx.post", return_value=mock_resp):
        result = onboard_user(access_token="at-x", tier_id="free-tier", project_id="")
    assert result.get("done") is True


def test_load_code_assist_raises_on_401():
    from opencomputer.auth.google_code_assist import (
        CodeAssistError,
        load_code_assist,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.text = '{"error":{"code":401,"message":"Unauthorized"}}'

    with patch("httpx.post", return_value=mock_resp):
        with pytest.raises(CodeAssistError, match="unauthorized"):
            load_code_assist(access_token="bad-token")


def test_load_code_assist_raises_on_429():
    from opencomputer.auth.google_code_assist import (
        CodeAssistError,
        load_code_assist,
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.text = (
        '{"error":{"code":429,"status":"RESOURCE_EXHAUSTED","message":"rate"}}'
    )

    with patch("httpx.post", return_value=mock_resp):
        with pytest.raises(CodeAssistError, match="rate_limited"):
            load_code_assist(access_token="x")
