"""Telegram channel adapter: PDF attachments survive into Message.attachments."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ADAPTER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "telegram" / "adapter.py"
)


def _load_adapter_module():
    spec = importlib.util.spec_from_file_location(
        "_test_telegram_adapter", ADAPTER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_telegram_adapter_does_not_explicitly_reject_pdfs():
    """Smoke check: the Telegram adapter source has no explicit PDF/document
    MIME-type rejection.

    This is a STATIC source check, not a behavior test — full end-to-end
    verification (send a real PDF via the bot, observe it land in
    Message.attachments) needs manual testing. Tracked in the PR's test
    plan checklist.

    We look for explicit blocklist patterns near ``application/pdf``:
    e.g. ``if mime_type == "application/pdf": skip`` or
    ``DISALLOWED_MIME = {"application/pdf", ...}``.

    Incidental occurrences of "skip"/"drop"/"block" in unrelated comments
    are excluded by requiring the rejection keyword to appear within the
    same logical block (~5 lines) as the PDF MIME literal.
    """
    source = ADAPTER_PATH.read_text()
    lines = source.splitlines()

    rejection_keywords = ("skip", "reject", "disallow", "blocklist", "denied")

    pdf_lines = [i for i, ln in enumerate(lines) if "application/pdf" in ln]
    assert pdf_lines, (
        "Adapter source does not mention application/pdf at all — "
        "expected at least the outgoing MIME mapping."
    )

    nearby_rejection = []
    for idx in pdf_lines:
        window = "\n".join(lines[max(0, idx - 5) : idx + 6]).lower()
        for kw in rejection_keywords:
            if kw in window:
                nearby_rejection.append((idx + 1, kw))

    assert not nearby_rejection, (
        f"Telegram adapter appears to reject PDFs near lines "
        f"{nearby_rejection}. Inspect the source and update the MIME "
        f"filter to allow application/pdf."
    )


def test_telegram_adapter_accepts_documents_unconditionally():
    """Behavior contract: the inbound document handler must add the
    file_id to ``attachments`` without checking ``mime_type`` against an
    allowlist.

    PDFs arrive as Telegram ``document`` updates with
    ``mime_type=application/pdf``. If the adapter ever introduces a
    MIME allowlist, this test catches it.
    """
    source = ADAPTER_PATH.read_text()

    # Find the inbound document-handling block. Pattern from adapter.py:
    #     if doc := msg.get("document"):
    #         file_id = doc.get("file_id")
    #         if file_id:
    #             attachments.append(f"telegram:{file_id}")
    doc_block_match = re.search(
        r'if doc := msg\.get\("document"\):(.*?)(?=\n        if |\n        # )',
        source,
        re.DOTALL,
    )
    assert doc_block_match, (
        "Could not locate the inbound document-handling block in "
        "extensions/telegram/adapter.py. The structural assumption of "
        "this test is stale — re-inspect the adapter."
    )

    block = doc_block_match.group(1)

    # The block must NOT contain a MIME allowlist check that would gate
    # PDF passthrough. We allow reading mime_type for metadata, but reject
    # any equality/membership check that would skip PDFs.
    forbidden_patterns = [
        r'mime_type\s*==\s*["\']image/',     # image-only allowlist
        r'mime_type\s*not in\s*\(',          # negative-list filter
        r'mime_type\s*not in\s*\[',
        r'mime_type\s*not in\s*\{',
        r'application/pdf.*?(?:skip|reject|return|continue)',
    ]
    for pat in forbidden_patterns:
        assert not re.search(pat, block, re.IGNORECASE | re.DOTALL), (
            f"Inbound document handler contains a MIME filter "
            f"({pat!r}) that may block PDFs. Audit the block:\n{block}"
        )
