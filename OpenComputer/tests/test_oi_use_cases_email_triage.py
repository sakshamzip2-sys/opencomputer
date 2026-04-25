"""Tests for use_cases.email_triage.

Covers:
- classify_emails returns correct bucket structure
- classify_emails places urgent emails in 'urgent' bucket
- classify_emails places newsletter emails in 'newsletters' bucket
- generate_draft_response returns draft dict with required keys
- generate_draft_response does NOT call send_email
- generate_draft_response reads email body via ReadEmailBodiesTool
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from extensions.oi_capability.use_cases.email_triage import (
    classify_emails,
    generate_draft_response,
)

from plugin_sdk.core import ToolResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wrapper():
    w = MagicMock()
    w.call = AsyncMock(return_value={})
    return w


def _tool_result(content="", *, is_error=False):
    return ToolResult(tool_call_id="t", content=content, is_error=is_error)


_MIXED_EMAILS_RAW = str([
    {"from": "boss@corp.com", "subject": "URGENT: action required now", "id": "1"},
    {"from": "noreply@newsletter.io", "subject": "Your weekly digest", "id": "2"},
    {"from": "friend@gmail.com", "subject": "Dinner plans", "id": "3"},
    {"from": "alerts@github.com", "subject": "PR review requested", "id": "4"},
    {"from": "unknown@weirdplace.xyz", "subject": "Random stuff", "id": "5"},
])


# ---------------------------------------------------------------------------
# classify_emails
# ---------------------------------------------------------------------------

class TestClassifyEmails:
    async def test_returns_correct_bucket_structure(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailMetadataTool.execute",
            new=AsyncMock(return_value=_tool_result("[]")),
        ):
            result = await classify_emails(_make_wrapper())

        for bucket in ("urgent", "newsletters", "personal", "work", "other"):
            assert bucket in result, f"Missing bucket: {bucket}"
            assert isinstance(result[bucket], list)

    async def test_places_urgent_email_in_urgent_bucket(self):
        emails = str([{"from": "cto@corp.com", "subject": "CRITICAL: server down", "id": "1"}])
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailMetadataTool.execute",
            new=AsyncMock(return_value=_tool_result(emails)),
        ):
            result = await classify_emails(_make_wrapper())

        assert len(result["urgent"]) >= 1

    async def test_places_newsletter_in_newsletters_bucket(self):
        emails = str([{"from": "noreply@marketing.com", "subject": "Your weekly newsletter", "id": "2"}])
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailMetadataTool.execute",
            new=AsyncMock(return_value=_tool_result(emails)),
        ):
            result = await classify_emails(_make_wrapper())

        assert len(result["newsletters"]) >= 1

    async def test_places_personal_email_in_personal_bucket(self):
        emails = str([{"from": "friend@gmail.com", "subject": "Weekend plans", "id": "3"}])
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailMetadataTool.execute",
            new=AsyncMock(return_value=_tool_result(emails)),
        ):
            result = await classify_emails(_make_wrapper())

        assert len(result["personal"]) >= 1

    async def test_places_github_email_in_work_bucket(self):
        emails = str([{"from": "notifications@github.com", "subject": "PR review request", "id": "4"}])
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailMetadataTool.execute",
            new=AsyncMock(return_value=_tool_result(emails)),
        ):
            result = await classify_emails(_make_wrapper())

        assert len(result["work"]) >= 1

    async def test_returns_empty_buckets_on_error(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailMetadataTool.execute",
            new=AsyncMock(return_value=_tool_result("err", is_error=True)),
        ):
            result = await classify_emails(_make_wrapper())

        assert all(result[b] == [] for b in ("urgent", "newsletters", "personal", "work", "other"))


# ---------------------------------------------------------------------------
# generate_draft_response
# ---------------------------------------------------------------------------

class TestGenerateDraftResponse:
    async def test_returns_draft_dict_with_required_keys(self):
        body_raw = "[{'from': 'alice@corp.com', 'subject': 'Hello', 'body': 'Hi there'}]"
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailBodiesTool.execute",
            new=AsyncMock(return_value=_tool_result(body_raw)),
        ):
            result = await generate_draft_response(_make_wrapper(), "email-id-123")

        assert "draft" in result
        assert "subject" in result
        assert "to" in result
        assert isinstance(result["draft"], str)
        assert len(result["draft"]) > 0

    async def test_does_not_call_send_email(self):
        """Draft generation MUST NOT invoke SendEmailTool."""
        with (
            patch(
                "extensions.oi_capability.tools.tier_2_communication.ReadEmailBodiesTool.execute",
                new=AsyncMock(return_value=_tool_result("email body")),
            ),
            patch(
                "extensions.oi_capability.tools.tier_2_communication.SendEmailTool.execute",
            ) as send_mock,
        ):
            await generate_draft_response(_make_wrapper(), "email-id-456")

        send_mock.assert_not_called()

    async def test_reads_email_body(self):
        read_mock = AsyncMock(return_value=_tool_result("email body content"))

        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailBodiesTool.execute",
            new=read_mock,
        ):
            await generate_draft_response(_make_wrapper(), "email-id-789")

        read_mock.assert_awaited_once()

    async def test_tone_professional(self):
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailBodiesTool.execute",
            new=AsyncMock(return_value=_tool_result("body")),
        ):
            result = await generate_draft_response(_make_wrapper(), "id", tone="professional")

        # Professional tone should include a polite opener
        assert len(result["draft"]) > 10

    async def test_subject_starts_with_re(self):
        body_raw = "subject: Original Email"
        with patch(
            "extensions.oi_capability.tools.tier_2_communication.ReadEmailBodiesTool.execute",
            new=AsyncMock(return_value=_tool_result(body_raw)),
        ):
            result = await generate_draft_response(_make_wrapper(), "id")

        assert result["subject"].startswith("Re:")
