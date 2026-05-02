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
from dataclasses import dataclass
from datetime import datetime

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
    def from_response(cls, data: dict) -> FileMetadata:
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
