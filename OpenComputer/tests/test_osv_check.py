"""
Tests for opencomputer.mcp.osv_check (Round 2B P-7).

Network-free: every OSV.dev call is mocked via ``monkeypatch`` of
``httpx.post``. The cache layer writes to a tmp ``HOME`` so test
isolation is clean per-test (the home fixture rebuilds it).

Scenarios covered:

(a) clean package        — no vulns → empty result, fail-open allows
(b) vuln pkg fail-open   — high-sev hit logs warning, allows launch
(c) vuln pkg fail-closed — high-sev hit refuses launch with error
(d) network error        — fail-open returns empty + warns
(e) cache hit within TTL — second call skips the network entirely
(f) cache hit beyond TTL — second call refreshes the entry

Plus integration scenarios with MCPConnection to confirm the
pre-flight wiring fires + bus events are emitted.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from opencomputer.agent.config import MCPServerConfig
from opencomputer.ingestion.bus import reset_default_bus
from opencomputer.mcp import client as mcp_client
from opencomputer.mcp import osv_check
from opencomputer.mcp.client import MCPConnection, _extract_package
from opencomputer.mcp.osv_check import check_package, has_high_severity

# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test gets a fresh ~ so the cache file starts empty."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path / ".opencomputer"))
    return tmp_path


@pytest.fixture(autouse=True)
def fresh_bus() -> Any:
    """Reset the singleton bus around each test so events don't leak.

    Captures the pre-test singleton + restores it on teardown so other
    test modules that import ``default_bus`` at module load time (e.g.
    ``test_typed_event_bus.test_default_bus_is_singleton``) keep the
    same identity they expect.
    """
    from opencomputer.ingestion import bus as bus_module

    original = bus_module.default_bus
    reset_default_bus()
    yield
    bus_module.default_bus = original  # type: ignore[assignment]


def _fake_post(payload: dict[str, Any], *, status: int = 200) -> Any:
    """Build a ``httpx.post`` substitute returning ``payload`` once."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = status
    response.json.return_value = payload
    if status >= 400:
        def _raise() -> None:
            raise httpx.HTTPStatusError(
                "boom", request=MagicMock(), response=response
            )
        response.raise_for_status = _raise
    else:
        response.raise_for_status = MagicMock()

    def _post(*args: Any, **kwargs: Any) -> Any:
        return response

    return _post


# ─── (a) clean package ────────────────────────────────────────────────


class TestCleanPackage:
    def test_clean_returns_empty_vulns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(osv_check.httpx, "post", _fake_post({"vulns": []}))
        result = check_package("safe-pkg", "npm")
        assert result["vulns"] == []
        assert result["cached"] is False

    def test_clean_pre_flight_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(osv_check.httpx, "post", _fake_post({"vulns": []}))
        cfg = MCPServerConfig(
            name="x",
            transport="stdio",
            command="npx",
            args=("-y", "safe-pkg"),
        )
        conn = MCPConnection(config=cfg)
        assert conn._osv_pre_flight(fail_closed=True) is None
        assert conn._osv_pre_flight(fail_closed=False) is None


# ─── (b) vuln package, fail-open ──────────────────────────────────────


def _high_sev_vuln_payload() -> dict[str, Any]:
    return {
        "vulns": [
            {
                "id": "GHSA-test-high",
                "database_specific": {"severity": "HIGH"},
            }
        ]
    }


class TestVulnFailOpen:
    def test_fail_open_pre_flight_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setattr(
            osv_check.httpx, "post", _fake_post(_high_sev_vuln_payload())
        )
        cfg = MCPServerConfig(
            name="bad",
            transport="stdio",
            command="npx",
            args=("-y", "bad-pkg"),
        )
        conn = MCPConnection(config=cfg)
        with caplog.at_level("WARNING", logger="opencomputer.mcp.client"):
            blocked = conn._osv_pre_flight(fail_closed=False)
        assert blocked is None
        # Log fingerprint mentions HIGH severity
        assert any("HIGH severity" in rec.message for rec in caplog.records)

    def test_fail_open_emits_bus_event(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            osv_check.httpx, "post", _fake_post(_high_sev_vuln_payload())
        )
        from opencomputer.ingestion.bus import default_bus

        captured: list[Any] = []
        sub = default_bus.subscribe("mcp_security.osv_hit", captured.append)
        try:
            cfg = MCPServerConfig(
                name="bad-srv",
                transport="stdio",
                command="npx",
                args=("-y", "bad-pkg"),
            )
            MCPConnection(config=cfg)._osv_pre_flight(fail_closed=False)
            assert len(captured) == 1
            ev = captured[0]
            assert ev.event_type == "mcp_security.osv_hit"
            assert ev.package == "bad-pkg"
            assert ev.ecosystem == "npm"
            assert ev.server_name == "bad-srv"
            assert ev.high_severity is True
            assert ev.blocked is False
            assert "GHSA-test-high" in ev.vuln_ids
        finally:
            sub.unsubscribe()


# ─── (c) vuln package, fail-closed ────────────────────────────────────


class TestVulnFailClosed:
    def test_fail_closed_returns_error_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            osv_check.httpx, "post", _fake_post(_high_sev_vuln_payload())
        )
        cfg = MCPServerConfig(
            name="bad",
            transport="stdio",
            command="npx",
            args=("-y", "bad-pkg"),
        )
        conn = MCPConnection(config=cfg)
        blocked = conn._osv_pre_flight(fail_closed=True)
        assert blocked is not None
        assert "bad-pkg" in blocked
        assert "GHSA-test-high" in blocked

    def test_fail_closed_event_marks_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            osv_check.httpx, "post", _fake_post(_high_sev_vuln_payload())
        )
        from opencomputer.ingestion.bus import default_bus

        captured: list[Any] = []
        sub = default_bus.subscribe("mcp_security.osv_hit", captured.append)
        try:
            cfg = MCPServerConfig(
                name="bad",
                transport="stdio",
                command="npx",
                args=("-y", "bad-pkg"),
            )
            MCPConnection(config=cfg)._osv_pre_flight(fail_closed=True)
            assert len(captured) == 1
            assert captured[0].blocked is True
        finally:
            sub.unsubscribe()

    @pytest.mark.asyncio
    async def test_fail_closed_connect_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            osv_check.httpx, "post", _fake_post(_high_sev_vuln_payload())
        )
        cfg = MCPServerConfig(
            name="bad",
            transport="stdio",
            command="npx",
            args=("-y", "bad-pkg"),
        )
        conn = MCPConnection(config=cfg)
        ok = await conn.connect(osv_check_enabled=True, osv_check_fail_closed=True)
        assert ok is False
        assert conn.state == "error"
        assert conn.last_error is not None
        assert "OSV blocked launch" in conn.last_error


# ─── (d) network error ────────────────────────────────────────────────


class TestNetworkError:
    def test_network_error_is_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise httpx.ConnectError("network unreachable")

        monkeypatch.setattr(osv_check.httpx, "post", _boom)
        result = check_package("any-pkg", "npm")
        assert result["vulns"] == []
        assert result["cached"] is False

    def test_network_error_pre_flight_allows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise httpx.TimeoutException("slow")

        monkeypatch.setattr(osv_check.httpx, "post", _boom)
        cfg = MCPServerConfig(
            name="x",
            transport="stdio",
            command="uvx",
            args=("some-pkg",),
        )
        conn = MCPConnection(config=cfg)
        # Even fail-closed allows when the lookup itself fails — we only
        # block on positive HIGH advisories, not on enrichment outages.
        assert conn._osv_pre_flight(fail_closed=True) is None

    def test_http_error_status_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(osv_check.httpx, "post", _fake_post({}, status=503))
        result = check_package("any-pkg", "npm")
        assert result["vulns"] == []


# ─── (e) cache hit within TTL ─────────────────────────────────────────


class TestCacheHitFresh:
    def test_second_call_skips_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = {"n": 0}

        def _post(*args: Any, **kwargs: Any) -> Any:
            call_count["n"] += 1
            response = MagicMock(spec=httpx.Response)
            response.json.return_value = {"vulns": []}
            response.raise_for_status = MagicMock()
            return response

        monkeypatch.setattr(osv_check.httpx, "post", _post)
        first = check_package("cached-pkg", "npm")
        assert first["cached"] is False
        assert call_count["n"] == 1
        second = check_package("cached-pkg", "npm")
        assert second["cached"] is True
        assert call_count["n"] == 1  # no second hit

    def test_cache_persisted_to_disk(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(osv_check.httpx, "post", _fake_post({"vulns": []}))
        check_package("disk-pkg", "PyPI")
        cache_file = osv_check._cache_path()
        assert cache_file.exists()
        body = json.loads(cache_file.read_text())
        assert "PyPI:disk-pkg" in body

    def test_cache_dir_mode_700(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(osv_check.httpx, "post", _fake_post({"vulns": []}))
        check_package("modepkg", "npm")
        parent = osv_check._cache_path().parent
        # On filesystems without POSIX modes the chmod call is best-effort,
        # so we accept anything <= 0o700 (== 0o700 in the typical case).
        mode = parent.stat().st_mode & 0o777
        assert mode <= 0o700


# ─── (f) cache hit beyond TTL ─────────────────────────────────────────


class TestCacheStale:
    def test_stale_entry_refreshes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Seed the cache with an entry older than the TTL.
        cache_file = osv_check._cache_path()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        old = time.time() - (osv_check._CACHE_TTL_S + 60)
        cache_file.write_text(
            json.dumps(
                {
                    "npm:stale-pkg": {
                        "vulns": [{"id": "OLD-VULN"}],
                        "cached_at": old,
                    }
                }
            )
        )
        monkeypatch.setattr(osv_check.httpx, "post", _fake_post({"vulns": []}))
        result = check_package("stale-pkg", "npm")
        # Refreshed entry overrides the old one.
        assert result["cached"] is False
        assert result["vulns"] == []
        # Persisted refresh wins on disk too.
        body = json.loads(cache_file.read_text())
        assert body["npm:stale-pkg"]["vulns"] == []


# ─── package extraction helper ────────────────────────────────────────


class TestExtractPackage:
    def test_npx_with_y_flag(self) -> None:
        cfg = MCPServerConfig(
            name="x",
            transport="stdio",
            command="npx",
            args=("-y", "@scope/pkg", "more-args"),
        )
        assert _extract_package(cfg) == ("@scope/pkg", "npm")

    def test_npx_without_flag(self) -> None:
        cfg = MCPServerConfig(
            name="x",
            transport="stdio",
            command="npx",
            args=("simple-pkg",),
        )
        assert _extract_package(cfg) == ("simple-pkg", "npm")

    def test_uvx(self) -> None:
        cfg = MCPServerConfig(
            name="x",
            transport="stdio",
            command="uvx",
            args=("mcp-server-fetch",),
        )
        assert _extract_package(cfg) == ("mcp-server-fetch", "PyPI")

    def test_other_commands_skip(self) -> None:
        cfg = MCPServerConfig(
            name="x",
            transport="stdio",
            command="python3",
            args=("my-server.py",),
        )
        assert _extract_package(cfg) is None

    def test_http_transport_skip(self) -> None:
        cfg = MCPServerConfig(
            name="x",
            transport="http",
            url="https://example.com",
        )
        assert _extract_package(cfg) is None


# ─── has_high_severity helper ─────────────────────────────────────────


class TestHasHighSeverity:
    def test_top_level_high(self) -> None:
        assert has_high_severity([{"database_specific": {"severity": "HIGH"}}])

    def test_top_level_critical(self) -> None:
        assert has_high_severity([{"database_specific": {"severity": "critical"}}])

    def test_affected_high(self) -> None:
        vulns = [
            {
                "affected": [
                    {"database_specific": {"severity": "HIGH"}},
                ]
            }
        ]
        assert has_high_severity(vulns)

    def test_low_returns_false(self) -> None:
        assert not has_high_severity([{"database_specific": {"severity": "low"}}])

    def test_empty_returns_false(self) -> None:
        assert not has_high_severity([])

    def test_malformed_entries_skip(self) -> None:
        # Mix of garbage + a high-sev hit — should still detect.
        vulns: list[Any] = [None, "string", {"database_specific": {"severity": "HIGH"}}]
        assert has_high_severity(vulns)


# ─── disabled flag short-circuits everything ──────────────────────────


class TestDisabledFlag:
    @pytest.mark.asyncio
    async def test_disabled_skips_pre_flight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When osv_check_enabled=False, connect() should NOT invoke
        ``_osv_pre_flight`` even on stdio launches with vuln packages.
        """
        called = {"pre_flight": False}

        def _track(self: MCPConnection, *, fail_closed: bool) -> str | None:
            called["pre_flight"] = True
            return None

        monkeypatch.setattr(MCPConnection, "_osv_pre_flight", _track)

        # Patch stdio_client to a sentinel-raising callable so connect()
        # short-circuits AFTER the OSV gate without attempting to spawn.
        sentinel = RuntimeError("sentinel — spawn would have happened here")

        def _stdio_sentinel(*args: Any, **kwargs: Any) -> Any:
            raise sentinel

        monkeypatch.setattr(mcp_client, "stdio_client", _stdio_sentinel)

        cfg = MCPServerConfig(
            name="x",
            transport="stdio",
            command="npx",
            args=("-y", "any-pkg"),
        )
        conn = MCPConnection(config=cfg)
        ok = await conn.connect(osv_check_enabled=False, osv_check_fail_closed=True)
        assert ok is False
        assert called["pre_flight"] is False
        # Hit the sentinel — confirms the spawn path was reached.
        assert "sentinel" in (conn.last_error or "")

    @pytest.mark.asyncio
    async def test_enabled_invokes_pre_flight(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """And the inverse — enabled flag DOES invoke the pre-flight gate."""
        called = {"pre_flight": False}

        def _track(self: MCPConnection, *, fail_closed: bool) -> str | None:
            called["pre_flight"] = True
            return None

        monkeypatch.setattr(MCPConnection, "_osv_pre_flight", _track)

        sentinel = RuntimeError("sentinel")

        def _stdio_sentinel(*args: Any, **kwargs: Any) -> Any:
            raise sentinel

        monkeypatch.setattr(mcp_client, "stdio_client", _stdio_sentinel)

        cfg = MCPServerConfig(
            name="x",
            transport="stdio",
            command="npx",
            args=("-y", "any-pkg"),
        )
        conn = MCPConnection(config=cfg)
        await conn.connect(osv_check_enabled=True, osv_check_fail_closed=False)
        assert called["pre_flight"] is True
