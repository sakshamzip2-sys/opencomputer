"""Tests for the dashboard ``GET /api/v1/memory/status`` REST endpoint.

REST mirror of the wire ``memory.status`` RPC — closes the dashboard SPA's
"fresh-connect blindness" gap so it can seed its memory panel from initial
state instead of waiting for the first ``memory.write`` SSE event.

Coverage:

* Helper-level — :func:`_collect_entries` resolves the active profile's
  MemoryConfig, reads files, returns wire-compatible dicts. Edge cases:
  no MemoryConfig, missing files, unreadable files.
* Route-level — endpoint returns 200 with the expected envelope and
  schema-compatible payload, including the empty-fallback behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencomputer.dashboard.routes.memory import (
    _collect_entries,
    _load_memory_config,
)
from opencomputer.dashboard.routes.memory import (
    router as memory_router,
)


@dataclass
class _StubMemoryConfig:
    """Minimal duck-type for MemoryConfig — enough fields for the helper."""

    declarative_path: Path
    user_path: Path
    memory_char_limit: int = 4000
    user_char_limit: int = 2000


def _patch_loader(monkeypatch: pytest.MonkeyPatch, config: Any | None) -> None:
    """Replace ``_load_memory_config`` with a fixed return."""
    monkeypatch.setattr(
        "opencomputer.dashboard.routes.memory._load_memory_config",
        lambda: config,
    )


def _make_app() -> FastAPI:
    """Minimal FastAPI app wired with just the memory router for testing."""
    app = FastAPI()
    app.include_router(memory_router)
    return app


# ─── Helper unit tests ──────────────────────────────────────────────


class TestCollectEntriesHelper:
    """The helper handles every failure mode without raising."""

    def test_no_memory_config_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_loader(monkeypatch, None)
        assert _collect_entries() == []

    def test_missing_files_report_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        cfg = _StubMemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
        )
        _patch_loader(monkeypatch, cfg)
        entries = _collect_entries()

        assert len(entries) == 2
        # Sorted alphabetical: MEMORY.md before USER.md
        assert entries[0]["target"] == "MEMORY.md"
        assert entries[0]["content_size"] == 0
        assert entries[0]["cap_limit"] == 4000
        assert entries[0]["pct"] == 0.0
        assert entries[1]["target"] == "USER.md"
        assert entries[1]["cap_limit"] == 2000

    def test_populated_files_reflect_disk_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        memory_text = "rule one\n\nrule two with longer text\n"
        user_text = "preference"
        (tmp_path / "MEMORY.md").write_text(memory_text)
        (tmp_path / "USER.md").write_text(user_text)
        cfg = _StubMemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
        )
        _patch_loader(monkeypatch, cfg)
        entries = _collect_entries()

        memory_entry = next(e for e in entries if e["target"] == "MEMORY.md")
        user_entry = next(e for e in entries if e["target"] == "USER.md")
        assert memory_entry["content_size"] == len(memory_text)
        assert user_entry["content_size"] == len(user_text)
        assert memory_entry["paragraph_count"] >= 1
        assert 0.0 < memory_entry["pct"] < 1.0

    def test_unreadable_file_omits_entry_not_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        memory_path = tmp_path / "MEMORY.md"
        user_path = tmp_path / "USER.md"
        memory_path.write_text("ok content")
        user_path.write_text("user content")
        cfg = _StubMemoryConfig(
            declarative_path=memory_path,
            user_path=user_path,
        )
        _patch_loader(monkeypatch, cfg)

        original_read = Path.read_text

        def patched_read(self: Path, *a: Any, **kw: Any) -> str:
            if self == user_path:
                raise PermissionError("denied")
            return original_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", patched_read)

        entries = _collect_entries()
        targets = {e["target"] for e in entries}
        assert "MEMORY.md" in targets
        assert "USER.md" not in targets

    def test_load_memory_config_real_path_handles_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If load_config raises, _load_memory_config returns None (logged)."""

        def boom(*_a, **_kw):
            raise RuntimeError("config explosion")

        monkeypatch.setattr(
            "opencomputer.agent.config_store.load_config", boom
        )
        result = _load_memory_config()
        assert result is None


# ─── Route integration tests ────────────────────────────────────────


class TestMemoryStatusRoute:
    """The HTTP surface returns the expected envelope + payload shape."""

    def test_endpoint_returns_200_with_entries_array(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "MEMORY.md").write_text("a rule")
        (tmp_path / "USER.md").write_text("a preference")
        cfg = _StubMemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
        )
        _patch_loader(monkeypatch, cfg)

        client = TestClient(_make_app())
        resp = client.get("/api/v1/memory/status")

        assert resp.status_code == 200
        body = resp.json()
        assert "entries" in body
        assert isinstance(body["entries"], list)
        assert len(body["entries"]) == 2
        for entry in body["entries"]:
            for field_name in (
                "target", "content_size", "cap_limit", "pct", "paragraph_count"
            ):
                assert field_name in entry, f"missing field: {field_name}"

    def test_endpoint_validates_against_wire_schema(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The REST shape must match the wire RPC's typed schema 1:1 so a
        single TS / Python type can be reused across surfaces."""
        from opencomputer.gateway.protocol_v2 import MemoryStatusResult

        (tmp_path / "MEMORY.md").write_text("rule")
        (tmp_path / "USER.md").write_text("pref")
        cfg = _StubMemoryConfig(
            declarative_path=tmp_path / "MEMORY.md",
            user_path=tmp_path / "USER.md",
        )
        _patch_loader(monkeypatch, cfg)

        client = TestClient(_make_app())
        body = client.get("/api/v1/memory/status").json()
        # Pydantic model_validate raises on shape mismatch — clean parse
        # confirms wire/REST agreement.
        result = MemoryStatusResult.model_validate(body)
        assert len(result.entries) == 2

    def test_endpoint_returns_empty_on_no_memory_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_loader(monkeypatch, None)
        client = TestClient(_make_app())
        resp = client.get("/api/v1/memory/status")
        assert resp.status_code == 200
        assert resp.json() == {"entries": []}

    def test_endpoint_route_registered_in_all_routers(self) -> None:
        """The new route must be in ALL_ROUTERS so the dashboard server
        actually mounts it. Catches the "added the file but forgot the
        registration" regression."""
        from opencomputer.dashboard.routes import ALL_ROUTERS, memory

        assert memory.router in ALL_ROUTERS
