"""Gap B — per-server stderr capture (mcp-openclaw-port follow-up).

Default OC behaviour inherited stderr from the parent — chatty MCP
servers spammed the user's terminal. This module-level test set
verifies:

* ``mcp_stderr_log_path(server_name)`` resolves the per-server log
  location under ``<profile_home>/logs/mcp/<server>.log``.
* ``open_mcp_stderr_log(server_name)`` creates the directory + opens
  the file in append mode + returns a handle.
* Filenames are sanitized so server names with weird characters can't
  escape the directory.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.mcp.stderr_capture import (
    SAFE_SERVER_NAME_RE,
    mcp_stderr_log_path,
    open_mcp_stderr_log,
    sanitize_server_name_for_path,
)


@pytest.fixture
def isolated_home(tmp_path: Path) -> Generator[Path, None, None]:
    """Patch _home() so tests don't write to the real profile."""
    with patch("opencomputer.mcp.stderr_capture._home", return_value=tmp_path):
        yield tmp_path


# ─── name sanitization ────────────────────────────────────────────


def test_safe_name_passthrough_for_simple_names() -> None:
    assert sanitize_server_name_for_path("memory") == "memory"
    assert sanitize_server_name_for_path("plug-a__github") == "plug-a__github"


def test_safe_name_replaces_unsafe_chars() -> None:
    assert sanitize_server_name_for_path("a/b") == "a_b"
    assert sanitize_server_name_for_path("a..b") == "a__b"
    assert sanitize_server_name_for_path("a\\b") == "a_b"


def test_safe_name_handles_empty() -> None:
    assert sanitize_server_name_for_path("") == "_unknown"


def test_safe_name_caps_length() -> None:
    long = "x" * 300
    out = sanitize_server_name_for_path(long)
    assert len(out) <= 128


def test_safe_name_regex_matches_sanitized() -> None:
    for raw in ("memory", "plug-a__github", "a/b", "a..b"):
        assert SAFE_SERVER_NAME_RE.fullmatch(sanitize_server_name_for_path(raw))


# ─── log path resolution ─────────────────────────────────────────


def test_log_path_under_logs_mcp(isolated_home: Path) -> None:
    p = mcp_stderr_log_path("memory")
    assert p == isolated_home / "logs" / "mcp" / "memory.log"


def test_log_path_sanitizes_unsafe_name(isolated_home: Path) -> None:
    p = mcp_stderr_log_path("evil/server")
    assert p == isolated_home / "logs" / "mcp" / "evil_server.log"
    # Resolved path must stay inside logs/mcp/
    assert (isolated_home / "logs" / "mcp") in p.parents


def test_log_path_creates_directory_on_open(isolated_home: Path) -> None:
    logs_dir = isolated_home / "logs" / "mcp"
    assert not logs_dir.exists()
    handle = open_mcp_stderr_log("memory")
    try:
        assert logs_dir.exists()
        assert (logs_dir / "memory.log").exists()
    finally:
        handle.close()


def test_log_handle_appends_writes(isolated_home: Path) -> None:
    handle = open_mcp_stderr_log("memory")
    try:
        handle.write("first line\n")
        handle.flush()
    finally:
        handle.close()
    # Reopen — append mode should preserve prior content
    handle2 = open_mcp_stderr_log("memory")
    try:
        handle2.write("second line\n")
        handle2.flush()
    finally:
        handle2.close()
    text = (isolated_home / "logs" / "mcp" / "memory.log").read_text()
    assert "first line" in text
    assert "second line" in text


def test_log_handle_writable_in_text_mode(isolated_home: Path) -> None:
    """File handle must be a text-mode write target so the SDK's
    subprocess stderr (writes Python str → fd) doesn't TypeError."""
    handle = open_mcp_stderr_log("memory")
    try:
        assert handle.writable()
        handle.write("test\n")
    finally:
        handle.close()
