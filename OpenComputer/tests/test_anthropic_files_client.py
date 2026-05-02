"""Tests for AnthropicFilesClient — typed async wrapper for Anthropic Files API."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest

CLIENT_PATH = (
    Path(__file__).parent.parent
    / "extensions" / "anthropic-provider" / "files_client.py"
)
_MODULE_NAME = "_test_anthropic_files_client"


def _load_module():
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, CLIENT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
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


def _mock_transport(handler):
    """Build an httpx MockTransport from a handler callable."""
    return httpx.MockTransport(handler)


def _client_with_mock(monkeypatch, handler, **overrides):
    module = _load_module()
    transport = _mock_transport(handler)

    client_kwargs = {"api_key": "sk-test", "base_url": "https://example.com", **overrides}
    client = module.AnthropicFilesClient(**client_kwargs)
    monkeypatch.setattr(
        client,
        "_make_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=client._timeout_s),
    )
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
