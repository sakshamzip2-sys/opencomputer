# SP2 — PDF + Provider Hardening — Design

**Date:** 2026-05-02
**Status:** approved (auto-mode brainstorm)
**Sub-project:** SP2 of the Anthropic-API-parity scope (C)
**Authors:** Saksham + Claude Code (Opus 4.7)

---

## 1. Context

After SP1 shipped (PR #354), the OpenComputer skill subsystem is Anthropic-spec compliant. Two provider-layer gaps remain from the Anthropic-API-parity audit:

1. **Bedrock citations footgun** ([`extensions/aws-bedrock-provider/transport.py:63`](../../../extensions/aws-bedrock-provider/transport.py)): `format_request()` builds `[{"text": msg.content}]` content blocks unconditionally. Anthropic Bedrock's Converse API silently degrades PDF visual understanding to text-only extraction (~7000 vs ~1000 tokens for a 3-page PDF) when `citations` is not enabled. **No warning, no error — just worse output.**

2. **Zero PDF awareness across providers**: The Anthropic provider builds image content blocks (5 MB cap) for `Message.attachments` but treats `.pdf` files like any other image. They get base64-encoded and rejected by the server with cryptic 400s. OpenAI provider same story (20 MB image cap). Bedrock has no attachment handling at all (text-only `format_request`).

Recent provider-agnostic infrastructure already in main makes SP2 cleaner:
- PR #351 established the `VisionUnsupportedError + BaseProvider.complete_vision()` pattern for unsupported-multimodal-input cases.
- PR #350 introduced `BatchUnsupportedError` for the same reason.
- `Message.attachments: list[str]` (paths) is already plumbed through the agent loop.

## 2. Goals

1. **Fix the Bedrock citations footgun.** Documents → `citations: enabled` automatically.
2. **Enable PDF attachments** to work end-to-end: user attaches a PDF in a channel → provider includes it in the API request as a `document` content block.
3. **Provider-portable behavior**: Anthropic and Bedrock support PDFs natively; OpenAI degrades gracefully with a logged warning (no crash, no silent corruption).
4. **Defensive guards**: 32 MB request size, soft warn at 100 pages, hard reject at 600 pages — matching Anthropic's published limits.
5. **Channel-adapter passthrough** for at least Telegram (Discord follows the same pattern; defer to follow-up if it's not trivial).

## 3. Non-goals

- **No new BaseProvider method** like `complete_with_documents`. The existing `Message.attachments` plumbing already handles multimodal payloads — this just extends it to a new MIME type. Avoiding a new provider method keeps the surface small and lets existing tests/calls work unchanged.
- **No Files API integration** (`file_id` source for documents) — that's SP3's territory.
- **No multi-page chunking strategy** — if a PDF exceeds the page limit, reject at the boundary; chunking is a feature, not a fix.
- **No OpenAI text-extraction fallback** — would require pdfplumber as a dep; deferred until concrete demand.
- **No Discord PDF passthrough in this PR** if it's non-trivial — track as follow-up. Telegram is the proof-of-concept channel.
- **No DocumentUnsupportedError** exception type. The OpenAI degradation is a warning, not an exception (the user's request still completes with text-only).

## 4. Approach

Mirror the existing image-attachment pattern. Each provider's `complete()` method receives `Message.attachments: list[str]`. Today they build image content blocks. After SP2 they:

- Detect MIME type per attachment (existing `mimetypes.guess_type` pattern)
- For images: existing image-block path (unchanged)
- For PDFs: new document-block path
- For other types: log + skip (existing behavior)

The Bedrock fix is folded in: when document blocks are present, also set `citations.enabled = true` in the Converse API request.

This is composition over invention — we add a small helper for document blocks and route attachments by MIME type. No new exceptions, no new provider methods, no churn in the agent loop.

## 5. Design

### 5.1 Document-block helper (per-provider, mirroring image helpers)

Each provider already has a `_build_<provider>_content_blocks` helper for images. Add an analogous `_build_pdf_block` (or extend the existing image helper to dispatch by MIME type).

**Anthropic shape** ([Anthropic PDF docs](https://docs.claude.com/en/build-with-claude/pdf-support)):

```python
{
    "type": "document",
    "source": {
        "type": "base64",
        "media_type": "application/pdf",
        "data": "<base64 string>",
    },
}
```

**Bedrock Converse shape** ([AWS Bedrock Converse PDF docs](https://docs.aws.amazon.com/bedrock/latest/userguide/document-chat.html)):

```python
{
    "document": {
        "format": "pdf",
        "name": "<sanitized filename>",
        "source": {"bytes": <raw bytes>},
    }
}
```

(Note: Bedrock takes raw bytes, not base64. AWS SDK serializes it.)

**OpenAI shape**: no native PDF support. The provider logs a warning and skips the attachment. Plain-text user message still goes through.

### 5.2 MIME-type dispatch

In each provider's existing attachment-handling helper:

```python
def _build_content_blocks(*, text: str, attachment_paths: list[str]) -> list[dict]:
    blocks = []
    for path_str in attachment_paths:
        path = Path(path_str)
        media_type = mimetypes.guess_type(path.name)[0]
        if media_type == "application/pdf":
            block = _build_pdf_block(path)
            if block:
                blocks.append(block)
        elif media_type in IMAGE_MEDIA_TYPES:
            block = _build_image_block(path, media_type)
            if block:
                blocks.append(block)
        else:
            log.warning("attachment has unsupported MIME type %r; skipping: %s", media_type, path)
    blocks.append({"type": "text", "text": text})
    return blocks
```

(Order: media first, then text — matches existing convention from [provider.py:94](../../../extensions/anthropic-provider/provider.py).)

### 5.3 Bedrock citations footgun fix

In [`extensions/aws-bedrock-provider/transport.py`](../../../extensions/aws-bedrock-provider/transport.py)'s `format_request()`:

1. Replace the unconditional `[{"text": msg.content}]` with a content-block builder that handles attachments.
2. When ANY document block is present in the final request, set `additionalModelRequestFields.citations.enabled = True` in the Converse API request body.

```python
# After building messages with potential document blocks:
has_documents = any(
    "document" in block
    for msg in messages
    for block in msg.get("content", [])
)
if has_documents:
    native["additionalModelRequestFields"] = {"citations": {"enabled": True}}
```

This is THE fix for the silent-degradation bug.

### 5.4 Per-provider behavior matrix

| Provider | PDF attachment behavior |
|---|---|
| **Anthropic** | Build `document` content block with base64 source. Reject if file >32MB. |
| **Bedrock** | Build Converse `document` block with raw bytes. Set `citations.enabled=true`. |
| **OpenAI** | Log warning ("OpenAI does not support PDF input natively; attachment dropped: <name>"). Continue with text-only. |
| **OpenAI-compat** (DeepSeek, Kimi, etc. — inherit from OpenAI) | Same as OpenAI: warn + drop. |
| **Other** (Gemini, Groq, Ollama, ...) | Inherit base provider behavior. If they don't override, attachments are silently passed through and providers either handle or 400. **Out of scope: per-provider PDF support beyond Anthropic/Bedrock.** |

### 5.5 Defensive guards (in the helper)

```python
PDF_MAX_BYTES = 32 * 1024 * 1024  # 32 MB — Anthropic's request limit
PDF_HARD_PAGE_LIMIT = 600          # Anthropic's hard cap
PDF_SOFT_PAGE_LIMIT = 100          # 200k-context-model effective cap

def _build_pdf_block_anthropic(path: Path) -> dict | None:
    try:
        data = path.read_bytes()
    except OSError as exc:
        log.warning("PDF attachment unreadable: %s (%s)", path, exc)
        return None
    if len(data) > PDF_MAX_BYTES:
        log.warning(
            "PDF attachment over 32 MB cap; skipping: %s (%d bytes)",
            path, len(data),
        )
        return None
    page_count = _count_pdf_pages(data)
    if page_count > PDF_HARD_PAGE_LIMIT:
        log.warning(
            "PDF over 600-page limit; skipping: %s (%d pages)",
            path, page_count,
        )
        return None
    if page_count > PDF_SOFT_PAGE_LIMIT:
        log.warning(
            "PDF over 100 pages; may exceed 200k-context-model capacity: %s (%d pages)",
            path, page_count,
        )
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(data).decode("ascii"),
        },
    }


def _count_pdf_pages(data: bytes) -> int:
    """Cheap page count via PDF object stream parsing.

    Counts `/Type /Page` markers in the raw bytes (not in /Pages catalogs).
    Approximate but doesn't require pypdf as a dep. Returns -1 if uncertain
    (no guard fires for unknown counts).
    """
    try:
        return data.count(b"/Type /Page") - data.count(b"/Type /Pages")
    except Exception:
        return -1
```

The page-count helper deliberately avoids adding `pypdf`/`pdfplumber` as a dep. Crude byte-counting is good enough for the soft/hard page limits — we're trying to catch obvious over-limit cases, not produce a precise count.

### 5.6 Channel adapter passthrough — Telegram

Telegram's existing message handler already downloads attachments to local paths and adds them to `Message.attachments`. **For PDF, no change should be needed** — the path flows through transparently to the provider.

Verification step in execution: send a PDF via Telegram bot, confirm provider builds the document block. If the channel adapter strips PDFs (e.g., MIME-type filter), update.

### 5.7 Tests

| File | Tests |
|---|---|
| `tests/test_anthropic_provider_pdf.py` (NEW) | `test_anthropic_builds_document_block_for_pdf_attachment`, `test_anthropic_rejects_oversize_pdf`, `test_anthropic_warns_over_100_page_pdf`, `test_anthropic_skips_unreadable_pdf` |
| `tests/test_bedrock_pdf_citations.py` (NEW) | `test_bedrock_sets_citations_enabled_when_pdf_present`, `test_bedrock_no_citations_when_text_only`, `test_bedrock_builds_document_block_with_raw_bytes` |
| `tests/test_openai_provider_pdf.py` (NEW) | `test_openai_warns_and_skips_pdf_attachment`, `test_openai_continues_text_only_when_pdf_dropped` |
| `tests/test_pdf_helpers.py` (NEW) | `test_count_pdf_pages_known_3_page_pdf`, `test_count_pdf_pages_returns_negative_for_garbage` |

Use synthetic PDF bytes built inline in tests (smallest valid PDF is ~250 bytes). No real-file fixtures.

### 5.8 Documentation

Add a section to `docs/skills/AUTHORING.md`-style location: `docs/providers/pdf-support.md` (NEW) explaining:
- Which providers support PDFs (Anthropic ✓, Bedrock ✓, OpenAI ✗-warns)
- Size/page limits
- How to send a PDF via channel adapter (Telegram works; Discord follows-up)
- The Bedrock citations footgun and how the auto-enable now prevents it

## 6. Decisions log

| Decision | Why |
|---|---|
| Extend `Message.attachments` rather than add `complete_with_documents()` | Mirrors how images already flow; avoids a new provider method; keeps surface small |
| OpenAI degrades to warn-and-drop, not raise | User's request still completes with text-only; less hostile UX than `DocumentUnsupportedError` |
| Bedrock auto-enables `citations` only when documents present | Don't pay the citations overhead for text-only requests |
| Crude byte-counting page heuristic, no `pypdf` dep | Page-count is for guard rails, not analysis; precision not needed |
| Telegram only for channel passthrough; Discord deferred | Most-used channel; if Discord follows the same shape (it likely does), follow-up PR is one line |
| No NormalizedRequest schema change for Bedrock | Existing path uses `m.content: str`; we extend the format_request to also read attachments. NormalizedRequest itself is unchanged. |

## 7. Risks

1. **Bedrock document format may differ across model families.** Claude on Bedrock supports it. Other Bedrock models may not. Mitigation: test against Claude on Bedrock; document the limitation.
2. **The page-count heuristic is approximate.** A weird PDF with `/Type/Page` in a content stream could over-count. Mitigation: only triggers warnings (not hard rejects) at the soft limit; hard limit is generous (600).
3. **Telegram document downloads have their own MIME-type detection.** If the adapter doesn't preserve `.pdf` extension or sets a wrong MIME type, our dispatch fails. Mitigation: dispatch on extension fallback, not just MIME.
4. **The `additionalModelRequestFields.citations` knob may not exist on older Bedrock SDK versions.** Mitigation: try/except around the field set; log if API rejects.

## 8. Open questions

None — all design decisions resolved.

## 9. Success criteria

- [ ] Bedrock citations footgun fixed: when ANY document block is in the request, `citations.enabled=true` is set.
- [ ] Anthropic provider builds proper PDF document blocks; image attachments still work unchanged.
- [ ] OpenAI provider logs warning + drops PDF attachments without raising.
- [ ] All 4 new test files passing.
- [ ] Bundled corpus tests + skills_hub + skills_guard remain green.
- [ ] Full pytest suite green; ruff clean.
- [ ] Documentation written.

## 10. Out of scope (later sub-projects)

- **SP3** — Files API + `oc files` CLI + tool-result spillover.
- **SP4** — Server-side tools / Skills-via-API.
- Discord PDF passthrough (follow-up if Telegram pattern doesn't trivially apply).
- OpenAI text-extraction fallback for PDFs (requires `pdfplumber` dep).
- Multi-page chunking strategy (split a large PDF across multiple requests).
- File ID-based document sources (depends on SP3).
- Per-provider PDF support beyond Anthropic/Bedrock.
- Sending PDFs from the agent (output) — only PDF input is in scope.

## 11. References

- [Anthropic PDF support](https://docs.claude.com/en/build-with-claude/pdf-support)
- [Bedrock Converse documentBlock](https://docs.aws.amazon.com/bedrock/latest/userguide/document-chat.html)
- PR #351 (`VisionUnsupportedError + complete_vision`) — the pattern this builds on
- PR #350 (`BatchUnsupportedError + submit_batch`) — same pattern, sibling
- SP1 design: `2026-05-02-skill-spec-compliance-design.md`
- SP1 plan: `2026-05-02-skill-spec-compliance.md`
