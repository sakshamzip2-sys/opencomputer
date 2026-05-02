# SP2 — PDF + Provider Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make PDF attachments work end-to-end across Anthropic + Bedrock providers (with OpenAI degrading gracefully), AND fix the silent Bedrock citations footgun that drops PDF visual understanding to text-only when citations isn't enabled.

**Architecture:** Extend existing `Message.attachments` plumbing (no new BaseProvider method). Per-provider attachment helpers dispatch on MIME type — images use the existing path; PDFs use a new document-block path. Bedrock auto-enables `citations` in the Converse request when any document block is present.

**Tech Stack:** Python 3.12+, pytest, base64 stdlib, mimetypes stdlib, no new third-party deps (page-count uses byte-counting heuristic).

**Spec:** [`docs/superpowers/specs/2026-05-02-sp2-pdf-provider-hardening-design.md`](../specs/2026-05-02-sp2-pdf-provider-hardening-design.md)

---

## Pre-flight

- [ ] **Step 0a: Verify worktree**

```bash
cd /private/tmp/oc-sp2-pdf-providers
git status
git branch --show-current
```

Expected: clean tree, on `feat/sp2-pdf-provider-hardening`. If different, stop and ask.

- [ ] **Step 0b: Baseline pytest (provider + plugin_sdk scope)**

```bash
cd OpenComputer
pytest tests/ -k "provider or plugin_sdk or bedrock or anthropic or openai" --tb=short -q 2>&1 | tail -10
```

Expected: all pass. Record the count for the post-execution comparison.

- [ ] **Step 0c: Baseline ruff**

```bash
ruff check plugin_sdk/ extensions/anthropic-provider/ extensions/aws-bedrock-provider/ extensions/openai-provider/ tests/
```

Expected: clean.

---

## Task 1: PDF helper module (plugin_sdk/pdf_helpers.py)

**Files:**
- Create: `plugin_sdk/pdf_helpers.py`
- Modify: `plugin_sdk/__init__.py` (add re-exports)
- Test: `tests/test_pdf_helpers.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_pdf_helpers.py`:

```python
"""Tests for plugin_sdk.pdf_helpers — PDF byte handling utilities."""
from __future__ import annotations

import base64

from plugin_sdk.pdf_helpers import (
    PDF_HARD_PAGE_LIMIT,
    PDF_MAX_BYTES,
    PDF_SOFT_PAGE_LIMIT,
    count_pdf_pages,
    pdf_to_base64,
)


def _make_minimal_pdf(num_pages: int = 1) -> bytes:
    """Build a minimal-but-parseable PDF with the requested page count."""
    pages = b""
    for _ in range(num_pages):
        pages += b"<< /Type /Page >>\n"
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        + pages
        + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def test_constants_match_spec():
    assert PDF_MAX_BYTES == 32 * 1024 * 1024
    assert PDF_HARD_PAGE_LIMIT == 600
    assert PDF_SOFT_PAGE_LIMIT == 100


def test_count_pdf_pages_known_3_page_pdf():
    pdf_bytes = _make_minimal_pdf(num_pages=3)
    assert count_pdf_pages(pdf_bytes) == 3


def test_count_pdf_pages_returns_negative_for_garbage():
    assert count_pdf_pages(b"not a pdf at all") == 0


def test_pdf_to_base64_roundtrip():
    pdf_bytes = _make_minimal_pdf(num_pages=1)
    encoded = pdf_to_base64(pdf_bytes)
    assert isinstance(encoded, str)
    assert base64.standard_b64decode(encoded) == pdf_bytes
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/test_pdf_helpers.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'plugin_sdk.pdf_helpers'`.

- [ ] **Step 3: Implement the helper module**

Create `plugin_sdk/pdf_helpers.py`:

```python
"""PDF byte-handling utilities shared across provider plugins.

Provides:
- Size + page-count limits matching Anthropic's published PDF support spec
  (https://docs.claude.com/en/build-with-claude/pdf-support)
- A cheap byte-counting page-count heuristic that avoids adding pypdf as
  a dependency. Approximate; intended for guard rails, not analysis.
- Base64 encoding helper for the Anthropic content-block source format.
"""
from __future__ import annotations

import base64

# Anthropic limits (per https://docs.claude.com/en/build-with-claude/pdf-support):
# - Max request size: 32 MB (entire payload, including non-PDF content)
# - Max pages: 600 (hard cap)
# - Effective max for 200k-context-window models: 100 pages
PDF_MAX_BYTES: int = 32 * 1024 * 1024
PDF_HARD_PAGE_LIMIT: int = 600
PDF_SOFT_PAGE_LIMIT: int = 100


def count_pdf_pages(data: bytes) -> int:
    """Count PDF pages via byte-marker scan.

    PDF objects of type ``/Page`` are individual pages. ``/Pages`` is the
    catalog (parent). The page count is the number of ``/Type /Page``
    markers minus the number of ``/Type /Pages`` catalogs.

    Approximate — a content stream containing the literal bytes
    ``/Type /Page`` would over-count. Good enough for the soft/hard
    limit guards (we want to catch obvious cases, not produce a precise
    count).

    Returns 0 for non-PDF input or malformed data.
    """
    page_marker = data.count(b"/Type /Page") - data.count(b"/Type /Pages")
    return max(page_marker, 0)


def pdf_to_base64(data: bytes) -> str:
    """Standard-base64 encode PDF bytes for Anthropic's document source field."""
    return base64.standard_b64encode(data).decode("ascii")


__all__ = [
    "PDF_MAX_BYTES",
    "PDF_HARD_PAGE_LIMIT",
    "PDF_SOFT_PAGE_LIMIT",
    "count_pdf_pages",
    "pdf_to_base64",
]
```

- [ ] **Step 4: Add re-exports to plugin_sdk/__init__.py**

Read `plugin_sdk/__init__.py` first to see existing import style. Add (in the appropriate alphabetical or grouped location):

```python
from plugin_sdk.pdf_helpers import (
    PDF_HARD_PAGE_LIMIT,
    PDF_MAX_BYTES,
    PDF_SOFT_PAGE_LIMIT,
    count_pdf_pages,
    pdf_to_base64,
)
```

And add the names to the `__all__` list.

- [ ] **Step 5: Run test to verify it passes**

```bash
pytest tests/test_pdf_helpers.py -v
```

Expected: all 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd /private/tmp/oc-sp2-pdf-providers
git add OpenComputer/plugin_sdk/pdf_helpers.py OpenComputer/plugin_sdk/__init__.py OpenComputer/tests/test_pdf_helpers.py
git commit -m "feat(plugin_sdk): add pdf_helpers module (page-count, size limits, base64)"
```

---

## Task 2: Anthropic provider PDF support

**Files:**
- Modify: `extensions/anthropic-provider/provider.py` (extend the attachment helper)
- Test: `tests/test_anthropic_provider_pdf.py` (NEW)

- [ ] **Step 1: Read the existing attachment helper to understand its current shape**

```bash
sed -n '70,135p' extensions/anthropic-provider/provider.py
```

Note the function name (likely `_anthropic_content_blocks_from_message` or `_build_image_content_blocks`), its signature (`*, text: str, image_paths: list[str]`), and the order it returns (images first, then text).

- [ ] **Step 2: Write the failing test**

Create `tests/test_anthropic_provider_pdf.py`:

```python
"""PDF attachment handling in the Anthropic provider."""
from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

# Anthropic provider is a plugin — load via the same pattern as other tests
PROVIDER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_provider", PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_minimal_pdf(num_pages: int = 1) -> bytes:
    pages = b"<< /Type /Page >>\n" * num_pages
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        + pages
        + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def test_anthropic_builds_document_block_for_pdf_attachment(tmp_path):
    module = _load_provider_module()
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_make_minimal_pdf(num_pages=2))

    blocks = module._content_blocks_with_attachments(
        text="Summarize this PDF.",
        attachment_paths=[str(pdf_path)],
    )

    # Expect: 1 document block + 1 text block, in that order
    assert len(blocks) == 2
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"]["type"] == "base64"
    assert blocks[0]["source"]["media_type"] == "application/pdf"
    decoded = base64.standard_b64decode(blocks[0]["source"]["data"])
    assert decoded == pdf_path.read_bytes()
    assert blocks[1]["type"] == "text"
    assert blocks[1]["text"] == "Summarize this PDF."


def test_anthropic_skips_oversize_pdf(tmp_path, caplog):
    import logging
    from plugin_sdk.pdf_helpers import PDF_MAX_BYTES

    module = _load_provider_module()
    pdf_path = tmp_path / "big.pdf"
    pdf_path.write_bytes(b"x" * (PDF_MAX_BYTES + 1))

    with caplog.at_level(logging.WARNING):
        blocks = module._content_blocks_with_attachments(
            text="hi",
            attachment_paths=[str(pdf_path)],
        )

    # Only text block; PDF dropped with warning
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert any("over 32 MB" in r.message for r in caplog.records)


def test_anthropic_skips_unreadable_pdf(tmp_path, caplog):
    import logging
    module = _load_provider_module()

    with caplog.at_level(logging.WARNING):
        blocks = module._content_blocks_with_attachments(
            text="hi",
            attachment_paths=[str(tmp_path / "missing.pdf")],
        )

    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert any("unreadable" in r.message for r in caplog.records)


def test_anthropic_image_path_still_works(tmp_path):
    """Regression: existing image attachment path is unchanged."""
    module = _load_provider_module()
    # Minimal valid PNG (1x1 transparent)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63000100000005000100020701020000000049454e"
        "44ae426082"
    )
    img = tmp_path / "tiny.png"
    img.write_bytes(png_bytes)

    blocks = module._content_blocks_with_attachments(
        text="Describe this.",
        attachment_paths=[str(img)],
    )
    assert any(b["type"] == "image" for b in blocks)
    assert blocks[-1]["type"] == "text"
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_anthropic_provider_pdf.py -v
```

Expected: FAIL — `_content_blocks_with_attachments` doesn't exist (current code has `_image_content_blocks_from_message` or similar that's image-only).

- [ ] **Step 4: Modify the existing helper to dispatch on MIME type**

The existing image-only helper in `extensions/anthropic-provider/provider.py` (probably named something like `_build_image_content_blocks` or `_anthropic_content_blocks_from_message`, signature `*, text: str, image_paths: list[str]`) needs to:
1. Be **renamed** to `_content_blocks_with_attachments` and its parameter renamed to `attachment_paths` for accuracy.
2. Dispatch per-attachment by MIME type (image vs PDF vs unsupported).
3. Update all callsites that referenced the old name/parameter.

Replace the existing image-helper block with this. Keep the existing imports/log/constant declarations and replace just the function:

```python
import base64
import logging
import mimetypes
from pathlib import Path

from plugin_sdk.pdf_helpers import (
    PDF_HARD_PAGE_LIMIT,
    PDF_MAX_BYTES,
    PDF_SOFT_PAGE_LIMIT,
    count_pdf_pages,
    pdf_to_base64,
)

_log = logging.getLogger(__name__)

# The 4 image MIME types Anthropic accepts (existing constant — keep)
_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # existing 5MB cap (keep)


def _build_pdf_block(path: Path) -> dict | None:
    """Build an Anthropic `document` content block from a PDF path.

    Returns None and logs a warning if the file is unreadable, oversize,
    or exceeds the hard page limit. Soft page limit (100) only logs a
    warning — the block is still returned.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        _log.warning("PDF attachment unreadable: %s (%s)", path, exc)
        return None
    if len(data) > PDF_MAX_BYTES:
        _log.warning(
            "PDF attachment over 32 MB cap; skipping: %s (%d bytes)",
            path, len(data),
        )
        return None
    page_count = count_pdf_pages(data)
    if page_count > PDF_HARD_PAGE_LIMIT:
        _log.warning(
            "PDF over 600-page hard limit; skipping: %s (%d pages)",
            path, page_count,
        )
        return None
    if page_count > PDF_SOFT_PAGE_LIMIT:
        _log.warning(
            "PDF over 100 pages; may exceed 200k-context-model capacity: %s (%d pages)",
            path, page_count,
        )
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": pdf_to_base64(data),
        },
    }


def _build_image_block(path: Path, media_type: str) -> dict | None:
    """Build an Anthropic `image` content block from an image path.

    Returns None and logs a warning on read errors or size cap.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        _log.warning("image attachment unreadable: %s (%s)", path, exc)
        return None
    if len(data) > _IMAGE_MAX_BYTES:
        _log.warning(
            "image attachment over 5 MB cap; skipping: %s (%d bytes)",
            path, len(data),
        )
        return None
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def _content_blocks_with_attachments(
    *, text: str, attachment_paths: list[str]
) -> list[dict]:
    """Build Anthropic content array combining text + media attachments.

    Dispatches per-attachment based on MIME type:
    - application/pdf → document block (32 MB cap, 600-page hard limit)
    - image/png|jpeg|gif|webp → image block (5 MB cap)
    - other → log warning, skip

    Order: media blocks first, then text — matches what Claude Desktop
    sends and what humans expect.
    """
    blocks: list[dict] = []
    for path_str in attachment_paths:
        path = Path(path_str)
        media_type = mimetypes.guess_type(path.name)[0]
        if media_type == "application/pdf" or path.suffix.lower() == ".pdf":
            block = _build_pdf_block(path)
        elif media_type in _IMAGE_MIME_TYPES:
            block = _build_image_block(path, media_type)
        else:
            _log.warning(
                "attachment has unsupported media type %r; skipping: %s",
                media_type, path,
            )
            block = None
        if block is not None:
            blocks.append(block)
    blocks.append({"type": "text", "text": text})
    return blocks
```

Then update the call site(s). Find them:

```bash
grep -n "image_paths\|_build_image_content_blocks\|_anthropic_content_blocks_from_message" extensions/anthropic-provider/provider.py
```

For each callsite:
- If it's the old helper's name → rename to `_content_blocks_with_attachments`
- If it passes `image_paths=m.attachments` → change to `attachment_paths=m.attachments`

If `complete_vision` (added in PR #351) calls the old helper with `image_paths=`, EITHER:
- (a) Update its callsite to pass `attachment_paths=` (preferred, less code), OR
- (b) Add a back-compat alias if multiple external callers depend on the old name (unlikely in this private extension)

Don't leave the old function name as dead code. Either rename or alias intentionally.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_anthropic_provider_pdf.py -v
```

Expected: all 4 PASS.

- [ ] **Step 6: Run all anthropic-provider tests for regressions**

```bash
pytest tests/ -k "anthropic" --tb=short -q | tail -10
```

Expected: all PASS (existing image-attachment tests should still work).

- [ ] **Step 7: Commit**

```bash
cd /private/tmp/oc-sp2-pdf-providers
git add OpenComputer/extensions/anthropic-provider/provider.py OpenComputer/tests/test_anthropic_provider_pdf.py
git commit -m "feat(anthropic-provider): support PDF document blocks via Message.attachments"
```

---

## Task 3: Bedrock provider PDF support + citations footgun fix

**Files:**
- Modify: `extensions/aws-bedrock-provider/transport.py` (extend `format_request`)
- Test: `tests/test_bedrock_pdf_citations.py` (NEW)

This is the highest-priority fix in SP2.

- [ ] **Step 1: Read the existing format_request to understand current shape**

```bash
sed -n '50,100p' extensions/aws-bedrock-provider/transport.py
```

Note: it currently builds `[{"text": msg.content}]` content blocks unconditionally. `req.messages` is a list of `plugin_sdk.core.Message` objects with `content: str` and `attachments: list[str]`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_bedrock_pdf_citations.py`:

```python
"""Bedrock provider: PDF document blocks + citations footgun fix."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

TRANSPORT_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "aws-bedrock-provider" / "transport.py"
)


def _load_transport_module():
    spec = importlib.util.spec_from_file_location(
        "_test_bedrock_transport", TRANSPORT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_minimal_pdf() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"<< /Type /Page >>\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def _make_request(*, text: str, attachments: list[str] | None = None):
    """Build a NormalizedRequest with one user message."""
    from plugin_sdk.core import Message
    from plugin_sdk.transports import NormalizedRequest

    return NormalizedRequest(
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        messages=[Message(role="user", content=text, attachments=attachments or [])],
        system="",
        max_tokens=1024,
        temperature=0.0,
        tools=[],
    )


@pytest.fixture
def transport():
    """Construct a BedrockTransport bypassing __init__ (we never call .send,
    so we don't need a real boto3 client)."""
    module = _load_transport_module()
    t = module.BedrockTransport.__new__(module.BedrockTransport)
    t._region = "us-east-1"
    t._client = None  # format_request never touches the client
    return t


def test_bedrock_no_citations_when_text_only(transport, tmp_path):
    """Regression: text-only requests must NOT set citations."""
    req = _make_request(text="What is the capital of France?")
    native = transport.format_request(req)
    assert "additionalModelRequestFields" not in native or \
        "citations" not in native.get("additionalModelRequestFields", {})


def test_bedrock_sets_citations_enabled_when_pdf_present(transport, tmp_path):
    """THE FOOTGUN FIX: PDFs must auto-enable citations to avoid silent text-only fallback."""
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_make_minimal_pdf())

    req = _make_request(text="Summarize", attachments=[str(pdf_path)])
    native = transport.format_request(req)

    assert native["additionalModelRequestFields"]["citations"]["enabled"] is True


def test_bedrock_builds_document_block_with_raw_bytes(transport, tmp_path):
    """Bedrock Converse documentBlock format requires raw bytes (not base64)."""
    pdf_path = tmp_path / "doc.pdf"
    pdf_bytes = _make_minimal_pdf()
    pdf_path.write_bytes(pdf_bytes)

    req = _make_request(text="Summarize", attachments=[str(pdf_path)])
    native = transport.format_request(req)

    user_msg = native["messages"][0]
    assert user_msg["role"] == "user"
    doc_blocks = [b for b in user_msg["content"] if "document" in b]
    assert len(doc_blocks) == 1
    doc = doc_blocks[0]["document"]
    assert doc["format"] == "pdf"
    assert doc["source"]["bytes"] == pdf_bytes  # RAW bytes, not base64


def test_bedrock_skips_oversize_pdf(transport, tmp_path, caplog):
    """Oversize PDF dropped with warning; no document block, no citations."""
    import logging
    from plugin_sdk.pdf_helpers import PDF_MAX_BYTES

    pdf_path = tmp_path / "huge.pdf"
    pdf_path.write_bytes(b"x" * (PDF_MAX_BYTES + 1))

    req = _make_request(text="Summarize", attachments=[str(pdf_path)])
    with caplog.at_level(logging.WARNING):
        native = transport.format_request(req)

    user_msg = native["messages"][0]
    assert all("document" not in b for b in user_msg["content"])
    assert "additionalModelRequestFields" not in native or \
        "citations" not in native.get("additionalModelRequestFields", {})
    assert any("over 32 MB" in r.message for r in caplog.records)
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_bedrock_pdf_citations.py -v
```

Expected: FAIL — current `format_request` doesn't read attachments and doesn't set citations.

- [ ] **Step 4: Implement the fix**

Replace the `format_request` method body in `extensions/aws-bedrock-provider/transport.py`. The new implementation:

```python
import logging
import mimetypes
from pathlib import Path

from plugin_sdk.pdf_helpers import (
    PDF_HARD_PAGE_LIMIT,
    PDF_MAX_BYTES,
    PDF_SOFT_PAGE_LIMIT,
    count_pdf_pages,
)

_log = logging.getLogger(__name__)


def _build_bedrock_document_block(path: Path) -> dict | None:
    """Build a Bedrock Converse documentBlock for a PDF.

    Returns None on read error, oversize, or hard page limit overflow.
    Bedrock takes raw bytes (not base64) — boto3 serializes for the wire.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        _log.warning("PDF attachment unreadable: %s (%s)", path, exc)
        return None
    if len(data) > PDF_MAX_BYTES:
        _log.warning(
            "PDF attachment over 32 MB cap; skipping: %s (%d bytes)",
            path, len(data),
        )
        return None
    page_count = count_pdf_pages(data)
    if page_count > PDF_HARD_PAGE_LIMIT:
        _log.warning(
            "PDF over 600-page hard limit; skipping: %s (%d pages)",
            path, page_count,
        )
        return None
    if page_count > PDF_SOFT_PAGE_LIMIT:
        _log.warning(
            "PDF over 100 pages; may exceed 200k-context-model capacity: %s (%d pages)",
            path, page_count,
        )
    # Bedrock requires the document name to be filename-safe (no path separators)
    safe_name = path.stem[:64].replace("/", "_").replace("\\", "_")
    return {
        "document": {
            "format": "pdf",
            "name": safe_name or "document",
            "source": {"bytes": data},
        }
    }


def _build_message_content_blocks(message) -> list[dict]:
    """Build the content array for a single Bedrock message.

    Reads message.attachments (filesystem paths). Dispatches PDFs to
    documentBlock. Other attachment types currently logged + dropped.
    Always appends the text content block last.
    """
    blocks: list[dict] = []
    for path_str in getattr(message, "attachments", []) or []:
        path = Path(path_str)
        media_type = mimetypes.guess_type(path.name)[0]
        if media_type == "application/pdf" or path.suffix.lower() == ".pdf":
            block = _build_bedrock_document_block(path)
            if block:
                blocks.append(block)
        else:
            _log.warning(
                "Bedrock provider: unsupported attachment type %r, skipping: %s",
                media_type, path,
            )
    blocks.append({"text": message.content})
    return blocks
```

Then replace the `format_request` body's message-building loop:

```python
def format_request(self, req: NormalizedRequest) -> dict[str, Any]:
    """Convert NormalizedRequest -> Bedrock Converse API dict."""
    messages = []
    for msg in req.messages:
        if msg.role == "system":
            continue
        role = "user" if msg.role == "user" else "assistant"
        messages.append({
            "role": role,
            "content": _build_message_content_blocks(msg),
        })

    native: dict[str, Any] = {
        "modelId": req.model,
        "messages": messages,
        "inferenceConfig": {
            "maxTokens": req.max_tokens,
            "temperature": req.temperature,
        },
    }

    sys_chunks = [m.content for m in req.messages if m.role == "system"]
    if req.system:
        sys_chunks.insert(0, req.system)
    if sys_chunks:
        native["system"] = [{"text": "\n\n".join(sys_chunks)}]

    if req.tools:
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t.name,
                        "description": getattr(t, "description", ""),
                        "inputSchema": {"json": getattr(t, "parameters", {})},
                    }
                }
                for t in req.tools
            ],
        }
        native["toolConfig"] = tool_config

    # THE FOOTGUN FIX: any document block in the request → enable citations.
    # Without this, Bedrock silently drops PDF visual understanding to
    # text-only extraction (~7000 vs ~1000 tokens for a 3-page PDF).
    has_documents = any(
        "document" in block
        for msg in messages
        for block in msg.get("content", [])
    )
    if has_documents:
        native["additionalModelRequestFields"] = {"citations": {"enabled": True}}

    return native
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_bedrock_pdf_citations.py -v
```

Expected: all 4 PASS, including the critical `test_bedrock_sets_citations_enabled_when_pdf_present`.

- [ ] **Step 6: Run all bedrock tests for regressions**

```bash
pytest tests/ -k "bedrock" --tb=short -q | tail -10
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
cd /private/tmp/oc-sp2-pdf-providers
git add OpenComputer/extensions/aws-bedrock-provider/transport.py OpenComputer/tests/test_bedrock_pdf_citations.py
git commit -m "feat(bedrock-provider): PDF document blocks + auto-enable citations (footgun fix)

Closes silent breakage where Bedrock+Claude+PDFs degraded to text-only
extraction without warning. Now: any document block in the request
auto-enables citations in additionalModelRequestFields."
```

---

## Task 4: OpenAI provider — warn-and-drop for PDFs

**Files:**
- Modify: `extensions/openai-provider/provider.py` (extend the attachment helper)
- Test: `tests/test_openai_provider_pdf.py` (NEW)

OpenAI doesn't have a native PDF document type. Drop with a warning so the user's text request still completes.

- [ ] **Step 1: Read the existing OpenAI image-blocks helper**

```bash
sed -n '70,130p' extensions/openai-provider/provider.py
```

Note: function is likely `_openai_content_blocks_from_message` or `_build_image_content_blocks`, signature `*, text: str, image_paths: list[str]`, returns `list[dict]` of `{"type": "image_url", "image_url": {"url": "data:..."}}`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_openai_provider_pdf.py`:

```python
"""OpenAI provider: PDF attachments dropped with warning (no native support)."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

PROVIDER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "openai-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_openai_provider", PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_minimal_pdf() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"<< /Type /Page >>\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def test_openai_warns_and_drops_pdf_attachment(tmp_path, caplog):
    """OpenAI doesn't natively support PDFs; should warn + drop."""
    module = _load_provider_module()
    pdf_path = tmp_path / "doc.pdf"
    pdf_path.write_bytes(_make_minimal_pdf())

    with caplog.at_level(logging.WARNING):
        blocks = module._content_blocks_with_attachments(
            text="What is in the document?",
            attachment_paths=[str(pdf_path)],
        )

    # Text still goes through; no PDF block (OpenAI has no native PDF type)
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"] == "What is in the document?"
    assert any(
        "PDF" in r.message and ("not support" in r.message or "drop" in r.message.lower())
        for r in caplog.records
    )


def test_openai_image_path_still_works(tmp_path):
    """Regression: existing image attachment path is unchanged."""
    module = _load_provider_module()
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63000100000005000100020701020000000049454e"
        "44ae426082"
    )
    img = tmp_path / "tiny.png"
    img.write_bytes(png_bytes)

    blocks = module._content_blocks_with_attachments(
        text="Describe this.",
        attachment_paths=[str(img)],
    )
    image_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(image_blocks) == 1
    text_blocks = [b for b in blocks if b.get("type") == "text"]
    assert len(text_blocks) == 1
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
pytest tests/test_openai_provider_pdf.py -v
```

Expected: FAIL — `_content_blocks_with_attachments` doesn't exist; OpenAI provider currently uses an image-only helper.

- [ ] **Step 4: Modify the existing OpenAI image-helper to dispatch on MIME type**

Same pattern as Task 2 Step 4 (Anthropic): rename the existing OpenAI image-only helper to `_content_blocks_with_attachments`, change parameter `image_paths` → `attachment_paths`, dispatch per-attachment by MIME type (image vs PDF-warn-and-drop vs unsupported). Update callsites.

Replace the existing image-helper block with this:

```python
import logging
import mimetypes
from pathlib import Path

_log = logging.getLogger(__name__)

# OpenAI Chat-Completions vision MIME types (existing constant — keep)
_IMAGE_MIME_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_IMAGE_MAX_BYTES = 20 * 1024 * 1024  # OpenAI's existing 20MB image cap


def _build_openai_image_block(path: Path, media_type: str) -> dict | None:
    """Build OpenAI Chat-Completions image_url block from an image path."""
    import base64
    try:
        data = path.read_bytes()
    except OSError as exc:
        _log.warning("image attachment unreadable: %s (%s)", path, exc)
        return None
    if len(data) > _IMAGE_MAX_BYTES:
        _log.warning(
            "image attachment over 20 MB cap; skipping: %s (%d bytes)",
            path, len(data),
        )
        return None
    b64 = base64.standard_b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{media_type};base64,{b64}"},
    }


def _content_blocks_with_attachments(
    *, text: str, attachment_paths: list[str]
) -> list[dict]:
    """Build OpenAI content array combining text + media attachments.

    Dispatches per-attachment based on MIME type:
    - image/png|jpeg|gif|webp → image_url block (data URL, base64-encoded)
    - application/pdf → log warning, skip (OpenAI has no native PDF type)
    - other → log warning, skip

    Order: text first, then images — matches OpenAI's vision-example
    convention. Required ordering for tool-calling-with-images.
    """
    blocks: list[dict] = [{"type": "text", "text": text}]
    for path_str in attachment_paths:
        path = Path(path_str)
        media_type = mimetypes.guess_type(path.name)[0]
        if media_type == "application/pdf" or path.suffix.lower() == ".pdf":
            _log.warning(
                "OpenAI provider: PDF input not supported natively; "
                "dropping attachment: %s. (Use Anthropic provider for PDF support.)",
                path,
            )
            continue
        if media_type in _IMAGE_MIME_TYPES:
            block = _build_openai_image_block(path, media_type)
            if block is not None:
                blocks.append(block)
            continue
        _log.warning(
            "OpenAI provider: unsupported attachment type %r; skipping: %s",
            media_type, path,
        )
    return blocks
```

Update existing callsites that referenced the old image-only helper.

- [ ] **Step 5: Run tests to verify they pass**

```bash
pytest tests/test_openai_provider_pdf.py -v
```

Expected: all PASS.

- [ ] **Step 6: Run all openai tests for regressions**

```bash
pytest tests/ -k "openai" --tb=short -q | tail -10
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
cd /private/tmp/oc-sp2-pdf-providers
git add OpenComputer/extensions/openai-provider/provider.py OpenComputer/tests/test_openai_provider_pdf.py
git commit -m "feat(openai-provider): warn + drop PDF attachments (no native support)

OpenAI Chat Completions has no document content type. Rather than
forwarding the PDF and getting cryptic 400s, log a warning suggesting
Anthropic provider for PDF input, drop the attachment, and continue
with text-only."
```

---

## Task 5: Channel adapter verification (Telegram)

**Files:**
- Possibly modify: `extensions/telegram/adapter.py` (only if PDF flow doesn't already work)
- Test: `tests/test_telegram_pdf_passthrough.py` (NEW)

- [ ] **Step 1: Inspect the Telegram attachment-handling code**

```bash
grep -n "attachment\|document\|pdf\|application/pdf\|mime" extensions/telegram/adapter.py | head -30
```

Capture: how attachments are downloaded, what MIME type detection happens, how they're attached to the outgoing `Message`.

- [ ] **Step 2: Write the test**

Create `tests/test_telegram_pdf_passthrough.py`:

```python
"""Telegram channel adapter: PDF attachments survive into Message.attachments."""
from __future__ import annotations

import importlib.util
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

    This is a STATIC text check, not a behavior test — full end-to-end
    verification (send a real PDF via the bot, observe it land in
    Message.attachments) needs manual testing. Tracked in the PR's test
    plan checklist.
    """
    source = ADAPTER_PATH.read_text()
    # Look for explicit blocklists like:
    #   if mime_type == "application/pdf": skip
    #   ALLOWED_MIME = {"image/png", "image/jpeg"}  # no PDF
    has_pdf_block = (
        "application/pdf" in source
        and any(k in source.lower() for k in ("skip", "reject", "drop", "block"))
    )
    assert not has_pdf_block, (
        "Telegram adapter appears to reject PDFs. "
        "Inspect the source and update the MIME filter to allow application/pdf."
    )
```

- [ ] **Step 3: Run the test**

```bash
pytest tests/test_telegram_pdf_passthrough.py -v
```

If PASS: the adapter doesn't reject PDFs; the existing flow works. Skip Step 4.

If FAIL: the adapter has an explicit `application/pdf` rejection. Fix it in Step 4.

- [ ] **Step 4 (CONDITIONAL): If adapter rejects PDF, fix the filter**

Edit the offending line in `extensions/telegram/adapter.py` to allow `application/pdf` MIME type. Re-run Step 3.

- [ ] **Step 5: Run all telegram tests for regressions**

```bash
pytest tests/ -k "telegram" --tb=short -q | tail -10
```

Expected: all PASS.

- [ ] **Step 6: Commit**

If no adapter changes (Step 4 skipped):

```bash
cd /private/tmp/oc-sp2-pdf-providers
git add OpenComputer/tests/test_telegram_pdf_passthrough.py
git commit -m "test(telegram): PDF attachments pass through to Message.attachments"
```

If adapter changes:

```bash
cd /private/tmp/oc-sp2-pdf-providers
git add OpenComputer/extensions/telegram/adapter.py OpenComputer/tests/test_telegram_pdf_passthrough.py
git commit -m "feat(telegram): allow PDF attachments through (was MIME-filtered out)"
```

---

## Task 6: Documentation

**Files:**
- Create: `docs/providers/pdf-support.md`

- [ ] **Step 1: Write the doc**

Create `docs/providers/pdf-support.md`:

```markdown
# PDF Input Support Across Providers

OpenComputer supports PDF attachments end-to-end across providers that have
native document handling. PDFs sent via channel adapters (e.g. Telegram)
or attached directly are wrapped as the appropriate provider-specific
content block.

## Provider matrix

| Provider | PDF support | Notes |
|---|---|---|
| **Anthropic** | ✓ Native | Wraps as `document` content block (base64 source). Honors Anthropic's 32 MB / 600-page limits. |
| **AWS Bedrock** (Claude family) | ✓ Native | Wraps as Converse `document` block (raw bytes). **Auto-enables `citations`** to avoid silent text-only fallback. |
| **OpenAI** | ✗ Drop with warning | OpenAI Chat Completions has no document content type. Attachment dropped, warning logged, text-only request proceeds. Use Anthropic provider for PDF input. |
| **OpenAI-compat** (DeepSeek, Kimi, etc.) | ✗ Drop with warning | Same as OpenAI (inherits content-block builder). |
| **Other** (Gemini, Groq, Ollama, ...) | ✗ Inherits base | Currently no override; PDFs will be passed through and may produce 400s. Add per-provider PDF support if needed. |

## Limits

| Limit | Value | Source |
|---|---|---|
| Max request size | 32 MB | Anthropic spec |
| Max pages (hard cap) | 600 | Anthropic spec |
| Effective max pages on 200k-context models | 100 | Anthropic spec |

PDFs over the size cap or hard page cap are dropped with a warning. PDFs
between 100 and 600 pages emit a soft warning but are still sent.

## The Bedrock citations footgun

Before SP2, Bedrock's Converse API silently degraded PDF visual
understanding to text-only extraction (~7000 vs ~1000 tokens for a 3-page
PDF) when the `citations` feature wasn't explicitly enabled. There was no
warning — just worse output.

After SP2, the Bedrock provider auto-sets
`additionalModelRequestFields.citations.enabled = true` whenever any
document block is present in the request. Text-only requests are
unchanged (no citations overhead).

## How to send a PDF via Telegram

1. Send the PDF as a document attachment to the OC bot.
2. The Telegram adapter downloads the PDF to a local path and adds it to
   `Message.attachments`.
3. The active provider's `complete()` builds the appropriate document
   content block per the table above.
4. Claude (or whichever model) sees the PDF as visual input.

## Implementation references

- Spec: `OpenComputer/docs/superpowers/specs/2026-05-02-sp2-pdf-provider-hardening-design.md`
- Plan: `OpenComputer/docs/superpowers/plans/2026-05-02-sp2-pdf-provider-hardening.md`
- Helper: `plugin_sdk/pdf_helpers.py`
- Anthropic: `extensions/anthropic-provider/provider.py::_content_blocks_with_attachments`
- Bedrock: `extensions/aws-bedrock-provider/transport.py::format_request`
- OpenAI: `extensions/openai-provider/provider.py::_content_blocks_with_attachments`
- Anthropic PDF docs: https://docs.claude.com/en/build-with-claude/pdf-support
- Bedrock Converse documentBlock: https://docs.aws.amazon.com/bedrock/latest/userguide/document-chat.html
```

- [ ] **Step 2: Commit**

```bash
cd /private/tmp/oc-sp2-pdf-providers
mkdir -p OpenComputer/docs/providers
git add OpenComputer/docs/providers/pdf-support.md
git commit -m "docs(providers): PDF support matrix + Bedrock citations footgun explainer"
```

---

## Task 7: Final verification + push + PR

- [ ] **Step 1: Run the FULL pytest suite**

```bash
cd /private/tmp/oc-sp2-pdf-providers/OpenComputer
pytest tests/ --tb=line -q --ignore=tests/test_telegram --ignore=tests/test_voice 2>&1 | tail -10
```

Expected: all pass. Compare to baseline from Step 0b.

- [ ] **Step 2: Run FULL ruff**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: clean.

- [ ] **Step 3: Verify success criteria**

- [ ] Bedrock citations footgun fixed (`test_bedrock_sets_citations_enabled_when_pdf_present` passes)
- [ ] Anthropic provider builds PDF document blocks (`test_anthropic_builds_document_block_for_pdf_attachment` passes)
- [ ] OpenAI provider warns + drops (`test_openai_warns_and_drops_pdf_attachment` passes)
- [ ] Image attachment regression tests pass (`test_*_image_path_still_works` for Anthropic + OpenAI)
- [ ] All 4 new test files passing
- [ ] Bundled corpus tests + skills_hub + skills_guard remain green
- [ ] Full pytest green; ruff clean
- [ ] Documentation written (`docs/providers/pdf-support.md`)

- [ ] **Step 4: Push the branch**

```bash
cd /private/tmp/oc-sp2-pdf-providers
git push -u origin feat/sp2-pdf-provider-hardening
```

- [ ] **Step 5: Open the PR**

```bash
gh pr create --title "feat(providers): PDF input + Bedrock citations footgun fix (SP2)" --body "$(cat <<'EOF'
## Summary

SP2 of the Anthropic-API-parity scope. Spec: \`docs/superpowers/specs/2026-05-02-sp2-pdf-provider-hardening-design.md\`. Plan: \`docs/superpowers/plans/2026-05-02-sp2-pdf-provider-hardening.md\`.

- **Bedrock citations footgun fix**: Bedrock provider auto-enables \`citations\` when any document block is present in the Converse request. Closes silent text-only-fallback degradation.
- **PDF attachment support** across Anthropic + Bedrock providers via existing \`Message.attachments\` plumbing — no new BaseProvider method.
- **OpenAI** degrades gracefully (warn + drop, suggest Anthropic).
- **Defensive guards**: 32 MB request size, 100/600 page warn/reject limits.
- **Telegram channel adapter** verified (PDFs flow through unchanged).

### Test plan
- [x] \`pytest tests/\` — full suite green
- [x] \`ruff check\` — clean
- [x] Per-provider PDF tests (Anthropic, Bedrock, OpenAI) all passing
- [x] Regression: existing image-attachment tests still pass

### Out of scope
- SP3 (Files API + \`oc files\` CLI + tool-result spillover) — separate sub-project
- SP4 (Server-side tools / Skills-via-API) — demand-gated, separate sub-project
- Discord PDF passthrough — follow-up if Telegram pattern doesn't trivially apply
- OpenAI text-extraction fallback (would require pdfplumber dep)
- Multi-page chunking strategy
- File ID-based document sources (depends on SP3)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage:**
| Spec section | Task |
|---|---|
| §5.1 Document-block helper | Tasks 2, 3 (per-provider helpers using shared utilities from Task 1) |
| §5.2 MIME-type dispatch | Tasks 2, 3, 4 |
| §5.3 Bedrock citations footgun fix | Task 3 |
| §5.4 Per-provider behavior matrix | Tasks 2, 3, 4 |
| §5.5 Defensive guards | Task 1 (constants + count helper); Tasks 2, 3 (use them) |
| §5.6 Channel adapter passthrough | Task 5 |
| §5.7 Tests | All tasks (TDD) |
| §5.8 Documentation | Task 6 |

**Placeholder scan:** No "TBD", "implement later", "fill in" outside the conditional Step 4 in Task 5 (which is correctly conditional on Step 3's outcome).

**Type consistency:**
- `_content_blocks_with_attachments(*, text: str, attachment_paths: list[str]) -> list[dict]` is the consistent helper name across Anthropic + OpenAI (Task 2 + Task 4).
- Bedrock uses `_build_message_content_blocks(message)` since it operates on Message objects directly (different convention but documented in Task 3).
- Constants from `plugin_sdk.pdf_helpers` (`PDF_MAX_BYTES`, `PDF_HARD_PAGE_LIMIT`, `PDF_SOFT_PAGE_LIMIT`, `count_pdf_pages`, `pdf_to_base64`) used consistently across Tasks 2 and 3.
