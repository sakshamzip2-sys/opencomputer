# SP2+SP3 Integration — PDF Files-API Caching — Design

**Date:** 2026-05-02
**Status:** approved (auto-mode brainstorm)
**Sub-project:** SP2+SP3 follow-up (the "two-line integration" that's actually a small feature)
**Authors:** Saksham + Claude Code (Opus 4.7)

---

## 1. Context

SP2 (PR #359) added PDF document-block support to the Anthropic provider. Today every PDF attachment is base64-encoded and inlined into each request. For multi-turn discussions about the same PDF, this means re-uploading the entire file on every turn.

SP3 (PR #360) added an Anthropic Files API client (`AnthropicFilesClient`). With it, a file can be uploaded once and referenced by `file_id` across many turns — saving bandwidth and (eventually) some cost.

This sub-project wires SP2 and SP3 together: when enabled, PDFs go through Files API once, then get referenced by `file_id` on subsequent turns. Default OFF (preserves SP2's behavior).

## 2. Why now (honest framing)

I previously deferred this as "no demand signal yet." The user explicitly authorized building it. Reality check on scope:
- It IS a small feature (~80 LOC + tests + docs), not literally "2 lines"
- Default-OFF behavior means zero impact on users who don't opt in
- Opt-in saves bandwidth on multi-turn PDF conversations (real value when used)
- Pairs naturally with SP4's opt-in pattern (env var + runtime flag)

## 3. Goals

1. **Default OFF.** Without explicit opt-in, behavior is identical to SP2.
2. **Single opt-in knob.** `OPENCOMPUTER_ANTHROPIC_FILES_CACHE=1` env var OR `runtime.custom["anthropic_files_cache"] = True`.
3. **Content-hash cache.** SHA-256 of file bytes → `file_id`, persisted to `<profile_home>/anthropic_files_cache.json`. Same file uploaded twice → one upload + one cache hit.
4. **Fail open.** If Files API upload fails (rate limit, quota, network), fall back to base64. Logged warning. Never break the user's request.
5. **Honest semantics.** `oc files list` shows what's been uploaded; `oc files delete` works on cache entries too (or prune via `oc files cleanup` follow-up).

## 4. Non-goals

- **No cache TTL/auto-prune.** Files persist until DELETE per Anthropic; cache mirrors that. Manual prune via `oc files delete` (existing CLI from SP3).
- **No multi-provider abstraction.** Anthropic-only — Bedrock + OpenAI providers untouched.
- **No automatic file ID validation.** If a cached `file_id` no longer exists server-side (e.g., user deleted via `oc files delete`), the API will 404; we catch + invalidate cache + re-upload.
- **No new CLI command.** Pruning happens via existing `oc files delete`. Listing happens via existing `oc files list` (queries server, not cache; cache becomes stale but server is truth).
- **No image/document-other-than-PDF caching.** PDFs only — that's where the bandwidth bite lives. Images are <5MB; not worth it.
- **No `oc files cache --status` introspection.** YAGNI; cache file is JSON, user can `cat ~/.opencomputer/<profile>/anthropic_files_cache.json` if curious.

## 5. Approach

A small `FilesCache` class persists hash→file_id mappings. Lives in `extensions/anthropic-provider/files_cache.py` (next to `files_client.py`, not in `plugin_sdk/` — Anthropic-specific).

`_build_anthropic_pdf_block` (from SP2) is extended:
1. If caching enabled (env or runtime): hash the PDF, check cache
2. Cache hit: build `{type: document, source: {type: file, file_id: <cached>}}` block
3. Cache miss: upload via `AnthropicFilesClient`, cache the result, build file_id-block
4. Upload failure: log warning, fall back to base64 path (existing SP2 behavior)
5. 404 on subsequent use (file deleted server-side): catch, invalidate cache entry, re-upload

The `_resolve_anthropic_files_cache_enabled(runtime)` resolver mirrors SP4's `_resolve_anthropic_skills` pattern (runtime → env → default).

## 6. Design

### 6.1 Module: `extensions/anthropic-provider/files_cache.py` (NEW)

```python
"""Persistent content-hash → file_id cache for the Anthropic Files API.

Default OFF. Opt-in via:
  - OPENCOMPUTER_ANTHROPIC_FILES_CACHE=1 env var
  - runtime.custom["anthropic_files_cache"] = True

Cache file: <profile_home>/anthropic_files_cache.json
Format: {"<sha256-hex>": {"file_id": "...", "uploaded_at": "<iso>", "filename": "...", "size_bytes": N}}

Content-addressed: same bytes always hash to the same file_id, so cache
is safe across processes (last writer wins on race; that's fine — both
writers had the same data).

Failure-open: any cache I/O error is logged + treated as a miss.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

CACHE_FILENAME = "anthropic_files_cache.json"


@dataclass
class CacheEntry:
    file_id: str
    uploaded_at: str            # ISO8601
    filename: str
    size_bytes: int


def hash_file_bytes(data: bytes) -> str:
    """SHA-256 hex digest of file bytes (cache key)."""
    return hashlib.sha256(data).hexdigest()


class FilesCache:
    """JSON-backed content-hash → file_id cache. Failure-open."""

    def __init__(self, cache_path: Path) -> None:
        self.path = cache_path

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("FilesCache read failed (%s); treating as empty", exc)
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2, sort_keys=True))
        except OSError as exc:
            _log.warning("FilesCache write failed (%s); cache miss next time", exc)

    def get(self, content_hash: str) -> CacheEntry | None:
        data = self._load()
        entry = data.get(content_hash)
        if entry is None:
            return None
        try:
            return CacheEntry(**entry)
        except TypeError as exc:
            _log.warning("FilesCache entry malformed for %s (%s); ignoring", content_hash[:8], exc)
            return None

    def put(
        self,
        content_hash: str,
        *,
        file_id: str,
        filename: str,
        size_bytes: int,
    ) -> None:
        data = self._load()
        data[content_hash] = {
            "file_id": file_id,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            "filename": filename,
            "size_bytes": size_bytes,
        }
        self._save(data)

    def invalidate(self, content_hash: str) -> None:
        """Drop an entry — used when server returns 404 for a cached file_id."""
        data = self._load()
        if content_hash in data:
            del data[content_hash]
            self._save(data)
```

### 6.2 Cache resolver (in `provider.py`)

Mirroring SP4's pattern:

```python
def _resolve_anthropic_files_cache_enabled(runtime) -> bool:
    """Return True iff Files API caching is opted in.

    Resolution: runtime.custom flag first, env var second, default False.
    """
    if runtime is not None:
        explicit = (getattr(runtime, "custom", {}) or {}).get("anthropic_files_cache")
        if explicit is not None:
            return bool(explicit)
    return os.environ.get("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "").strip().lower() in ("1", "true", "yes", "on")
```

### 6.3 Provider integration: `_build_anthropic_pdf_block` extension

Add an optional `cache: FilesCache | None` and `client: AnthropicFilesClient | None` parameter to `_build_anthropic_pdf_block`. When both are provided AND caching is enabled, use the Files API path. Otherwise fall through to the existing base64 path (SP2's current behavior — unchanged).

```python
async def _build_anthropic_pdf_block(
    path: Path,
    *,
    cache: FilesCache | None = None,
    client: AnthropicFilesClient | None = None,
) -> dict[str, Any] | None:
    """Build an Anthropic `document` content block from a PDF path.

    If both `cache` and `client` are non-None, uses Files API:
      - SHA-256 the bytes
      - Cache hit → reference by file_id
      - Cache miss → upload, cache, reference
      - Failure → fall back to base64 inline (logged warning)

    Otherwise (cache or client is None): existing SP2 base64-inline behavior.
    """
    # ... existing read + size + page-count guards (unchanged) ...

    if cache is not None and client is not None:
        try:
            content_hash = hash_file_bytes(data)
            entry = cache.get(content_hash)
            if entry is not None:
                return {
                    "type": "document",
                    "source": {"type": "file", "file_id": entry.file_id},
                }
            # Cache miss → upload
            metadata = await client.upload(path)
            cache.put(
                content_hash,
                file_id=metadata.id,
                filename=metadata.filename,
                size_bytes=metadata.size_bytes,
            )
            return {
                "type": "document",
                "source": {"type": "file", "file_id": metadata.id},
            }
        except Exception as exc:
            _log.warning(
                "Files API caching failed for %s (%s); falling back to base64",
                path, exc,
            )
            # Fall through to base64 path

    # Existing base64-inline path (SP2's behavior — preserved verbatim)
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": pdf_to_base64(data),
        },
    }
```

`_anthropic_content_blocks_with_attachments` (SP2's dispatcher) needs to accept + thread the `cache`/`client` kwargs. It's `async` already (since `complete()` calls it via await); SP2's helper is currently sync. Need to make it async OR keep sync version + add async sibling. Decision: convert to async (single source of truth, simpler).

### 6.4 Wiring at the provider's call sites

In `complete()` and `stream_complete()` (where `_anthropic_content_blocks_with_attachments` is called), construct the cache + client when caching is enabled:

```python
files_cache_enabled = _resolve_anthropic_files_cache_enabled(runtime_extras_to_runtime(runtime_extras))
files_cache = None
files_client = None
if files_cache_enabled:
    cache_path = _profile_home() / CACHE_FILENAME
    files_cache = FilesCache(cache_path)
    files_client = AnthropicFilesClient(api_key=self._api_key)
```

(`_profile_home()` already exists somewhere in opencomputer for resolving the per-profile config dir.)

### 6.5 404 invalidation on subsequent use

If a cached `file_id` is referenced in a request and the server returns 404 (file was deleted via `oc files delete` or by another process), the API call will fail. This isn't directly catchable inside `_build_anthropic_pdf_block` (the failure happens in `messages.create`, not at block-build time).

Mitigation strategies (ranked by simplicity):

1. **Don't auto-invalidate.** User runs `oc files delete <id>` then `oc files cleanup-cache` (a tiny new command — out of scope for v1; document it as a known limitation).
2. **Pre-flight HEAD request.** Before using a cached `file_id`, call `client.get_metadata(file_id)`. Adds latency. Defeats the bandwidth-saving purpose.
3. **Catch 404 in `messages.create`.** Inspect error, invalidate cache entry, retry once with base64 fallback.

**Decision: option 1.** Documented limitation. Aligns with "fail open" — worst case is one 400/404 error response, user retries, cache miss path uploads again with new file_id. Don't add latency or complexity for a corner case.

### 6.6 Tests

| File | Tests |
|---|---|
| `tests/test_anthropic_files_cache.py` (NEW) | `test_hash_file_bytes_deterministic`, `test_cache_get_returns_none_when_missing`, `test_cache_put_then_get_roundtrip`, `test_cache_invalidate_removes_entry`, `test_cache_load_handles_missing_file`, `test_cache_load_handles_malformed_json`, `test_cache_get_handles_malformed_entry`, `test_cache_put_creates_parent_dir` |
| `tests/test_anthropic_pdf_caching.py` (NEW) | `test_pdf_block_uses_base64_when_cache_or_client_none`, `test_pdf_block_cache_hit_uses_file_id`, `test_pdf_block_cache_miss_uploads_then_uses_file_id`, `test_pdf_block_upload_failure_falls_back_to_base64`, `test_resolve_files_cache_enabled_env`, `test_resolve_files_cache_enabled_runtime`, `test_resolve_files_cache_enabled_default_false` |

All unit tests using `httpx.MockTransport` (for client) + `tmp_path` (for cache). No live API calls.

### 6.7 Documentation

Add to `docs/cli/files.md` (or a new section): describe the opt-in env var + runtime flag, the cache file location, the 404 limitation, when to use (multi-turn PDF conversations).

## 7. Decisions log

| Decision | Why |
|---|---|
| Default OFF | Preserves SP2's behavior; opt-in only mirrors SP4's pattern; conservative until demand emerges |
| SHA-256 content hash (not filename) | Same content from different paths shares cache; renames don't break cache |
| JSON cache file | Simple, debuggable (`cat anthropic_files_cache.json`); no DB dep |
| Failure-open everywhere | One bad cache I/O can't break a user's request |
| No 404 auto-invalidate | Corner case; pre-flight check defeats the purpose; document the limitation |
| `_build_anthropic_pdf_block` becomes async | SP2's sync version was OK; integration needs async for upload; one flavor is simpler than two |
| Cache module in extension dir, not plugin_sdk | Anthropic-specific; no SDK contract impact |

## 8. Risks

1. **Async conversion breaks callers.** `_build_anthropic_pdf_block` becomes `async`; its caller `_anthropic_content_blocks_with_attachments` must also become async. The chain bottoms out at `complete()` which is already async, so this is mechanical. Tests need adapting.
2. **Cache file write race between processes.** Two parallel processes could last-writer-wins. Acceptable: both writers have the same data (content-addressed). One process's CacheEntry might be slightly fresher, but file_id is identical.
3. **Cache file gets huge.** Each entry is ~150 bytes. 10K entries = 1.5MB. Practically unlimited for normal use. If it ever matters, `oc files cleanup` follow-up handles pruning.
4. **`messages.create` 404 on stale `file_id`.** Documented limitation. User retries; cache miss re-uploads with new file_id.
5. **Workspace deletion of files.** Files API is workspace-scoped. If another API key in the same workspace deletes a file, this cache becomes stale. Documented.

## 9. Open questions

None — all design decisions resolved.

## 10. Success criteria

- [ ] `FilesCache` module: 8 unit tests covering get/put/invalidate, malformed-data handling, missing-file handling, parent-dir creation.
- [ ] `_resolve_anthropic_files_cache_enabled` resolver: 3 tests (env, runtime, default).
- [ ] `_build_anthropic_pdf_block` extension: 4 integration tests covering cache hit, cache miss, upload failure fallback, no-cache path.
- [ ] When opt-in is OFF, behavior is byte-identical to SP2 (regression test).
- [ ] All existing SP2/SP3 tests still pass.
- [ ] Full pytest green; ruff clean.
- [ ] Documentation in `docs/cli/files.md`.

## 11. Out of scope (deferred)

- **`oc files cleanup-cache` command.** Manual: delete the JSON file; or wait for natural cache-miss after 404.
- **HEAD pre-flight to validate cached file_id.** Latency cost > benefit.
- **Pre-upload size checks.** SP2 already enforces 32 MB cap before reaching cache code.
- **Cross-provider files cache.** Anthropic only; OpenAI/Bedrock have no equivalent.
- **Cache metrics / telemetry.** Could be added if usage data ever drives a decision.

## 12. References

- SP2 spec: `2026-05-02-sp2-pdf-provider-hardening-design.md`
- SP3 spec: `2026-05-02-sp3-files-api-design.md`
- [Anthropic Files API](https://docs.claude.com/en/build-with-claude/files)
- [Anthropic PDF support](https://docs.claude.com/en/build-with-claude/pdf-support)
