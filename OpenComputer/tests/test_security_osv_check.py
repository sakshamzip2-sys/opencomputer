"""TS-T3 — OSV malware check tests.

Covers the hermes-port `opencomputer.security.osv_check` module: a
stdlib-only (urllib + json + re) pre-flight scan that blocks
``npx`` / ``uvx`` package launches when the package has a confirmed
malware advisory (``MAL-*``) in OSV.dev. Network failures are
fail-open by design.

Distinct from the older `tests/test_osv_check.py` which targets the
httpx-based vuln-cache module under `opencomputer.mcp.osv_check`.
"""
from __future__ import annotations

from unittest.mock import patch

from opencomputer.security.osv_check import (
    _infer_ecosystem,
    _parse_npm_package,
    _parse_pypi_package,
    check_package_for_malware,
)


def test_infer_ecosystem_npx():
    assert _infer_ecosystem("npx") == "npm"
    assert _infer_ecosystem("/usr/bin/npx.cmd") == "npm"


def test_infer_ecosystem_uvx():
    assert _infer_ecosystem("uvx") == "PyPI"
    assert _infer_ecosystem("pipx") == "PyPI"


def test_infer_ecosystem_unknown():
    assert _infer_ecosystem("docker") is None


def test_parse_npm_scoped():
    name, version = _parse_npm_package("@scope/pkg@1.2.3")
    assert name == "@scope/pkg"
    assert version == "1.2.3"


def test_parse_npm_unscoped():
    name, version = _parse_npm_package("react@18.0.0")
    assert name == "react"
    assert version == "18.0.0"


def test_parse_pypi_with_extras():
    name, version = _parse_pypi_package("requests[socks]==2.31.0")
    assert name == "requests"
    assert version == "2.31.0"


def test_check_unknown_command_returns_none():
    """Non-npx/uvx commands skip the check."""
    assert check_package_for_malware("docker", ["run", "redis"]) is None


def test_check_clean_package_returns_none():
    """Mock OSV API: clean response → None."""
    fake_response = b'{"vulns": []}'
    with patch("opencomputer.security.osv_check.urllib.request.urlopen") as mock:
        mock.return_value.__enter__.return_value.read.return_value = fake_response
        result = check_package_for_malware("npx", ["react"])
    assert result is None


def test_check_malware_returns_block_message():
    """Mock OSV API: MAL-* advisory → blocking message."""
    fake_response = b'{"vulns": [{"id": "MAL-2024-1234", "summary": "malicious code"}]}'
    with patch("opencomputer.security.osv_check.urllib.request.urlopen") as mock:
        mock.return_value.__enter__.return_value.read.return_value = fake_response
        result = check_package_for_malware("npx", ["evil-package"])
    assert result is not None
    assert "BLOCKED" in result
    assert "MAL-2024-1234" in result


def test_check_network_failure_fails_open():
    """Network errors → None (don't block on transient failures)."""
    with patch(
        "opencomputer.security.osv_check.urllib.request.urlopen",
        side_effect=Exception("network down"),
    ):
        result = check_package_for_malware("npx", ["react"])
    assert result is None


def test_check_ignores_regular_cves():
    """Regular CVE-* IDs are NOT blocked — only MAL-*."""
    fake_response = b'{"vulns": [{"id": "CVE-2024-1234", "summary": "xss"}]}'
    with patch("opencomputer.security.osv_check.urllib.request.urlopen") as mock:
        mock.return_value.__enter__.return_value.read.return_value = fake_response
        result = check_package_for_malware("npx", ["react"])
    assert result is None
