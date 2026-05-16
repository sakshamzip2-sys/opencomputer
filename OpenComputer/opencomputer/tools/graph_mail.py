"""``GraphSendMailTool`` — send an email through Microsoft Graph.

Build-chunk 3 of Milestone 3. The agent-facing tool over
:meth:`opencomputer.integrations.graph.client._MailOperations.send`
(``POST /me/sendMail``).

Consent tier — ``PER_ACTION``
-----------------------------
Sending mail is **irreversible** and **outward-facing**: a sent message cannot
be recalled and it leaves the user's account addressed to a named third party.
That is the textbook ``PER_ACTION`` case — the consent gate must show the user
*this* recipient list and *this* subject before *this* send. (An earlier plan
draft proposed ``EXPLICIT``; that tier grants a one-time "enable Graph mail" and
would let every subsequent send through unseen — too weak for this blast
radius. The Graph survey's pre-mortem reaches the same conclusion.)

Never retried
-------------
A send is the one Graph operation that must never be auto-retried — re-POSTing
``sendMail`` risks delivering a duplicate email. So this tool:

* acquires the access token *proactively* (``get_valid_access_token`` before the
  call), rather than relying on a reactive 401-refresh-retry; and
* issues exactly one ``mail.send`` call. Any failure — a 401, a 5xx, a
  transport error — is surfaced as an error :class:`ToolResult`. It is not
  retried, not on any status.

Recipient validation at the trust boundary
-------------------------------------------
``to`` / ``cc`` / ``bcc`` arrive from the model. Every address is validated as
a well-formed email *before any network call*; a malformed address yields a
clear error and the send is abandoned (Graph-survey pre-mortem failure mode #1
— a typo'd recipient must fail loudly, not silently mis-deliver).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from opencomputer.integrations.graph.client import GraphClient
from opencomputer.tools._graph_common import (
    NOT_AUTHENTICATED_MESSAGE,
    acquire_token,
    error_result,
    tool_available,
)
from plugin_sdk.consent import CapabilityClaim, ConsentTier
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.tool_contract import BaseTool, ToolSchema

#: Body-type values Microsoft Graph accepts (case-insensitive on input here;
#: the client normalizes to Graph's exact casing).
_VALID_BODY_TYPES = ("Text", "HTML")

#: Upper bound on recipients across ``to`` + ``cc`` + ``bcc`` for a single
#: send. Exchange Online enforces its own per-message limit; this is a sane
#: client-side guard so a runaway argument can't build a giant payload.
_MAX_RECIPIENTS = 100

#: Pragmatic email-syntax check applied at the trust boundary. This is a
#: *well-formedness* gate (one ``@``, a non-empty local part, a dotted domain
#: with a sane TLD, no whitespace / no angle brackets), not RFC 5322 in full —
#: deliverability is Graph's job. It rejects the realistic bad input: empty
#: strings, missing ``@``, ``"Name <addr>"`` display forms, and trailing junk.
_EMAIL_RE = re.compile(
    r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>.]{2,}$",
)


def _validate_recipients(field_name: str, raw: Any) -> list[str]:
    """Validate and normalize one recipient field from the model's arguments.

    Args:
        field_name: ``"to"`` / ``"cc"`` / ``"bcc"`` — used only in error text.
        raw: The value as supplied by the model. Expected: a list of strings.

    Returns:
        The cleaned, whitespace-trimmed addresses (possibly empty for ``cc`` /
        ``bcc``).

    Raises:
        ValueError: If ``raw`` is not a list of strings, or any element is not
            a well-formed email address. The message names the offending field
            and value so the agent can correct it.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(
            f"'{field_name}' must be a list of email-address strings, "
            f"got {type(raw).__name__}"
        )
    cleaned: list[str] = []
    for entry in raw:
        if not isinstance(entry, str):
            raise ValueError(
                f"'{field_name}' must contain only strings; found a "
                f"{type(entry).__name__}"
            )
        address = entry.strip()
        if not address:
            raise ValueError(f"'{field_name}' contains an empty address")
        if not _EMAIL_RE.match(address):
            raise ValueError(
                f"'{field_name}' contains a malformed email address: "
                f"{address!r}. Provide a plain address like "
                "'name@example.com' (no display name, no angle brackets)."
            )
        cleaned.append(address)
    return cleaned


class GraphSendMailTool(BaseTool):
    """Send an email from the signed-in Microsoft account via Microsoft Graph."""

    # Sends are inherently sequential side effects — never run two in parallel.
    parallel_safe: bool = False
    # A send is a one-shot side effect; exempting it from the loop detector is
    # unnecessary and the default (False) is correct.

    capability_claims: ClassVar[tuple[CapabilityClaim, ...]] = (
        CapabilityClaim(
            capability_id="graph.mail.send",
            tier_required=ConsentTier.PER_ACTION,
            human_description=(
                "Send an email from your Microsoft account. Each send is "
                "confirmed individually — you see the recipients and subject "
                "before it goes out."
            ),
            data_scope="microsoft-graph:Mail.Send",
        ),
    )

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="GraphSendMail",
            description=(
                "Send an email from the user's connected Microsoft account "
                "via Microsoft Graph (POST /me/sendMail). Requires the user to "
                "have run `oc auth login graph`. Each send is gated by a "
                "per-action consent prompt showing the recipients and subject. "
                "The send is never retried — if it fails, it is reported as an "
                "error rather than risking a duplicate email."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": (
                            "Recipient email addresses (at least one). Each "
                            "must be a plain address such as 'name@example.com' "
                            "- no display name, no angle brackets."
                        ),
                    },
                    "subject": {
                        "type": "string",
                        "description": "The email subject line.",
                    },
                    "body": {
                        "type": "string",
                        "description": "The email body content.",
                    },
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional carbon-copy recipient addresses.",
                    },
                    "bcc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional blind-carbon-copy recipient addresses."
                        ),
                    },
                    "body_type": {
                        "type": "string",
                        "enum": list(_VALID_BODY_TYPES),
                        "description": (
                            "Body content type: 'Text' (default) or 'HTML'."
                        ),
                    },
                },
                "required": ["to", "subject", "body"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:  # noqa: D102
        # 1. Inert until login (defence in depth — registration is also gated).
        if not tool_available():
            return ToolResult(
                tool_call_id=call.id,
                content=NOT_AUTHENTICATED_MESSAGE,
                is_error=True,
            )

        args = call.arguments if isinstance(call.arguments, dict) else {}

        # 2. Validate every argument BEFORE any network call. A malformed
        #    recipient must fail loudly here, not mis-deliver later.
        try:
            to = _validate_recipients("to", args.get("to"))
            cc = _validate_recipients("cc", args.get("cc"))
            bcc = _validate_recipients("bcc", args.get("bcc"))
            subject = self._require_str("subject", args.get("subject"))
            body = self._require_str("body", args.get("body"))
            body_type = self._validate_body_type(args.get("body_type"))
        except ValueError as exc:
            return ToolResult(
                tool_call_id=call.id, content=str(exc), is_error=True
            )

        if not to:
            return ToolResult(
                tool_call_id=call.id,
                content="'to' must contain at least one recipient.",
                is_error=True,
            )
        if len(to) + len(cc) + len(bcc) > _MAX_RECIPIENTS:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Too many recipients (limit {_MAX_RECIPIENTS} across "
                    "to / cc / bcc)."
                ),
                is_error=True,
            )

        # 3. Acquire the token PROACTIVELY before the send — so a near-expiry
        #    token is refreshed now, not discovered mid-send (a send is never
        #    retried, so there is no reactive 401-refresh path for it).
        try:
            token = await acquire_token()
        except Exception as exc:  # noqa: BLE001 - mapped to a clean ToolResult
            return error_result(call, exc)

        # 4. Exactly one send. Any failure is surfaced, never retried.
        try:
            async with GraphClient(token) as client:
                await client.mail.send(
                    to=to,
                    subject=subject,
                    body=body,
                    cc=cc or None,
                    bcc=bcc or None,
                    body_type=body_type,
                )
        except ValueError as exc:
            # Client-side rejection (e.g. body_type) — already validated, but
            # surface it cleanly rather than crashing.
            return ToolResult(
                tool_call_id=call.id, content=str(exc), is_error=True
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a clean ToolResult
            return error_result(call, exc)

        return ToolResult(
            tool_call_id=call.id,
            content=self._success_message(to, cc, bcc, subject),
        )

    @staticmethod
    def _require_str(field_name: str, value: Any) -> str:
        """Return ``value`` as a non-empty-ish string or raise ``ValueError``.

        ``subject`` may legitimately be empty; ``body`` may too. What is
        rejected is a non-string (the model passing a number / object).
        """
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError(
                f"'{field_name}' must be a string, got {type(value).__name__}"
            )
        return value

    @staticmethod
    def _validate_body_type(value: Any) -> str:
        """Validate the optional ``body_type`` argument (default ``Text``)."""
        if value is None:
            return "Text"
        if not isinstance(value, str):
            raise ValueError(
                f"'body_type' must be a string, got {type(value).__name__}"
            )
        normalized = value.strip().capitalize()
        # "HTML" capitalizes to "Html"; accept both spellings explicitly.
        if normalized == "Text":
            return "Text"
        if value.strip().lower() == "html":
            return "HTML"
        raise ValueError(
            f"'body_type' must be one of {list(_VALID_BODY_TYPES)}, got {value!r}"
        )

    @staticmethod
    def _success_message(
        to: list[str], cc: list[str], bcc: list[str], subject: str
    ) -> str:
        """Build the human-readable success line for a completed send."""
        lines = [
            "Email accepted for delivery by Microsoft Graph.",
            f"  To: {', '.join(to)}",
        ]
        if cc:
            lines.append(f"  Cc: {', '.join(cc)}")
        if bcc:
            lines.append(f"  Bcc: {', '.join(bcc)}")
        lines.append(f"  Subject: {subject or '(no subject)'}")
        lines.append(
            "Delivery is handled asynchronously by Exchange Online; HTTP 202 "
            "means accepted, not yet delivered."
        )
        return "\n".join(lines)


__all__ = ["GraphSendMailTool"]
