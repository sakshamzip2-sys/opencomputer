# SP3 — Files API + `oc files` CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a typed async client for Anthropic's Files API + an `oc files` CLI for upload/list/delete/download/info management.

**Architecture:** Standalone client module in `extensions/anthropic-provider/files_client.py` (no provider entanglement). CLI subcommand group in `opencomputer/cli_files.py` registered via `app.add_typer`. Both share the same Anthropic API key resolution as the existing provider. Free operations (uploads/lists/deletes are free per Anthropic docs).

**Tech Stack:** Python 3.12+, httpx (async), Typer (CLI), rich (table output), pytest, no new third-party deps.

**Spec:** [`docs/superpowers/specs/2026-05-02-sp3-files-api-design.md`](../specs/2026-05-02-sp3-files-api-design.md)

---

## Pre-flight

- [ ] **Step 0a: Verify worktree**

```bash
cd /private/tmp/oc-sp3-files-api
git status
git branch --show-current
```

Expected: clean tree, on `feat/sp3-files-api-and-spillover` (despite the name, we dropped the spillover scope — it's already done).

- [ ] **Step 0b: Baseline pytest scope**

```bash
cd OpenComputer
pytest tests/ -k "anthropic or cli or files" --tb=short -q 2>&1 | tail -10
```

Expected: all pass. Record count.

- [ ] **Step 0c: Baseline ruff**

```bash
ruff check opencomputer/ extensions/anthropic-provider/ tests/
```

Expected: clean.

- [ ] **Step 0d: Verify httpx is available (used by other modules already)**

```bash
python -c "import httpx; print(httpx.__version__)"
```

Expected: prints version.

---

## Task 1: FilesAPIError + FileMetadata + AnthropicFilesClient skeleton

**Files:**
- Create: `extensions/anthropic-provider/files_client.py`
- Test: `tests/test_anthropic_files_client.py` (NEW)

- [ ] **Step 1: Write the failing test for the dataclasses + error type**

Create `tests/test_anthropic_files_client.py`:

```python
"""Tests for AnthropicFilesClient — typed async wrapper for Anthropic Files API."""
from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path

import httpx
import pytest

CLIENT_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "files_client.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "_test_anthropic_files_client", CLIENT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_file_metadata_from_response_parses_iso8601():
    module = _load_module()
    payload = {
        "id": "file_abc123",
        "filename": "doc.pdf",
        "mime_type": "application/pdf",
        "size_bytes": 1024,
        "created_at": "2026-05-02T10:00:00Z",
        "downloadable": False,
    }
    meta = module.FileMetadata.from_response(payload)
    assert meta.id == "file_abc123"
    assert meta.filename == "doc.pdf"
    assert meta.mime_type == "application/pdf"
    assert meta.size_bytes == 1024
    assert isinstance(meta.created_at, datetime)
    assert meta.downloadable is False


def test_file_metadata_handles_missing_downloadable():
    module = _load_module()
    payload = {
        "id": "file_x",
        "filename": "f.txt",
        "mime_type": "text/plain",
        "size_bytes": 1,
        "created_at": "2026-05-02T00:00:00Z",
    }
    meta = module.FileMetadata.from_response(payload)
    assert meta.downloadable is False  # default


def test_files_api_error_has_status_code():
    module = _load_module()
    err = module.FilesAPIError("oops", status_code=429)
    assert str(err) == "oops"
    assert err.status_code == 429


def test_client_constructs_with_api_key():
    module = _load_module()
    client = module.AnthropicFilesClient(api_key="sk-test", base_url="https://example.com")
    assert client._api_key == "sk-test"
    assert client._base_url == "https://example.com"


def test_client_strips_trailing_slash_from_base_url():
    module = _load_module()
    client = module.AnthropicFilesClient(api_key="sk-test", base_url="https://example.com/")
    assert client._base_url == "https://example.com"


def test_client_headers_include_required_fields():
    module = _load_module()
    client = module.AnthropicFilesClient(api_key="sk-test")
    headers = client._headers()
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["anthropic-beta"] == "files-api-2025-04-14"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_anthropic_files_client.py -v
```

Expected: FAIL — `files_client.py` doesn't exist.

- [ ] **Step 3: Implement the skeleton**

Create `extensions/anthropic-provider/files_client.py`:

```python
"""Anthropic Files API client.

Beta header: files-api-2025-04-14
Endpoints (per https://docs.claude.com/en/api/files):
  POST   /v1/files                multipart upload
  GET    /v1/files                list (paginated)
  GET    /v1/files/{id}           metadata
  GET    /v1/files/{id}/content   download (only for model-created files)
  DELETE /v1/files/{id}           delete

All operations are FREE; token usage in /v1/messages is what costs.
Workspace-scoped (all keys in a workspace see each other's files).
500 MB per file, 500 GB per org, ~100 req/min beta rate limit.
NOT ZDR-eligible.
"""
from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.anthropic.com"
BETA_HEADER = "files-api-2025-04-14"
ANTHROPIC_VERSION = "2023-06-01"
MAX_FILE_BYTES = 500 * 1024 * 1024  # 500 MB
RATE_LIMIT_HINT = "Anthropic Files API beta rate limit is ~100 req/min."


@dataclass
class FileMetadata:
    """Metadata for a file in the Anthropic Files API workspace."""
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
    """Async client for the Anthropic Files API.

    Operations are FREE per Anthropic docs; only token usage in
    /v1/messages costs. Workspace-scoped: all API keys in your
    workspace see each other's files. NOT ZDR-eligible — uploaded
    files are retained per Anthropic's standard retention policy.
    """

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

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "anthropic-beta": BETA_HEADER,
        }

    def _make_client(self) -> httpx.AsyncClient:
        """Test seam — replace with httpx.MockTransport in tests."""
        return httpx.AsyncClient(timeout=self._timeout_s)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_anthropic_files_client.py -v
```

Expected: 6/6 PASS.

- [ ] **Step 5: Commit**

```bash
cd /private/tmp/oc-sp3-files-api
git add OpenComputer/extensions/anthropic-provider/files_client.py OpenComputer/tests/test_anthropic_files_client.py
git commit -m "feat(anthropic-provider): files_client.py skeleton (FilesAPIError + FileMetadata + AnthropicFilesClient)"
```

---

## Task 2: upload + list + get_metadata methods

**Files:**
- Modify: `extensions/anthropic-provider/files_client.py` (add 3 methods)
- Modify: `tests/test_anthropic_files_client.py` (add tests)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_anthropic_files_client.py`:

```python
def _mock_transport(handler):
    """Build an httpx MockTransport from a handler callable."""
    return httpx.MockTransport(handler)


def _client_with_mock(monkeypatch, handler, **overrides):
    module = _load_module()
    transport = _mock_transport(handler)

    def _patched_make_client(self):
        return httpx.AsyncClient(transport=transport, timeout=self._timeout_s)

    client_kwargs = {"api_key": "sk-test", "base_url": "https://example.com", **overrides}
    client = module.AnthropicFilesClient(**client_kwargs)
    monkeypatch.setattr(client, "_make_client", lambda: httpx.AsyncClient(transport=transport, timeout=client._timeout_s))
    return module, client


@pytest.mark.asyncio
async def test_upload_returns_metadata(monkeypatch, tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/files"
        assert request.headers["x-api-key"] == "sk-test"
        assert request.headers["anthropic-beta"] == "files-api-2025-04-14"
        return httpx.Response(
            200,
            json={
                "id": "file_xyz",
                "filename": "doc.pdf",
                "mime_type": "application/pdf",
                "size_bytes": pdf.stat().st_size,
                "created_at": "2026-05-02T10:00:00Z",
                "downloadable": False,
            },
        )

    _module, client = _client_with_mock(monkeypatch, handler)
    meta = await client.upload(pdf)
    assert meta.id == "file_xyz"
    assert meta.filename == "doc.pdf"
    assert meta.size_bytes == pdf.stat().st_size


@pytest.mark.asyncio
async def test_upload_rejects_oversize_local(monkeypatch, tmp_path):
    """Oversize file caught locally — never reaches the API."""
    module = _load_module()
    pdf = tmp_path / "huge.pdf"
    pdf.write_bytes(b"x" * (module.MAX_FILE_BYTES + 1))

    def handler(request):
        raise AssertionError("API should not be called for oversize file")

    _module, client = _client_with_mock(monkeypatch, handler)
    with pytest.raises(module.FilesAPIError, match="500 MB"):
        await client.upload(pdf)


@pytest.mark.asyncio
async def test_upload_raises_for_missing_file(monkeypatch, tmp_path):
    module = _load_module()
    def handler(request):
        raise AssertionError("API should not be called")
    _module, client = _client_with_mock(monkeypatch, handler)
    with pytest.raises(FileNotFoundError):
        await client.upload(tmp_path / "missing.pdf")


@pytest.mark.asyncio
async def test_list_returns_files(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/files"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "file_a",
                        "filename": "a.pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": 100,
                        "created_at": "2026-05-02T10:00:00Z",
                    },
                    {
                        "id": "file_b",
                        "filename": "b.png",
                        "mime_type": "image/png",
                        "size_bytes": 200,
                        "created_at": "2026-05-02T11:00:00Z",
                    },
                ]
            },
        )

    _module, client = _client_with_mock(monkeypatch, handler)
    files = await client.list()
    assert len(files) == 2
    assert files[0].id == "file_a"
    assert files[1].filename == "b.png"


@pytest.mark.asyncio
async def test_get_metadata(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/files/file_xyz"
        return httpx.Response(
            200,
            json={
                "id": "file_xyz",
                "filename": "doc.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 100,
                "created_at": "2026-05-02T10:00:00Z",
                "downloadable": False,
            },
        )

    _module, client = _client_with_mock(monkeypatch, handler)
    meta = await client.get_metadata("file_xyz")
    assert meta.id == "file_xyz"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_anthropic_files_client.py -v -k "upload or list_returns or get_metadata"
```

Expected: FAIL — methods don't exist.

- [ ] **Step 3: Implement the methods**

Append to `extensions/anthropic-provider/files_client.py` (after the class header):

```python
    async def upload(self, path: Path) -> FileMetadata:
        """Upload a file; returns metadata including the new file_id.

        Raises:
            FileNotFoundError: if path does not exist.
            FilesAPIError: if file exceeds 500 MB or API rejects.
        """
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
        """Fetch metadata for a single file."""
        async with self._make_client() as client:
            resp = await client.get(
                f"{self._base_url}/v1/files/{file_id}",
                headers=self._headers(),
            )
        _raise_for_status(resp)
        return FileMetadata.from_response(resp.json())


def _guess_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _raise_for_status(resp: httpx.Response) -> None:
    """Stub — Task 4 fleshes this out with status-specific messages."""
    if not resp.is_success:
        raise FilesAPIError(
            f"Files API error (HTTP {resp.status_code}): {resp.text}",
            status_code=resp.status_code,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_anthropic_files_client.py -v
```

Expected: all PASS (Task 1's 6 + Task 2's 5 = 11 total).

- [ ] **Step 5: Commit**

```bash
cd /private/tmp/oc-sp3-files-api
git add OpenComputer/extensions/anthropic-provider/files_client.py OpenComputer/tests/test_anthropic_files_client.py
git commit -m "feat(anthropic-provider): files_client upload + list + get_metadata methods"
```

---

## Task 3: download + delete methods

**Files:**
- Modify: `extensions/anthropic-provider/files_client.py` (add 2 methods)
- Modify: `tests/test_anthropic_files_client.py` (add tests)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_anthropic_files_client.py`:

```python
@pytest.mark.asyncio
async def test_download_writes_bytes(monkeypatch, tmp_path):
    payload = b"file content here"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/files/file_xyz/content"
        return httpx.Response(200, content=payload)

    _module, client = _client_with_mock(monkeypatch, handler)
    out = tmp_path / "downloaded.bin"
    bytes_written = await client.download("file_xyz", out)
    assert bytes_written == len(payload)
    assert out.read_bytes() == payload


@pytest.mark.asyncio
async def test_download_403_for_non_downloadable(monkeypatch, tmp_path):
    module = _load_module()

    def handler(request):
        return httpx.Response(403, text="not downloadable")

    _module, client = _client_with_mock(monkeypatch, handler)
    with pytest.raises(module.FilesAPIError) as exc_info:
        await client.download("file_userupload", tmp_path / "out")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_delete(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/files/file_xyz"
        return httpx.Response(204)

    _module, client = _client_with_mock(monkeypatch, handler)
    await client.delete("file_xyz")  # no return; should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_anthropic_files_client.py -v -k "download or delete"
```

Expected: FAIL — `download` and `delete` not defined.

- [ ] **Step 3: Implement the methods**

Append (before the `_guess_mime` and `_raise_for_status` module-level helpers):

```python
    async def download(self, file_id: str, output_path: Path) -> int:
        """Download a model-created file. Returns bytes written.

        Raises FilesAPIError(403) if the file isn't downloadable —
        Anthropic only permits download of skill/code-exec outputs,
        not user-uploaded files.
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
        """Delete a file. No return; raises on error."""
        async with self._make_client() as client:
            resp = await client.delete(
                f"{self._base_url}/v1/files/{file_id}",
                headers=self._headers(),
            )
        _raise_for_status(resp)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_anthropic_files_client.py -v -k "download or delete"
```

Expected: all 3 PASS.

- [ ] **Step 5: Commit**

```bash
cd /private/tmp/oc-sp3-files-api
git add OpenComputer/extensions/anthropic-provider/files_client.py OpenComputer/tests/test_anthropic_files_client.py
git commit -m "feat(anthropic-provider): files_client download + delete methods"
```

---

## Task 4: Status-specific error handling

**Files:**
- Modify: `extensions/anthropic-provider/files_client.py` (flesh out `_raise_for_status`)
- Modify: `tests/test_anthropic_files_client.py` (add tests)

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_anthropic_files_client.py`:

```python
@pytest.mark.asyncio
async def test_rate_limit_429_raises_with_hint(monkeypatch):
    module = _load_module()
    def handler(request):
        return httpx.Response(429, text="too many requests")

    _module, client = _client_with_mock(monkeypatch, handler)
    with pytest.raises(module.FilesAPIError) as exc_info:
        await client.list()
    assert exc_info.value.status_code == 429
    assert "100 req/min" in str(exc_info.value)


@pytest.mark.asyncio
async def test_storage_quota_403_raises_with_hint(monkeypatch):
    module = _load_module()
    def handler(request):
        return httpx.Response(403, text="storage quota exceeded for organization")

    _module, client = _client_with_mock(monkeypatch, handler)
    with pytest.raises(module.FilesAPIError) as exc_info:
        await client.list()
    assert exc_info.value.status_code == 403
    assert "500 GB" in str(exc_info.value)


@pytest.mark.asyncio
async def test_404_raises(monkeypatch):
    module = _load_module()
    def handler(request):
        return httpx.Response(404, text="file not found")

    _module, client = _client_with_mock(monkeypatch, handler)
    with pytest.raises(module.FilesAPIError) as exc_info:
        await client.get_metadata("nonexistent")
    assert exc_info.value.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_anthropic_files_client.py -v -k "rate_limit or storage_quota or 404"
```

Expected: tests fail because the stub `_raise_for_status` lumps everything into a generic message.

- [ ] **Step 3: Replace the stub _raise_for_status**

Replace the existing `_raise_for_status` function with:

```python
def _raise_for_status(resp: httpx.Response) -> None:
    """Translate Files API HTTP errors into FilesAPIError with helpful messages."""
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
    if status == 403:
        raise FilesAPIError(
            f"forbidden (403). Body: {body}",
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

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_anthropic_files_client.py -v
```

Expected: all 14 PASS (6 + 5 + 3 + 3).

- [ ] **Step 5: Commit**

```bash
cd /private/tmp/oc-sp3-files-api
git add OpenComputer/extensions/anthropic-provider/files_client.py OpenComputer/tests/test_anthropic_files_client.py
git commit -m "feat(anthropic-provider): status-specific FilesAPIError messages (429/403/404/generic)"
```

---

## Task 5: `oc files` CLI command group

**Files:**
- Create: `opencomputer/cli_files.py`
- Modify: `opencomputer/cli.py` (register the subcommand group)
- Test: `tests/test_cli_files.py` (NEW)

- [ ] **Step 1: Find the existing `add_typer` registration pattern**

```bash
grep -n "add_typer" /private/tmp/oc-sp3-files-api/OpenComputer/opencomputer/cli.py
```

Note the imports + the registration line (e.g., `app.add_typer(profile_app, name="profile")`).

- [ ] **Step 2: Find how API key resolution works in the existing provider**

```bash
grep -n "ANTHROPIC_API_KEY\|api_key" extensions/anthropic-provider/provider.py | head -10
```

Note the env var pattern. The CLI will resolve the same way.

- [ ] **Step 3: Write the failing tests**

Create `tests/test_cli_files.py`:

```python
"""Tests for the `oc files` CLI subcommand group."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner


def _runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def test_files_missing_api_key_exits_with_message(monkeypatch):
    """No ANTHROPIC_API_KEY → friendly error + exit 1."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from opencomputer.cli_files import files_app

    result = _runner().invoke(files_app, ["list"])
    assert result.exit_code != 0
    assert "ANTHROPIC_API_KEY" in (result.stderr or result.stdout)


def test_files_list_prints_table(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from opencomputer.cli_files import files_app
    from extensions_anthropic_provider_files_client import FileMetadata
    from datetime import datetime, timezone

    fake_files = [
        FileMetadata(
            id="file_a",
            filename="a.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            created_at=datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc),
            downloadable=False,
        ),
    ]

    with patch("opencomputer.cli_files.AnthropicFilesClient") as MockClient:
        instance = MockClient.return_value
        instance.list = AsyncMock(return_value=fake_files)
        result = _runner().invoke(files_app, ["list"])

    assert result.exit_code == 0
    assert "file_a" in result.stdout
    assert "a.pdf" in result.stdout


def test_files_upload_prints_id(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from opencomputer.cli_files import files_app
    from extensions_anthropic_provider_files_client import FileMetadata
    from datetime import datetime, timezone

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    fake_meta = FileMetadata(
        id="file_new",
        filename="doc.pdf",
        mime_type="application/pdf",
        size_bytes=pdf.stat().st_size,
        created_at=datetime(2026, 5, 2, 10, 0, 0, tzinfo=timezone.utc),
        downloadable=False,
    )

    with patch("opencomputer.cli_files.AnthropicFilesClient") as MockClient:
        instance = MockClient.return_value
        instance.upload = AsyncMock(return_value=fake_meta)
        result = _runner().invoke(files_app, ["upload", str(pdf)])

    assert result.exit_code == 0
    assert "file_new" in result.stdout


def test_files_delete_calls_client(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from opencomputer.cli_files import files_app

    with patch("opencomputer.cli_files.AnthropicFilesClient") as MockClient:
        instance = MockClient.return_value
        instance.delete = AsyncMock(return_value=None)
        result = _runner().invoke(files_app, ["delete", "file_xyz"])

    assert result.exit_code == 0
    instance.delete.assert_called_once_with("file_xyz")


def test_files_download_writes_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from opencomputer.cli_files import files_app

    out_path = tmp_path / "downloaded.bin"
    with patch("opencomputer.cli_files.AnthropicFilesClient") as MockClient:
        instance = MockClient.return_value
        instance.download = AsyncMock(return_value=42)
        result = _runner().invoke(files_app, ["download", "file_xyz", str(out_path)])

    assert result.exit_code == 0
    assert "42" in result.stdout or "bytes" in result.stdout.lower()
    instance.download.assert_called_once()
```

Note: the test imports use the synthetic module name `extensions_anthropic_provider_files_client` — this is because the actual extension module isn't on `sys.path`. The CLI tests rely on the test being able to access `FileMetadata`. Adapt the import strategy as needed once Step 4 is implemented (the CLI file will need to handle this too).

- [ ] **Step 4: Implement the CLI**

Create `opencomputer/cli_files.py`:

```python
"""`oc files` CLI subcommand group — manage Anthropic Files API uploads.

Wraps :class:`AnthropicFilesClient` from
``extensions/anthropic-provider/files_client.py``.

All operations are FREE per Anthropic docs; only message tokens cost.
Workspace-scoped: all API keys in your workspace see each other's files.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

# Lazy-load the FilesClient module from the extension dir.
# The Anthropic provider lives in extensions/ which isn't on sys.path,
# so import via spec_from_file_location.
_REPO_ROOT = Path(__file__).parent.parent
_FILES_CLIENT_PATH = (
    _REPO_ROOT / "extensions" / "anthropic-provider" / "files_client.py"
)


def _load_files_client_module():
    spec = importlib.util.spec_from_file_location(
        "extensions_anthropic_provider_files_client",
        _FILES_CLIENT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


_module = _load_files_client_module()
AnthropicFilesClient = _module.AnthropicFilesClient
FilesAPIError = _module.FilesAPIError
FileMetadata = _module.FileMetadata


files_app = typer.Typer(
    help="Manage Anthropic Files API uploads (upload/list/delete/download/info).",
    no_args_is_help=True,
)
console = Console()


def _resolve_api_key() -> str:
    """Get the Anthropic API key from env. Exits with message if absent."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        typer.echo(
            "error: ANTHROPIC_API_KEY not set. Set it or configure your "
            "Anthropic provider before using `oc files`.",
            err=True,
        )
        raise typer.Exit(code=1)
    return key


def _client() -> "AnthropicFilesClient":
    return AnthropicFilesClient(api_key=_resolve_api_key())


def _format_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _run(coro):
    """Run an async coroutine; surface FilesAPIError cleanly."""
    try:
        return asyncio.run(coro)
    except FilesAPIError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@files_app.command("list")
def cmd_list(limit: int = typer.Option(50, "--limit", "-n", help="Max files to show.")):
    """List uploaded files in this workspace."""
    files = _run(_client().list(limit=limit))
    if not files:
        typer.echo("(no files uploaded)")
        return
    table = Table(title=f"Anthropic Files ({len(files)})")
    table.add_column("ID")
    table.add_column("Filename")
    table.add_column("MIME")
    table.add_column("Size")
    table.add_column("Created")
    table.add_column("Downloadable")
    for f in files:
        table.add_row(
            f.id,
            f.filename,
            f.mime_type,
            _format_size(f.size_bytes),
            f.created_at.strftime("%Y-%m-%d %H:%M"),
            "yes" if f.downloadable else "no",
        )
    console.print(table)


@files_app.command("upload")
def cmd_upload(path: Path = typer.Argument(..., help="Local file path to upload.")):
    """Upload a file; prints the resulting file_id."""
    meta = _run(_client().upload(path))
    typer.echo(f"uploaded: {meta.id} ({meta.filename}, {_format_size(meta.size_bytes)})")


@files_app.command("delete")
def cmd_delete(file_id: str = typer.Argument(..., help="File ID to delete.")):
    """Delete a file from the workspace."""
    _run(_client().delete(file_id))
    typer.echo(f"deleted: {file_id}")


@files_app.command("download")
def cmd_download(
    file_id: str = typer.Argument(..., help="File ID (must be model-created)."),
    output: Path = typer.Argument(..., help="Local output path."),
):
    """Download a model-created file (skills / code-execution outputs).

    User-uploaded files cannot be downloaded — Anthropic API restriction.
    """
    bytes_written = _run(_client().download(file_id, output))
    typer.echo(f"downloaded: {file_id} -> {output} ({bytes_written} bytes)")


@files_app.command("info")
def cmd_info(file_id: str = typer.Argument(..., help="File ID.")):
    """Show metadata for a single file."""
    meta = _run(_client().get_metadata(file_id))
    typer.echo(f"id:           {meta.id}")
    typer.echo(f"filename:     {meta.filename}")
    typer.echo(f"mime_type:    {meta.mime_type}")
    typer.echo(f"size_bytes:   {meta.size_bytes} ({_format_size(meta.size_bytes)})")
    typer.echo(f"created_at:   {meta.created_at.isoformat()}")
    typer.echo(f"downloadable: {'yes' if meta.downloadable else 'no'}")
```

- [ ] **Step 5: Register the subcommand group in opencomputer/cli.py**

Find the section with other `app.add_typer(...)` calls. Add:

```python
from opencomputer.cli_files import files_app
app.add_typer(files_app, name="files")
```

In the appropriate import block + registration block (alphabetical or grouped per existing convention).

- [ ] **Step 6: Run tests to verify they pass**

```bash
pytest tests/test_cli_files.py -v
```

Expected: 5 PASS.

- [ ] **Step 7: Smoke-check the CLI registration**

```bash
opencomputer files --help
```

Expected: shows `list`, `upload`, `delete`, `download`, `info` subcommands.

- [ ] **Step 8: Commit**

```bash
cd /private/tmp/oc-sp3-files-api
git add OpenComputer/opencomputer/cli_files.py OpenComputer/opencomputer/cli.py OpenComputer/tests/test_cli_files.py
git commit -m "feat(cli): oc files subcommand group (list/upload/delete/download/info)"
```

---

## Task 6: Documentation

**Files:**
- Create: `docs/cli/files.md` (NEW)

- [ ] **Step 1: Create the doc**

```bash
mkdir -p /private/tmp/oc-sp3-files-api/OpenComputer/docs/cli
```

Create `docs/cli/files.md`:

```markdown
# `oc files` — Anthropic Files API management

Manage files uploaded to your Anthropic workspace. All operations are
FREE per Anthropic — only token usage in `/v1/messages` costs money.

## Commands

### `oc files list [--limit N]`
Show all files in your workspace. Defaults to 50.

```
$ oc files list
                Anthropic Files (3)
┏━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━┳──────────────────┳━━━━━━━━━━━━━━┓
┃ ID       ┃ Filename ┃ MIME            ┃ Size  ┃ Created          ┃ Downloadable ┃
┡━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━╇──────────────────╇━━━━━━━━━━━━━━┩
│ file_abc │ doc.pdf  │ application/pdf │ 2.3 MB│ 2026-05-02 10:00 │ no           │
│ file_def │ data.csv │ text/csv        │ 412 KB│ 2026-05-02 11:15 │ no           │
│ file_ghi │ chart.png│ image/png       │ 18 KB │ 2026-05-02 12:30 │ yes          │
└──────────┴──────────┴─────────────────┴───────┴──────────────────┴──────────────┘
```

### `oc files upload <path>`
Upload a local file. Prints the resulting `file_id` you can reference
elsewhere.

```
$ oc files upload report.pdf
uploaded: file_xyz789 (report.pdf, 1.2 MB)
```

Limit: 500 MB per file.

### `oc files info <file_id>`
Show metadata for a single file.

### `oc files delete <file_id>`
Delete a file from the workspace.

```
$ oc files delete file_xyz789
deleted: file_xyz789
```

### `oc files download <file_id> <output_path>`
Download a model-created file (e.g., a chart or PowerPoint generated by
a skill). **User-uploaded files cannot be downloaded** — Anthropic API
restriction. Attempting to download a user-uploaded file returns 403.

## Caveats

| Caveat | Detail |
|---|---|
| **Workspace-scoped** | All API keys in your workspace see each other's files. |
| **Not ZDR-eligible** | Files are retained per Anthropic's standard retention policy. |
| **500 MB per file, 500 GB per org** | Hard limits. |
| **~100 req/min beta rate limit** | If you hit it, the CLI prints a helpful message and exits 1. |
| **Files persist until DELETE** | No auto-expiry. Run `oc files list` periodically + `oc files delete` to clean up. |
| **Download restriction** | Only files the model created (skills / code-execution outputs) can be downloaded. |

## How this fits with `oc` channels

Currently (after SP2), PDFs sent through Telegram or other channel
adapters are uploaded inline as base64 in each request. A planned
follow-up (post-SP3 merge) will integrate `oc files` with the provider's
PDF block builder so a single PDF gets uploaded ONCE, then referenced by
`file_id` across turns — saving bandwidth on multi-turn document
discussions.

## Implementation references

- Spec: `OpenComputer/docs/superpowers/specs/2026-05-02-sp3-files-api-design.md`
- Plan: `OpenComputer/docs/superpowers/plans/2026-05-02-sp3-files-api.md`
- Client: `extensions/anthropic-provider/files_client.py`
- CLI:    `opencomputer/cli_files.py`
- Anthropic Files API docs: https://docs.claude.com/en/build-with-claude/files
```

- [ ] **Step 2: Commit**

```bash
cd /private/tmp/oc-sp3-files-api
git add OpenComputer/docs/cli/files.md
git commit -m "docs(cli): oc files command reference"
```

---

## Task 7: Final verification + push + PR

- [ ] **Step 1: Run the FULL pytest suite**

```bash
cd /private/tmp/oc-sp3-files-api/OpenComputer
pytest tests/ --tb=line -q --ignore=tests/test_voice 2>&1 | tail -10
```

Expected: all pass.

- [ ] **Step 2: Run FULL ruff**

```bash
ruff check opencomputer/ plugin_sdk/ extensions/ tests/
```

Expected: clean.

- [ ] **Step 3: Smoke-check CLI**

```bash
opencomputer files --help
opencomputer files list --help
opencomputer files upload --help
```

Expected: each prints help text successfully.

- [ ] **Step 4: Push the branch**

```bash
cd /private/tmp/oc-sp3-files-api
git push -u origin feat/sp3-files-api-and-spillover
```

- [ ] **Step 5: Open the PR**

```bash
gh pr create --title "feat(files): Anthropic Files API client + oc files CLI (SP3)" --body "$(cat <<'EOF'
## Summary

SP3 of the Anthropic-API-parity scope. Spec: \`docs/superpowers/specs/2026-05-02-sp3-files-api-design.md\`. Plan: \`docs/superpowers/plans/2026-05-02-sp3-files-api.md\`.

- **AnthropicFilesClient**: typed async wrapper for the Anthropic Files API (upload/list/get_metadata/download/delete) with status-specific error messages (429 rate limit, 403 quota, 404 not found, generic).
- **\`oc files\` CLI**: 5 subcommands (list/upload/delete/download/info) for managing workspace files. Resolves \`ANTHROPIC_API_KEY\` from env; clean error UX on missing key, rate limits, and quota.
- **Free operations** per Anthropic docs (only message tokens cost).
- **Tool-result spillover dropped from scope** — discovered already fully wired in main (\`opencomputer/agent/tool_result_storage.py\` + \`loop.py:3280\`); no work needed.

### Test plan
- [x] \`pytest tests/test_anthropic_files_client.py\` — 14 unit tests (httpx.MockTransport, no network)
- [x] \`pytest tests/test_cli_files.py\` — 5 CLI tests (Typer CliRunner + AsyncMock)
- [x] \`pytest tests/\` — full suite green
- [x] \`ruff check\` — clean
- [x] Smoke: \`oc files --help\` shows all 5 subcommands

### Out of scope (deferred follow-ups)
- **SP2 + SP3 integration**: auto-cache PDF uploads via Files API in the provider's \`_build_pdf_block\` (small follow-up after both merge — adds bandwidth-saving for multi-turn PDF discussions).
- **Content-hash deduplication**: natural home is the SP2-integration follow-up.
- **\`oc files cleanup --days N\`**: YAGNI until manual cleanup gets painful.
- **Multi-provider Files API abstraction**: only matters if a second provider gets one.

### Caveats users should know
- Workspace-scoped (all API keys see each other's files)
- Not ZDR-eligible
- Files persist until explicit DELETE (no auto-expiry)
- Only model-created files are downloadable (skills/code-execution outputs)
- ~100 req/min beta rate limit

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
| §6.1 AnthropicFilesClient module | Tasks 1, 2, 3, 4 |
| §6.2 oc files CLI module | Task 5 |
| §6.3 Registration in cli.py | Task 5 Step 5 |
| §6.4 Error handling | Task 4 (status-specific messages); Task 5 (CLI exit codes) |
| §6.5 Tests | All tasks (TDD) |
| §6.6 Documentation | Task 6 |

**Placeholder scan:** No "TBD" / "fill in later" outside conditional steps.

**Type consistency:**
- `FileMetadata` dataclass field names consistent (id, filename, mime_type, size_bytes, created_at, downloadable).
- `AnthropicFilesClient` method names: `upload`, `list`, `get_metadata`, `download`, `delete`.
- `FilesAPIError(message, *, status_code=None)` signature consistent across raises.
- `BETA_HEADER = "files-api-2025-04-14"` used consistently.
