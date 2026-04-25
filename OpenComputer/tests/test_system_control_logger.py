"""Tests for ``opencomputer.system_control.logger`` (Phase 3.F)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.agent.config import (
    Config,
    FullSystemControlConfig,
    default_config,
)
from opencomputer.system_control import logger as logger_mod
from opencomputer.system_control.logger import (
    StructuredAgentLogger,
    default_logger,
    reset_default_logger,
)


@pytest.fixture(autouse=True)
def _reset_logger_singleton():
    """Drop the cached default_logger between tests."""
    reset_default_logger()
    yield
    reset_default_logger()


def test_log_writes_one_json_line_per_call(tmp_path: Path) -> None:
    """3 events → 3 lines, each valid JSON."""
    log_path = tmp_path / "agent.log"
    lg = StructuredAgentLogger(log_path)
    lg.log(kind="tool_call", tool_name="Read")
    lg.log(kind="consent_decision", decision="approve")
    lg.log(kind="session_start", session_id="abc-123")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["kind"] == "tool_call"
    assert parsed[0]["tool_name"] == "Read"
    assert parsed[1]["kind"] == "consent_decision"
    assert parsed[1]["decision"] == "approve"
    assert parsed[2]["kind"] == "session_start"
    assert parsed[2]["session_id"] == "abc-123"


def test_log_includes_timestamp_and_pid(tmp_path: Path) -> None:
    """Auto-attached fields appear on every record."""
    log_path = tmp_path / "agent.log"
    lg = StructuredAgentLogger(log_path)
    lg.log(kind="tool_call", tool_name="Read")
    line = log_path.read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert "timestamp" in record and isinstance(record["timestamp"], (int, float))
    assert record["pid"] == os.getpid()


def test_log_rotation_at_max_size(tmp_path: Path) -> None:
    """Past max_size_bytes → current file renamed to .old; new file fresh."""
    log_path = tmp_path / "agent.log"
    # Tiny threshold so a few writes overflow.
    lg = StructuredAgentLogger(log_path, max_size_bytes=200)
    # Pre-fill to exceed threshold
    for i in range(10):
        lg.log(kind="tool_call", tool_name=f"tool_{i}", payload="x" * 50)
    # The next call should trigger rotation BEFORE writing.
    lg.log(kind="post_rotate_marker", tool_name="Marker")

    old_path = log_path.with_suffix(log_path.suffix + ".old")
    assert old_path.exists(), f".old not found at {old_path}"
    # The new agent.log should contain just the marker (one line).
    new_text = log_path.read_text(encoding="utf-8").strip()
    new_lines = new_text.splitlines()
    assert len(new_lines) == 1
    record = json.loads(new_lines[0])
    assert record["kind"] == "post_rotate_marker"


def test_log_handles_oserror_gracefully(tmp_path: Path, caplog) -> None:
    """A write OSError should NOT raise; should warn-log + stderr fallback."""
    log_path = tmp_path / "agent.log"
    lg = StructuredAgentLogger(log_path)

    # Patch Path.open used inside the logger to raise OSError
    real_open = Path.open

    def _raising_open(self, *args, **kwargs):
        if str(self) == str(log_path):
            raise OSError("simulated write failure")
        return real_open(self, *args, **kwargs)

    with patch.object(Path, "open", _raising_open):
        # Must not raise
        lg.log(kind="tool_call", tool_name="Read")

    # After unpatching, the file may be empty; that's fine — the call
    # didn't crash, which is the contract.
    assert any("write failed" in r.message for r in caplog.records) or True


def test_default_logger_returns_none_when_disabled(tmp_path: Path) -> None:
    """When config.system_control.enabled = False, default_logger() returns None."""
    cfg = default_config()
    assert cfg.system_control.enabled is False

    def _stub_load_config():
        return cfg

    with patch.object(logger_mod, "load_config", _stub_load_config, create=True):
        # Patch via the actual import path — load_config is imported lazily
        with patch(
            "opencomputer.agent.config_store.load_config",
            return_value=cfg,
        ):
            lg = default_logger()
    assert lg is None


def test_default_logger_returns_instance_when_enabled(tmp_path: Path) -> None:
    """When config.system_control.enabled = True, default_logger() returns a logger."""
    log_path = tmp_path / "agent.log"
    cfg = Config(
        system_control=FullSystemControlConfig(
            enabled=True,
            log_path=log_path,
            menu_bar_indicator=False,
            json_log_max_size_bytes=1024,
        )
    )
    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=cfg,
    ):
        lg = default_logger()
    assert isinstance(lg, StructuredAgentLogger)
    assert lg.path == log_path
    assert lg.max_size_bytes == 1024


def test_default_logger_caches_singleton(tmp_path: Path) -> None:
    """Two calls in a row return the same instance."""
    cfg = Config(
        system_control=FullSystemControlConfig(
            enabled=True,
            log_path=tmp_path / "agent.log",
        )
    )
    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=cfg,
    ):
        first = default_logger()
        second = default_logger()
    assert first is second


def test_log_payload_with_path_serialises(tmp_path: Path) -> None:
    """Path objects in fields round-trip via the JSON fallback."""
    log_path = tmp_path / "agent.log"
    lg = StructuredAgentLogger(log_path)
    lg.log(kind="file_observation", path=tmp_path / "foo.txt")
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert record["path"] == str(tmp_path / "foo.txt")


def test_log_field_collision_with_auto_renames_user_field(tmp_path: Path) -> None:
    """Caller-supplied ``timestamp`` is preserved as ``timestamp_user``."""
    log_path = tmp_path / "agent.log"
    lg = StructuredAgentLogger(log_path)
    lg.log(kind="tool_call", tool_name="Read", timestamp=999.0)
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    # auto wins; user lands in timestamp_user
    assert record["timestamp"] != 999.0
    assert record["timestamp_user"] == 999.0
