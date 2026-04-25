# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
"""Email triage helpers.

Composes Tier 2 communication tools to classify incoming email and generate
stub draft responses. **Never auto-sends** — all outputs are drafts only.

Design note: ``generate_draft_response`` produces a template-based stub.
LLM-driven draft generation is the agent-loop's responsibility (Phase 5 scope).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..tools.tier_2_communication import ReadEmailBodiesTool, ReadEmailMetadataTool

if TYPE_CHECKING:
    from ..subprocess.wrapper import OISubprocessWrapper

# Heuristic keyword sets for classification
_NEWSLETTER_KEYWORDS = frozenset({
    "unsubscribe",
    "newsletter",
    "weekly digest",
    "monthly update",
    "mailing list",
    "subscription",
    "no-reply",
    "noreply",
    "marketing",
    "promotion",
    "offer",
    "deal",
    "sale",
})

_URGENT_KEYWORDS = frozenset({
    "urgent",
    "asap",
    "immediately",
    "action required",
    "critical",
    "deadline",
    "important",
    "high priority",
    "time sensitive",
    "escalation",
})

_WORK_DOMAINS = frozenset({
    "github.com",
    "jira.atlassian.com",
    "slack.com",
    "linear.app",
    "notion.so",
    "google.com",
    "microsoft.com",
    "amazonaws.com",
    "stripe.com",
    "vercel.com",
})


def _classify_email(email: dict) -> str:
    """Classify a single email dict into a bucket name."""
    sender = str(email.get("from", "")).lower()
    subject = str(email.get("subject", "")).lower()
    combined = f"{sender} {subject}"

    # Urgent check (highest priority)
    if any(kw in combined for kw in _URGENT_KEYWORDS):
        return "urgent"

    # Newsletter check
    if any(kw in combined for kw in _NEWSLETTER_KEYWORDS):
        return "newsletters"

    # Work heuristic — sender domain matches work domains
    domain_match = re.search(r"@([\w.-]+)", sender)
    if domain_match:
        domain = domain_match.group(1)
        if any(wd in domain for wd in _WORK_DOMAINS):
            return "work"

    # Personal — generic @gmail / @yahoo / @icloud etc.
    if re.search(r"@(gmail|yahoo|icloud|hotmail|outlook|proton)\.", sender):
        return "personal"

    return "other"


async def classify_emails(
    wrapper: OISubprocessWrapper,
    *,
    days_back: int = 1,
) -> dict[str, list[dict]]:
    """Fetch recent email metadata and classify into buckets.

    Uses :class:`ReadEmailMetadataTool` (Tier 2) to read the last N emails
    (approximated by ``number=days_back * 50``), then classifies each into:

    * ``urgent`` — action-required keywords in subject or sender
    * ``newsletters`` — mailing-list / unsubscribe patterns
    * ``personal`` — consumer email domains
    * ``work`` — known work/SaaS domains
    * ``other`` — everything else

    Returns::

        {
            "urgent": [...],
            "newsletters": [...],
            "personal": [...],
            "work": [...],
            "other": [...],
        }
    """
    from plugin_sdk.core import ToolCall

    tool = ReadEmailMetadataTool(wrapper=wrapper)
    number = max(10, days_back * 50)
    call = ToolCall(
        id="classify-emails",
        name="read_email_metadata",
        arguments={"number": number, "unread_only": False},
    )
    result = await tool.execute(call)

    buckets: dict[str, list[dict]] = {
        "urgent": [],
        "newsletters": [],
        "personal": [],
        "work": [],
        "other": [],
    }

    if result.is_error or not result.content.strip():
        return buckets

    raw = result.content.strip()
    emails: list[dict] = []
    try:
        import ast

        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            emails = [e if isinstance(e, dict) else {"raw": str(e)} for e in parsed]
        elif isinstance(parsed, dict):
            emails = [parsed]
    except (ValueError, SyntaxError):
        # Can't parse — treat as single raw entry
        return buckets

    for email in emails:
        bucket = _classify_email(email)
        buckets[bucket].append(email)

    return buckets


async def generate_draft_response(
    wrapper: OISubprocessWrapper,
    email_id: str,
    *,
    tone: str = "professional",
) -> dict:
    """Generate a stub draft response for an email.

    Uses :class:`ReadEmailBodiesTool` (Tier 2) to load the original message,
    then produces a **template-based** draft (not LLM-driven — see module
    docstring).

    Drafts only — never calls ``send_email``.

    Parameters
    ----------
    wrapper:
        The OI subprocess wrapper.
    email_id:
        Identifier of the email to respond to (used in fetch params).
    tone:
        Tone of the draft response. Supported values: ``"professional"``,
        ``"casual"``, ``"brief"``.

    Returns::

        {
            "draft": str,     # draft body text
            "subject": str,   # reply subject
            "to": str,        # recipient address
        }
    """
    from plugin_sdk.core import ToolCall

    tool = ReadEmailBodiesTool(wrapper=wrapper)
    call = ToolCall(
        id=f"read-email-body-{email_id}",
        name="read_email_bodies",
        arguments={"number": 1, "unread_only": False},
    )
    result = await tool.execute(call)

    original_content = result.content if not result.is_error else ""

    # Extract metadata from the body string for use in the draft
    # Real parsing would require structured email data; this is a stub
    sender = ""
    subject = ""
    if original_content:
        for line in original_content.splitlines():
            lc = line.lower()
            if lc.startswith("'from'") or lc.startswith('"from"') or "from:" in lc:
                sender = line.split(":", 1)[-1].strip().strip("'\"")
            if lc.startswith("'subject'") or lc.startswith('"subject"') or "subject:" in lc:
                subject = line.split(":", 1)[-1].strip().strip("'\"")

    reply_subject = f"Re: {subject}" if subject else "Re: (your email)"
    to_addr = sender if sender else "unknown@example.com"

    # Template-based draft — LLM integration is Phase 5 / agent-loop scope
    templates = {
        "professional": (
            "Thank you for reaching out.\n\n"
            "I have reviewed your message and will follow up shortly.\n\n"
            "Best regards"
        ),
        "casual": (
            "Hey,\n\n"
            "Thanks for the message! I'll get back to you soon.\n\n"
            "Cheers"
        ),
        "brief": "Acknowledged. Will follow up.",
    }
    draft_body = templates.get(tone, templates["professional"])

    return {
        "draft": draft_body,
        "subject": reply_subject,
        "to": to_addr,
    }
