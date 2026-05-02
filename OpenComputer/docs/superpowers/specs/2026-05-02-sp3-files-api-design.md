# SP3 — Anthropic Files API client + `oc files` CLI — Design

**Date:** 2026-05-02
**Status:** approved (auto-mode brainstorm)
**Sub-project:** SP3 of the Anthropic-API-parity scope (C)
**Authors:** Saksham + Claude Code (Opus 4.7)

---

## 1. Context

After SP1 (PR #354) and SP2 (PR #359 in flight), two of three remaining items from the broader scope are addressed. The original SP3 scope was:

- ❌ Files API client wrapper — not wired
- ❌ `oc files` CLI — not exists
- ✅ Tool-result spillover — **already fully wired** (per survey: `opencomputer/agent/tool_result_storage.py` exists, `loop.py:3280-3288` actively calls it, `vision_analyze.py:78` reads from it). **DROPPED from SP3 scope.**
- ⏭️ Skills+code-exec output download path — depends on SP4 server-side skills; deferred to SP4 territory

This narrows SP3 to: a clean Anthropic Files API integration with a CLI surface for management.

## 2. Why now (honest framing)

OC is local-first; the filesystem already IS the file store for most workflows. The Files API helps two specific scenarios:

1. **Multi-turn document conversations.** Today (after SP2), every turn re-uploads the PDF as base64 in the request body. With Files API, upload once + reference by `file_id` across turns. For a 30MB PDF over 10 turns: ~300MB transfer → ~30MB.
2. **Power-user file management.** `oc files list` and `oc files delete` for explicit control of what's uploaded to your Anthropic workspace. Cleanup-first design (the alternative is files accumulating forever in the workspace).

This sub-project does NOT auto-integrate with SP2's PDF blocks. That integration is a small follow-up PR after both SP2 and SP3 merge (literally: "in `_build_pdf_block`, check the file_id cache first"). Keeping SP3 standalone avoids cross-PR coupling.

## 3. Goals

1. Provide a typed, async `FilesClient` for Anthropic's Files API ([`docs.claude.com/en/build-with-claude/files`](https://docs.claude.com/en/build-with-claude/files)): upload, list, get-metadata, download, delete.
2. Expose CLI: `oc files upload <path> | list | delete <id> | download <id> <out>`.
3. Set the right beta header (`files-api-2025-04-14`).
4. Honest error handling: clear messages on rate limits (~100 req/min beta), storage quota, file-too-large (500 MB), file-not-found.
5. Persistence and inspection: list output shows uploaded files with size + creation date.

## 4. Non-goals

- **No auto-caching of attachments.** That's a follow-up after SP2 merges. SP3 doesn't touch the provider's content-block builders.
- **No multi-provider abstraction.** Files API is Anthropic-specific; there's no equivalent on OpenAI/Bedrock for the same use case. If other providers add one later, that's a separate design.
- **No `oc files cleanup --days N` command.** YAGNI; manual `oc files list` + `oc files delete` is enough for the dogfood phase. Add automation only if cleanup becomes painful.
- **No content-hash deduplication.** Defer until auto-caching is added (it's the natural home for it).
- **No download of user-uploaded files.** Anthropic's API only allows download of files the model created (skills/code-execution outputs). For SP3, `oc files download` is for retrieving model-created artifacts only — clearly documented as such.
- **No SP2 PDF-block integration.** Explicit deferred follow-up.

## 5. Approach

A small async client (`AnthropicFilesClient`) lives next to the existing Anthropic provider, not inside it (the provider is already 900+ lines; don't bloat it). The CLI wraps the client. Both share a single config source: the Anthropic API key + base URL the provider already uses.

Files-API operations are FREE (per Anthropic docs); only the token usage in messages costs money. So we don't need cost guards or budget checks here — just clean operational tooling.

## 6. Design

### 6.1 Module: `extensions/anthropic-provider/files_client.py` (NEW)

```python
"""Anthropic Files API client.

Beta header: files-api-2025-04-14
Endpoints (per https://docs.claude.com/en/api/files):
  POST   /v1/files           multipart upload
  GET    /v1/files           list (paginated)
  GET    /v1/files/{id}      metadata
  GET    /v1/files/{id}/content   download (only for model-created files)
  DELETE /v1/files/{id}      delete

All operations are FREE; token usage in /v1/messages is what costs.
Workspace-scoped (all keys in a workspace see each other's files).
500 MB per file, 500 GB per org, ~100 req/min beta rate limit.
NOT ZDR-eligible.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import httpx

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"
BETA_HEADER = "files-api-2025-04-14"
MAX_FILE_BYTES = 500 * 1024 * 1024  # 500 MB
RATE_LIMIT_HINT = "Anthropic Files API beta rate limit is ~100 req/min."


@dataclass
class FileMetadata:
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    created_at: datetime
    downloadable: bool

    @classmethod
    def from_response(cls, data: dict) -> "FileMetadata":
        return cls(
            id=data["id"],
            filename=data["filename"],
            mime_type=data["mime_type"],
            size_bytes=data["size_bytes"],
            created_at=datetime.fromisoformat(data["created_at"].replace("Z", "+00:00")),
            downloadable=data.get("downloadable", False),
        )


class FilesAPIError(RuntimeError):
    """Raised on Files API HTTP errors with a helpful message."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AnthropicFilesClient:
    """Async client for the Anthropic Files API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s

    def _headers(self, *, with_content_type: bool = True) -> dict[str, str]:
        h = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": BETA_HEADER,
        }
        return h

    def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout_s)

    async def upload(self, path: Path) -> FileMetadata:
        """Upload a file; returns metadata including the new file_id."""
        if not path.exists():
            raise FileNotFoundError(f"file not found: {path}")
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise FilesAPIError(
                f"file exceeds 500 MB limit: {path} ({size} bytes)"
            )
        async with self._make_client() as client:
            with path.open("rb") as fh:
                files = {"file": (path.name, fh, _guess_mime(path))}
                resp = await client.post(
                    f"{self._base_url}/v1/files",
                    headers=self._headers(),
                    files=files,
                )
        _raise_for_status(resp)
        return FileMetadata.from_response(resp.json())

    async def list(self, limit: int = 50) -> list[FileMetadata]:
        """List uploaded files in this workspace."""
        async with self._make_client() as client:
            resp = await client.get(
                f"{self._base_url}/v1/files",
                headers=self._headers(),
                params={"limit": limit},
            )
        _raise_for_status(resp)
        return [FileMetadata.from_response(d) for d in resp.json().get("data", [])]

    async def get_metadata(self, file_id: str) -> FileMetadata:
        async with self._make_client() as client:
            resp = await client.get(
                f"{self._base_url}/v1/files/{file_id}",
                headers=self._headers(),
            )
        _raise_for_status(resp)
        return FileMetadata.from_response(resp.json())

    async def download(self, file_id: str, output_path: Path) -> int:
        """Download a model-created file. Returns bytes written.

        Raises FilesAPIError(403) if the file isn't downloadable
        (Anthropic only permits download of skill/code-exec outputs,
        not user-uploaded files).
        """
        async with self._make_client() as client:
            async with client.stream(
                "GET",
                f"{self._base_url}/v1/files/{file_id}/content",
                headers=self._headers(),
            ) as resp:
                _raise_for_status(resp)
                total = 0
                with output_path.open("wb") as out:
                    async for chunk in resp.aiter_bytes():
                        out.write(chunk)
                        total += len(chunk)
        return total

    async def delete(self, file_id: str) -> None:
        async with self._make_client() as client:
            resp = await client.delete(
                f"{self._base_url}/v1/files/{file_id}",
                headers=self._headers(),
            )
        _raise_for_status(resp)


def _guess_mime(path: Path) -> str:
    import mimetypes
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.is_success:
        return
    status = resp.status_code
    body = resp.text
    if status == 429:
        raise FilesAPIError(
            f"rate-limited (429). {RATE_LIMIT_HINT} Body: {body}",
            status_code=status,
        )
    if status == 403 and "storage" in body.lower():
        raise FilesAPIError(
            f"storage quota exceeded (403). Org limit is 500 GB. Body: {body}",
            status_code=status,
        )
    if status == 404:
        raise FilesAPIError(
            f"file not found (404). Body: {body}",
            status_code=status,
        )
    raise FilesAPIError(
        f"Files API error (HTTP {status}): {body}",
        status_code=status,
    )
```

### 6.2 Module: `opencomputer/cli_files.py` (NEW)

A Typer subcommand group registered in `opencomputer/cli.py`. Commands:

- `oc files upload <path>` — upload a file; print resulting `file_id`
- `oc files list` — table of all files (id / filename / mime / size / created)
- `oc files delete <file_id>` — delete a file
- `oc files download <file_id> <output_path>` — download a model-created file
- `oc files info <file_id>` — show metadata

Each command:
1. Resolves the Anthropic API key from existing config (same source as the provider — `ANTHROPIC_API_KEY` env or config.yaml)
2. Constructs an `AnthropicFilesClient`
3. Awaits the call
4. Pretty-prints output (rich table for `list`)
5. Catches `FilesAPIError` and prints clean error message + exit 1

### 6.3 Registration in `opencomputer/cli.py`

Add (in the existing imports + Typer registration area):

```python
from opencomputer.cli_files import files_app
app.add_typer(files_app, name="files")
```

Find the existing pattern (look for `add_typer` calls — the `profile`, `plugin`, `mcp` groups all use this).

### 6.4 Error handling

- API key missing → friendly "Set ANTHROPIC_API_KEY or configure your provider" + exit 1
- File too large for upload → "File exceeds 500 MB Anthropic limit" + exit 1
- Rate limit hit → "Rate-limited; ~100 req/min beta limit. Wait and retry" + exit 1
- File not downloadable (user-uploaded) → "Only model-created files can be downloaded" + exit 1
- Generic API error → print body + status + exit 1

No silent failures. Every error path has a user-visible message.

### 6.5 Tests

| File | Tests |
|---|---|
| `tests/test_anthropic_files_client.py` (NEW) | `test_upload_returns_metadata`, `test_upload_rejects_oversize_local`, `test_list_returns_files`, `test_get_metadata`, `test_download_writes_bytes`, `test_download_403_for_non_downloadable`, `test_delete`, `test_rate_limit_429_raises_with_hint`, `test_storage_quota_403_raises_with_hint`, `test_404_raises` |
| `tests/test_cli_files.py` (NEW) | `test_files_upload_prints_id`, `test_files_list_prints_table`, `test_files_delete_calls_client`, `test_files_download_writes_bytes`, `test_files_missing_api_key_exits_with_message` |

Use `httpx.MockTransport` for unit testing the client. CLI tests use Typer's `CliRunner`.

### 6.6 Documentation

Add `docs/cli/files.md` (NEW): user-facing guide covering each command + worked examples + the workspace-scoped + free-operations + ZDR-not-eligible caveats.

## 7. Decisions log

| Decision | Why |
|---|---|
| FilesClient lives in `extensions/anthropic-provider/`, not in `plugin_sdk/` | Provider-specific; no other provider has an equivalent API. Don't pollute the SDK contract. |
| Standalone PR; no SP2 PDF-block integration in this PR | Avoids cross-PR coupling. Integration is 2 lines in `_build_pdf_block` after both merge. |
| No auto-caching / no content-hash dedup in this PR | Caching belongs with the consumer (the provider's PDF block builder). Adding it standalone wastes effort. |
| `oc files download` only works for model-created files | Anthropic API contract; documented honestly with a clear error message |
| No `oc files cleanup --days N` command | YAGNI; manual list+delete is sufficient for dogfood phase |
| Tool-result spillover dropped from SP3 scope | Already fully implemented; survey confirmed. Original audit was stale. |
| Use `httpx.MockTransport` for client tests | Avoids real network. Same pattern OC already uses elsewhere (per `vision_analyze.py:53`). |

## 8. Risks

1. **Beta header churn.** `files-api-2025-04-14` may change. Mitigation: header is a module-level constant; one-line update if Anthropic bumps it.
2. **Workspace pollution.** Multiple Claude processes (parallel sessions, scheduled jobs) sharing one workspace will see each other's files. Documented in `docs/cli/files.md`.
3. **Rate-limit surprise.** 100 req/min beta limit is restrictive. If `oc files list` is run repeatedly in scripts, will throttle. Documented; we don't add backoff (YAGNI).
4. **Files API not eligible for ZDR.** If user has ZDR enabled, uploaded files violate that policy. Documented prominently. CLI does NOT warn at runtime — that would require feature-flag plumbing not in scope.

## 9. Open questions

None — all design decisions resolved.

## 10. Success criteria

- [ ] `AnthropicFilesClient` exposes upload/list/get_metadata/download/delete with typed responses.
- [ ] All HTTP error paths covered (429, 403, 404, generic) with helpful messages.
- [ ] `oc files` CLI: 5 commands working (upload/list/delete/download/info).
- [ ] All 15 new tests passing.
- [ ] No regressions in existing tests.
- [ ] `docs/cli/files.md` written.
- [ ] Full pytest green; ruff clean.

## 11. Out of scope (deferred)

- **SP4** — Server-side tools / Skills-via-API (separate sub-project, demand-gated).
- **SP2 + SP3 integration** — auto-cache PDF uploads via Files API in the provider's `_build_pdf_block` (small follow-up PR after both merge).
- **Multi-provider Files API abstraction** — only matters if a second provider gets one (none today).
- **Content-hash deduplication** — natural home is the SP2-integration follow-up.
- **`oc files cleanup --days N`** — YAGNI until manual cleanup gets painful.
- **Download non-model-created files** — Anthropic API doesn't permit this; documenting the limitation is the fix.
- **Tool-result spillover** — already done.

## 12. References

- [Anthropic Files API](https://docs.claude.com/en/build-with-claude/files)
- [Files API endpoints](https://docs.claude.com/en/api/files)
- SP1 design: `2026-05-02-skill-spec-compliance-design.md`
- SP2 design: `2026-05-02-sp2-pdf-provider-hardening-design.md`
- Tool-result spillover (already shipped): `opencomputer/agent/tool_result_storage.py`
