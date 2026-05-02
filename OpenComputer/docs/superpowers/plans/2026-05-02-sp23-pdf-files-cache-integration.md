# SP2+SP3 PDF Files-API Caching Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire SP2's `_build_anthropic_pdf_block` through SP3's `AnthropicFilesClient` via a content-hash cache so that opting in saves bandwidth on multi-turn PDF discussions.

**Architecture:** New `FilesCache` module (JSON-backed, content-addressed). New `_resolve_anthropic_files_cache_enabled` resolver mirroring SP4's pattern. Extend `_build_anthropic_pdf_block` to take optional `cache` + `client` and use Files API path when both present + opt-in active. Fall back to base64 on any failure.

**Tech Stack:** Python 3.12+, hashlib (stdlib), json (stdlib), pytest, no new third-party deps.

**Spec:** [`docs/superpowers/specs/2026-05-02-sp23-pdf-files-cache-integration-design.md`](../specs/2026-05-02-sp23-pdf-files-cache-integration-design.md)

---

## Pre-flight

- [ ] **Step 0a: Verify worktree (already has SP2+SP3 merged)**

```bash
cd /private/tmp/oc-sp23-pdf-files-integration
git status
git log --oneline -5
```

Expected: clean tree, on `feat/sp23-pdf-files-cache-integration`. The first 3 commits should be the spec + the two merge commits for SP2 and SP3.

- [ ] **Step 0b: Set up venv + verify SP2/SP3 baseline**

```bash
cd OpenComputer
if [ ! -d .venv ]; then
  uv venv .venv --python 3.12 2>&1 | tail -2
  uv pip install --python .venv/bin/python -e . pytest pytest-asyncio httpx ruff 2>&1 | tail -3
fi
.venv/bin/python -m pytest tests/test_anthropic_provider_pdf.py tests/test_anthropic_files_client.py tests/test_pdf_helpers.py tests/test_cli_files.py --tb=line -q 2>&1 | tail -5
```

Expected: all green (SP2's PDF tests + SP3's Files API tests). Record count.

- [ ] **Step 0c: Baseline ruff**

```bash
.venv/bin/ruff check extensions/anthropic-provider/ tests/
```

Expected: clean.

---

## Task 1: FilesCache module + tests

**Files:**
- Create: `extensions/anthropic-provider/files_cache.py`
- Test: `tests/test_anthropic_files_cache.py` (NEW)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_anthropic_files_cache.py`:

```python
"""Tests for AnthropicFilesClient persistent cache (SP2+SP3 integration)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

CACHE_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "files_cache.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_files_cache", CACHE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_hash_file_bytes_deterministic():
    module = _load_module()
    h1 = module.hash_file_bytes(b"hello world")
    h2 = module.hash_file_bytes(b"hello world")
    h3 = module.hash_file_bytes(b"different content")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # SHA-256 hex


def test_cache_get_returns_none_when_missing(tmp_path):
    module = _load_module()
    cache = module.FilesCache(tmp_path / "cache.json")
    assert cache.get("any_hash") is None


def test_cache_put_then_get_roundtrip(tmp_path):
    module = _load_module()
    cache = module.FilesCache(tmp_path / "cache.json")
    cache.put(
        "hash1",
        file_id="file_abc",
        filename="doc.pdf",
        size_bytes=1024,
    )
    entry = cache.get("hash1")
    assert entry is not None
    assert entry.file_id == "file_abc"
    assert entry.filename == "doc.pdf"
    assert entry.size_bytes == 1024
    assert entry.uploaded_at  # ISO timestamp populated


def test_cache_invalidate_removes_entry(tmp_path):
    module = _load_module()
    cache = module.FilesCache(tmp_path / "cache.json")
    cache.put("h", file_id="file_x", filename="f", size_bytes=1)
    assert cache.get("h") is not None
    cache.invalidate("h")
    assert cache.get("h") is None


def test_cache_load_handles_missing_file(tmp_path):
    module = _load_module()
    cache = module.FilesCache(tmp_path / "nonexistent.json")
    # Should not raise; just behave as empty
    assert cache.get("anything") is None


def test_cache_load_handles_malformed_json(tmp_path):
    module = _load_module()
    cache_file = tmp_path / "bad.json"
    cache_file.write_text("{ this is not valid json")
    cache = module.FilesCache(cache_file)
    # Should not raise; treat as empty + log warning
    assert cache.get("anything") is None


def test_cache_get_handles_malformed_entry(tmp_path):
    module = _load_module()
    cache_file = tmp_path / "weird.json"
    cache_file.write_text(json.dumps({"h": {"missing_required_fields": True}}))
    cache = module.FilesCache(cache_file)
    # Malformed entry → log + return None, don't crash
    assert cache.get("h") is None


def test_cache_put_creates_parent_dir(tmp_path):
    module = _load_module()
    nested = tmp_path / "deep" / "nested" / "cache.json"
    cache = module.FilesCache(nested)
    cache.put("h", file_id="file_y", filename="f", size_bytes=1)
    assert nested.exists()
    assert cache.get("h") is not None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /private/tmp/oc-sp23-pdf-files-integration/OpenComputer
.venv/bin/python -m pytest tests/test_anthropic_files_cache.py -v
```

Expected: FAIL — `files_cache.py` doesn't exist.

- [ ] **Step 3: Implement the module**

Create `extensions/anthropic-provider/files_cache.py`:

```python
"""Persistent content-hash → file_id cache for the Anthropic Files API.

Default OFF. Opt-in via:
  - OPENCOMPUTER_ANTHROPIC_FILES_CACHE=1 env var
  - runtime.custom["anthropic_files_cache"] = True

Cache file: <profile_home>/anthropic_files_cache.json
Format: {"<sha256-hex>": {"file_id": "...", "uploaded_at": "<iso>",
                          "filename": "...", "size_bytes": N}}

Content-addressed: same bytes always hash to the same file_id, so cache
is safe across processes (last writer wins on race; both writers had
the same data).

Failure-open: any cache I/O error is logged + treated as a miss.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

CACHE_FILENAME = "anthropic_files_cache.json"


@dataclass
class CacheEntry:
    """One cache entry — what we know about a previously-uploaded file."""
    file_id: str
    uploaded_at: str            # ISO8601
    filename: str
    size_bytes: int


def hash_file_bytes(data: bytes) -> str:
    """SHA-256 hex digest of file bytes — used as cache key."""
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
        """Return the cached entry, or None if missing or malformed."""
        data = self._load()
        entry = data.get(content_hash)
        if entry is None:
            return None
        try:
            return CacheEntry(**entry)
        except TypeError as exc:
            _log.warning(
                "FilesCache entry malformed for %s (%s); ignoring",
                content_hash[:8], exc,
            )
            return None

    def put(
        self,
        content_hash: str,
        *,
        file_id: str,
        filename: str,
        size_bytes: int,
    ) -> None:
        """Store an entry; overwrites any existing entry for this hash."""
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


__all__ = [
    "CACHE_FILENAME",
    "CacheEntry",
    "FilesCache",
    "hash_file_bytes",
]
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_anthropic_files_cache.py -v
```

Expected: 8/8 PASS.

- [ ] **Step 5: Commit**

```bash
cd /private/tmp/oc-sp23-pdf-files-integration
git add OpenComputer/extensions/anthropic-provider/files_cache.py OpenComputer/tests/test_anthropic_files_cache.py
git commit -m "feat(anthropic-provider): files_cache.py — content-hash → file_id JSON cache (SP2+SP3 integration)"
```

---

## Task 2: Cache-enable resolver + tests

**Files:**
- Modify: `extensions/anthropic-provider/provider.py` (add resolver near other resolvers)
- Test: `tests/test_anthropic_pdf_caching.py` (NEW)

- [ ] **Step 1: Find where SP4's `_resolve_anthropic_skills` lives in provider.py**

```bash
grep -n "_resolve_anthropic_skills\|ANTHROPIC_SKILLS_BETA_HEADERS" extensions/anthropic-provider/provider.py | head -5
```

Place the new resolver next to it for symmetry.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_anthropic_pdf_caching.py`:

```python
"""Tests for SP2+SP3 PDF Files-API caching integration."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

PROVIDER_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "provider.py"
)


def _load_provider_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_provider_pdf_caching", PROVIDER_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def _runtime(custom: dict | None = None):
    return SimpleNamespace(custom=custom or {})


# ─── _resolve_anthropic_files_cache_enabled ───────────────────


def test_resolve_files_cache_enabled_default_false(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", raising=False)
    module = _load_provider_module()
    assert module._resolve_anthropic_files_cache_enabled(_runtime()) is False
    assert module._resolve_anthropic_files_cache_enabled(None) is False


def test_resolve_files_cache_enabled_env(monkeypatch):
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "1")
    module = _load_provider_module()
    assert module._resolve_anthropic_files_cache_enabled(_runtime()) is True
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "true")
    assert module._resolve_anthropic_files_cache_enabled(_runtime()) is True
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "0")
    assert module._resolve_anthropic_files_cache_enabled(_runtime()) is False


def test_resolve_files_cache_enabled_runtime(monkeypatch):
    monkeypatch.delenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", raising=False)
    module = _load_provider_module()
    assert module._resolve_anthropic_files_cache_enabled(
        _runtime({"anthropic_files_cache": True})
    ) is True
    assert module._resolve_anthropic_files_cache_enabled(
        _runtime({"anthropic_files_cache": False})
    ) is False


def test_resolve_files_cache_enabled_runtime_overrides_env(monkeypatch):
    """Runtime flag wins over env var (per spec resolution order)."""
    monkeypatch.setenv("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "1")
    module = _load_provider_module()
    # Runtime explicitly False overrides env True
    assert module._resolve_anthropic_files_cache_enabled(
        _runtime({"anthropic_files_cache": False})
    ) is False
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_anthropic_pdf_caching.py -v
```

Expected: FAIL — `_resolve_anthropic_files_cache_enabled` doesn't exist.

- [ ] **Step 4: Implement the resolver**

Add to `extensions/anthropic-provider/provider.py` near `_resolve_anthropic_skills` (find via grep in Step 1):

```python
def _resolve_anthropic_files_cache_enabled(runtime) -> bool:
    """Return True iff Files API caching is opted in.

    Resolution order:
    1. runtime.custom["anthropic_files_cache"] (explicit programmatic — wins)
    2. OPENCOMPUTER_ANTHROPIC_FILES_CACHE env var (truthy values: 1/true/yes/on)
    3. False (default OFF)

    Mirrors _resolve_anthropic_skills' shape (SP4) for consistency.
    """
    if runtime is not None:
        explicit = (getattr(runtime, "custom", {}) or {}).get("anthropic_files_cache")
        if explicit is not None:
            return bool(explicit)
    env = os.environ.get("OPENCOMPUTER_ANTHROPIC_FILES_CACHE", "").strip().lower()
    return env in ("1", "true", "yes", "on")
```

(`os` is already imported at the top from SP4's work.)

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_anthropic_pdf_caching.py -v -k "resolve_files_cache"
```

Expected: 4/4 PASS.

- [ ] **Step 6: Commit**

```bash
cd /private/tmp/oc-sp23-pdf-files-integration
git add OpenComputer/extensions/anthropic-provider/provider.py OpenComputer/tests/test_anthropic_pdf_caching.py
git commit -m "feat(anthropic-provider): _resolve_anthropic_files_cache_enabled (SP2+SP3 integration)"
```

---

## Task 3: Extend `_build_anthropic_pdf_block` for cache path + tests

**Files:**
- Modify: `extensions/anthropic-provider/provider.py:_build_anthropic_pdf_block` + dispatcher
- Modify: `tests/test_anthropic_pdf_caching.py` (add 4 integration tests)

- [ ] **Step 1: Locate `_build_anthropic_pdf_block` and its caller**

```bash
grep -n "_build_anthropic_pdf_block\|_anthropic_content_blocks_with_attachments\|_content_blocks_with_attachments" extensions/anthropic-provider/provider.py | head -10
```

Note current signature (sync) and where it's called from.

- [ ] **Step 2: Write the failing integration tests**

Add to `tests/test_anthropic_pdf_caching.py`:

```python
@pytest.mark.asyncio
async def test_pdf_block_uses_base64_when_cache_or_client_none(tmp_path):
    """When cache/client unset, falls through to existing SP2 base64 behavior."""
    module = _load_provider_module()
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_minimal_pdf())

    block = await module._build_anthropic_pdf_block(pdf)  # no cache/client kwargs
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_pdf_block_cache_hit_uses_file_id(tmp_path):
    """Cache hit returns file_id-based block; no upload call made."""
    module = _load_provider_module()
    cache_module_spec = importlib.util.spec_from_file_location(
        "_test_files_cache",
        Path(__file__).parent.parent / "extensions" / "anthropic-provider" / "files_cache.py",
    )
    cache_module = importlib.util.module_from_spec(cache_module_spec)
    sys.modules.setdefault(cache_module_spec.name, cache_module)
    cache_module_spec.loader.exec_module(cache_module)

    pdf = tmp_path / "doc.pdf"
    pdf_bytes = _make_minimal_pdf()
    pdf.write_bytes(pdf_bytes)

    cache = cache_module.FilesCache(tmp_path / "cache.json")
    cache.put(
        cache_module.hash_file_bytes(pdf_bytes),
        file_id="file_cached",
        filename="doc.pdf",
        size_bytes=len(pdf_bytes),
    )

    fake_client = MagicMock()
    fake_client.upload = AsyncMock()  # should NOT be called

    block = await module._build_anthropic_pdf_block(
        pdf, cache=cache, client=fake_client
    )
    assert block == {
        "type": "document",
        "source": {"type": "file", "file_id": "file_cached"},
    }
    fake_client.upload.assert_not_called()


@pytest.mark.asyncio
async def test_pdf_block_cache_miss_uploads_then_uses_file_id(tmp_path):
    """Cache miss → upload → cache + file_id-based block."""
    module = _load_provider_module()
    cache_module_spec = importlib.util.spec_from_file_location(
        "_test_files_cache_b",
        Path(__file__).parent.parent / "extensions" / "anthropic-provider" / "files_cache.py",
    )
    cache_module = importlib.util.module_from_spec(cache_module_spec)
    sys.modules.setdefault(cache_module_spec.name, cache_module)
    cache_module_spec.loader.exec_module(cache_module)

    pdf = tmp_path / "doc.pdf"
    pdf_bytes = _make_minimal_pdf()
    pdf.write_bytes(pdf_bytes)
    cache = cache_module.FilesCache(tmp_path / "cache.json")

    # Mock client.upload to return fake metadata
    fake_metadata = MagicMock()
    fake_metadata.id = "file_uploaded_xyz"
    fake_metadata.filename = "doc.pdf"
    fake_metadata.size_bytes = len(pdf_bytes)
    fake_client = MagicMock()
    fake_client.upload = AsyncMock(return_value=fake_metadata)

    block = await module._build_anthropic_pdf_block(
        pdf, cache=cache, client=fake_client
    )

    fake_client.upload.assert_called_once_with(pdf)
    assert block == {
        "type": "document",
        "source": {"type": "file", "file_id": "file_uploaded_xyz"},
    }

    # Subsequent call hits cache (no second upload)
    fake_client.upload.reset_mock()
    block2 = await module._build_anthropic_pdf_block(
        pdf, cache=cache, client=fake_client
    )
    assert block2["source"]["file_id"] == "file_uploaded_xyz"
    fake_client.upload.assert_not_called()


@pytest.mark.asyncio
async def test_pdf_block_upload_failure_falls_back_to_base64(tmp_path, caplog):
    """If upload raises, falls back to base64 path; user's request still succeeds."""
    import logging
    module = _load_provider_module()
    cache_module_spec = importlib.util.spec_from_file_location(
        "_test_files_cache_c",
        Path(__file__).parent.parent / "extensions" / "anthropic-provider" / "files_cache.py",
    )
    cache_module = importlib.util.module_from_spec(cache_module_spec)
    sys.modules.setdefault(cache_module_spec.name, cache_module)
    cache_module_spec.loader.exec_module(cache_module)

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(_make_minimal_pdf())
    cache = cache_module.FilesCache(tmp_path / "cache.json")

    fake_client = MagicMock()
    fake_client.upload = AsyncMock(side_effect=RuntimeError("network down"))

    with caplog.at_level(logging.WARNING):
        block = await module._build_anthropic_pdf_block(
            pdf, cache=cache, client=fake_client
        )

    assert block["source"]["type"] == "base64"
    assert any(
        "fall" in r.message.lower() and "base64" in r.message.lower()
        for r in caplog.records
    )


def _make_minimal_pdf() -> bytes:
    """Smallest valid-looking PDF for tests."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        b"<< /Type /Page >>\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
.venv/bin/python -m pytest tests/test_anthropic_pdf_caching.py -v -k "pdf_block"
```

Expected: FAIL — `_build_anthropic_pdf_block` is currently sync and doesn't accept cache/client kwargs.

- [ ] **Step 4: Convert `_build_anthropic_pdf_block` to async + add cache path**

Replace the existing function in `extensions/anthropic-provider/provider.py`. Keep all existing guard rails (size, page count, error handling) — just wrap as async + add the optional cache path.

```python
async def _build_anthropic_pdf_block(
    path: Path,
    *,
    cache: "FilesCache | None" = None,
    client: "AnthropicFilesClient | None" = None,
) -> dict[str, Any] | None:
    """Build an Anthropic ``document`` content block from a PDF path.

    Honors the SP2 guard rails (``plugin_sdk.pdf_helpers``):
    - 32 MB request size cap
    - 600-page hard limit (returns None)
    - 100-page soft warning (returns block with warning logged)

    If both `cache` and `client` are provided, uses the SP3 Files API
    path: hash the bytes, check cache, upload + cache on miss, fall back
    to base64 inline on any failure. Otherwise (no cache or no client):
    base64 inline (SP2's original behavior — preserved verbatim).
    """
    # ── existing guard rails (unchanged from SP2) ──
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

    # ── new: Files API cache path (opt-in) ──
    if cache is not None and client is not None:
        try:
            # Lazy-import to avoid circular dep + only when caching is on
            from importlib import import_module
            cache_mod = import_module(__name__).__dict__.get("hash_file_bytes")
            if cache_mod is None:
                # cache_mod was imported at module top; this is the normal path
                from . import files_cache as _fc  # type: ignore  # may fail in test importlib loads
                hash_func = _fc.hash_file_bytes
            else:
                hash_func = cache_mod
            content_hash = hash_func(data)
            entry = cache.get(content_hash)
            if entry is not None:
                return {
                    "type": "document",
                    "source": {"type": "file", "file_id": entry.file_id},
                }
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
        except Exception as exc:  # noqa: BLE001 — fail-open by design
            _log.warning(
                "Files API caching failed for %s (%s); falling back to base64",
                path, exc,
            )
            # Fall through to base64 path

    # ── existing base64 path (SP2's behavior, preserved) ──
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": pdf_to_base64(data),
        },
    }
```

**Important: simpler import.** The `lazy import` dance above is overkill. Just import `hash_file_bytes` at module top when the file is structured normally. Replace the inner block with:

```python
    if cache is not None and client is not None:
        try:
            from extensions_anthropic_provider_files_cache import hash_file_bytes  # adjust import
            content_hash = hash_file_bytes(data)
            ...
```

Actually the cleanest: add `from extensions/anthropic-provider/files_cache import hash_file_bytes, FilesCache` at module top of `provider.py`. Since the extension files are loaded via `spec_from_file_location` in tests, you might need the same dance. Try the simplest import first:

```python
# At module top of provider.py:
from pathlib import Path

# Files cache (SP2+SP3 integration). Provider lives in same dir.
_files_cache_path = Path(__file__).parent / "files_cache.py"
import importlib.util as _ilu
_fc_spec = _ilu.spec_from_file_location("_provider_files_cache", _files_cache_path)
_fc_module = _ilu.module_from_spec(_fc_spec)
import sys as _sys
_sys.modules.setdefault(_fc_spec.name, _fc_module)
_fc_spec.loader.exec_module(_fc_module)
hash_file_bytes = _fc_module.hash_file_bytes
FilesCache = _fc_module.FilesCache
```

Verify in tests: imports + usage work. If this dance is too ugly, factor out a small `_load_files_cache()` helper.

- [ ] **Step 5: Update the dispatcher to be async + thread cache/client**

Find `_anthropic_content_blocks_with_attachments` (or similar; it's the function that loops attachments and calls `_build_anthropic_pdf_block`). Convert to async and add `cache` + `client` kwargs that are forwarded to the PDF builder. Image builder is unchanged.

- [ ] **Step 6: Run tests to verify they pass**

```bash
.venv/bin/python -m pytest tests/test_anthropic_pdf_caching.py -v
.venv/bin/python -m pytest tests/test_anthropic_provider_pdf.py -v --tb=short  # SP2's tests must still pass
```

Expected: all PASS. SP2's existing PDF tests must still work because the default path (no cache/client) is the same base64 behavior.

If SP2's tests fail with "coroutine was never awaited" or similar, the async conversion has a sync caller that needs updating.

- [ ] **Step 7: Commit**

```bash
cd /private/tmp/oc-sp23-pdf-files-integration
git add OpenComputer/extensions/anthropic-provider/provider.py OpenComputer/tests/test_anthropic_pdf_caching.py
git commit -m "feat(anthropic-provider): wire _build_anthropic_pdf_block through Files API cache (SP2+SP3 integration)"
```

---

## Task 4: Wire the cache + client at provider's `complete()` / `stream_complete()` callsites

**Files:**
- Modify: `extensions/anthropic-provider/provider.py` (call sites of `_anthropic_content_blocks_with_attachments`)

- [ ] **Step 1: Find the call sites**

```bash
grep -n "_content_blocks_with_attachments\|_anthropic_content_blocks_with_attachments" extensions/anthropic-provider/provider.py
```

Note each line number.

- [ ] **Step 2: At each call site, construct cache + client when caching is enabled**

Pseudocode for the wire-up at the start of each `complete`/`stream_complete` body (after kwargs setup):

```python
# Resolve runtime / runtime_extras → caching enabled?
runtime_obj = _runtime_extras_to_runtime(runtime_extras) if runtime_extras else None
cache_enabled = _resolve_anthropic_files_cache_enabled(runtime_obj)
files_cache = None
files_client = None
if cache_enabled:
    cache_path = _resolve_profile_home() / CACHE_FILENAME
    files_cache = FilesCache(cache_path)
    # AnthropicFilesClient lives in files_client.py — same lazy-load dance as files_cache
    files_client = _AnthropicFilesClient(api_key=self._api_key)
```

Where `_resolve_profile_home()` is whatever helper the provider already uses for paths (find via `grep -n "profile_home\|_home(" extensions/anthropic-provider/provider.py`).

If no profile-home helper exists in the provider, use `Path.home() / ".opencomputer" / "anthropic_files_cache.json"` as a sensible default — but check the rest of OC's profile-resolution pattern first (`opencomputer/agent/state.py` typically has `_home()` or similar).

- [ ] **Step 3: Pass `cache` + `client` to the dispatcher**

Update each call:

```python
content = await _anthropic_content_blocks_with_attachments(
    text=msg.content,
    attachment_paths=msg.attachments,
    cache=files_cache,
    client=files_client,
)
```

(Make sure all callers `await` since the function is now async.)

- [ ] **Step 4: Run the FULL provider test suite**

```bash
.venv/bin/python -m pytest tests/ -k "anthropic" --tb=short -q | tail -10
```

Expected: all green. SP2 + SP3 + new cache tests all pass.

- [ ] **Step 5: Commit**

```bash
cd /private/tmp/oc-sp23-pdf-files-integration
git add OpenComputer/extensions/anthropic-provider/provider.py
git commit -m "feat(anthropic-provider): wire Files-API caching into complete()/stream_complete() (SP2+SP3 integration)"
```

---

## Task 5: Documentation

**Files:**
- Modify: `docs/cli/files.md` (add caching section)

- [ ] **Step 1: Read the existing docs/cli/files.md**

```bash
cat docs/cli/files.md
```

- [ ] **Step 2: Add a "PDF auto-caching (opt-in)" section**

Append:

```markdown
## PDF auto-caching (opt-in)

OpenComputer's Anthropic provider can cache PDF uploads via the Files
API to save bandwidth on multi-turn conversations about the same file.
Default OFF.

### Enable

Either:

```bash
export OPENCOMPUTER_ANTHROPIC_FILES_CACHE=1
opencomputer chat
```

Or programmatically:

```python
from opencomputer.agent.runtime import RuntimeContext

runtime = RuntimeContext(custom={"anthropic_files_cache": True})
```

### How it works

1. SHA-256 hash of the PDF bytes is the cache key
2. Cache file: `<profile_home>/anthropic_files_cache.json`
3. Hit → request uses `{type: document, source: {type: file, file_id: <cached>}}`
4. Miss → upload via Files API, cache the file_id, then use it
5. Failure → log warning, fall back to base64 inline (request still succeeds)

### Caveats

- **Workspace-scoped.** All API keys in your workspace see each other's
  uploaded files. If another process deletes a file you have cached
  (via `oc files delete`), the next request using that cached file_id
  will return 404 from the API.
- **No auto-prune.** Cache file grows over time. Inspect with
  `cat <profile_home>/anthropic_files_cache.json`. To wipe: delete the
  file. To prune individual entries: delete the file ID server-side
  (`oc files delete <id>`) — the next cache miss will re-upload.
- **Anthropic provider only.** Bedrock and OpenAI ignore the flag.
```

- [ ] **Step 3: Commit**

```bash
cd /private/tmp/oc-sp23-pdf-files-integration
git add OpenComputer/docs/cli/files.md
git commit -m "docs(cli): PDF auto-caching opt-in section (SP2+SP3 integration)"
```

---

## Task 6: Final verification + push + PR

- [ ] **Step 1: Run FULL pytest scope (SP2+SP3+integration)**

```bash
cd /private/tmp/oc-sp23-pdf-files-integration/OpenComputer
.venv/bin/python -m pytest tests/test_anthropic_files_cache.py tests/test_anthropic_pdf_caching.py tests/test_anthropic_provider_pdf.py tests/test_anthropic_files_client.py tests/test_pdf_helpers.py tests/test_cli_files.py --tb=line -q | tail -10
```

Expected: all green.

- [ ] **Step 2: Anthropic regression sweep**

```bash
.venv/bin/python -m pytest tests/ -k "anthropic or files_client or pdf_helpers or cli_files" --tb=line -q | tail -10
```

Expected: green.

- [ ] **Step 3: Full ruff**

```bash
.venv/bin/ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: clean.

- [ ] **Step 4: Push branch**

```bash
cd /private/tmp/oc-sp23-pdf-files-integration
git push -u origin feat/sp23-pdf-files-cache-integration
```

- [ ] **Step 5: Open PR (depends on #359 + #360)**

```bash
gh pr create --title "feat(anthropic-provider): PDF Files-API caching (SP2+SP3 integration, default OFF)" --body "$(cat <<'EOF'
## Summary

SP2+SP3 integration. Spec: \`docs/superpowers/specs/2026-05-02-sp23-pdf-files-cache-integration-design.md\`. Plan: \`docs/superpowers/plans/2026-05-02-sp23-pdf-files-cache-integration.md\`.

Wires SP2's PDF document-block builder through SP3's Anthropic Files API client via a content-hash cache. **Default OFF.** Opt-in saves bandwidth on multi-turn PDF conversations.

- **New module**: \`files_cache.py\` — JSON-backed content-hash → file_id cache (SHA-256 key, fail-open I/O)
- **New resolver**: \`_resolve_anthropic_files_cache_enabled\` (env var + runtime flag, mirrors SP4's pattern)
- **Extended**: \`_build_anthropic_pdf_block\` becomes async, takes optional \`cache\` + \`client\`. Cache hit → file_id block. Cache miss → upload + cache. Upload failure → fall back to base64 (preserves SP2's behavior).
- **No new CLI command.** Pruning via existing \`oc files delete\`. Cache file is plain JSON if you want to inspect.

### Default-OFF behavior preservation
Without the env var or runtime flag, behavior is byte-identical to SP2. \`_build_anthropic_pdf_block\` returns the same base64-inline block when cache/client kwargs are None.

### Test plan
- [x] \`pytest tests/test_anthropic_files_cache.py\` — 8 unit tests
- [x] \`pytest tests/test_anthropic_pdf_caching.py\` — 4 resolver + 4 integration tests
- [x] SP2 regression: \`pytest tests/test_anthropic_provider_pdf.py\` — green
- [x] SP3 regression: \`pytest tests/test_anthropic_files_client.py\` + \`test_cli_files.py\` — green
- [x] Full ruff — clean

### Depends on
- PR #359 (SP2) — defines \`_build_anthropic_pdf_block\` and PDF dispatcher
- PR #360 (SP3) — defines \`AnthropicFilesClient\`

This PR can only merge after both #359 and #360 land. The branch already has both merged in.

### Caveats documented in \`docs/cli/files.md\`
- Workspace-scoped (multi-key visibility)
- No auto-prune (manual via \`oc files delete\` or delete the JSON cache file)
- 404 on stale cached file_id surfaces to the user; cache miss re-uploads (one bad request before recovery)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 6: Report PR URL**

---

## Self-Review

**Spec coverage:**
| Spec section | Task |
|---|---|
| §6.1 FilesCache module | Task 1 |
| §6.2 Cache-enable resolver | Task 2 |
| §6.3 _build_anthropic_pdf_block extension | Task 3 |
| §6.4 Provider call-site wire-up | Task 4 |
| §6.5 404 invalidation (documented limitation) | Task 5 (docs only — no code per design decision) |
| §6.6 Tests | All TDD tasks |
| §6.7 Documentation | Task 5 |

**Placeholder scan:** No "TBD" / "fill in later" outside the conditional `lazy-import` discussion in Task 3 Step 4 (which gives 2 alternatives + says "try simplest first").

**Type consistency:**
- `FilesCache(cache_path: Path)` consistent across tasks
- `CacheEntry` field names: `file_id`, `uploaded_at`, `filename`, `size_bytes` (consistent)
- `hash_file_bytes(data: bytes) -> str` consistent
- `_resolve_anthropic_files_cache_enabled(runtime) -> bool` consistent
- `_build_anthropic_pdf_block(path, *, cache=None, client=None) -> dict | None` consistent (and matches existing SP2 signature except for the new kwargs)
- `OPENCOMPUTER_ANTHROPIC_FILES_CACHE` env var name consistent
