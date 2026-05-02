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
