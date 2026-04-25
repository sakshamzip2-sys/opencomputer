"""Tests for ``opencomputer.system_control.bus_listener`` (Phase 3.F)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from opencomputer.agent.config import Config, FullSystemControlConfig, default_config
from opencomputer.ingestion.bus import reset_default_bus
from opencomputer.system_control import bus_listener as bus_listener_mod
from opencomputer.system_control.bus_listener import (
    active_subscription,
    attach_to_bus,
    detach_from_bus,
)
from opencomputer.system_control.logger import reset_default_logger
from plugin_sdk.ingestion import HookSignalEvent, ToolCallEvent


@pytest.fixture(autouse=True)
def _reset_state():
    """Drop logger singleton + active subscription between tests.

    Bus singleton is swapped+restored so the cross-file invariant
    ``get_default_bus() is default_bus`` (test_typed_event_bus.py) survives
    test ordering. Restoring the original instance — rather than calling
    reset_default_bus() at teardown — keeps the original module-level
    binding intact.
    """
    from opencomputer.ingestion import bus as bus_module

    detach_from_bus()
    reset_default_logger()
    saved_bus = bus_module.default_bus
    reset_default_bus()
    yield
    detach_from_bus()
    reset_default_logger()
    bus_module.default_bus = saved_bus


def test_attach_to_bus_when_disabled_returns_none() -> None:
    """config.enabled=False → no subscription created."""
    cfg = default_config()
    assert cfg.system_control.enabled is False
    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=cfg,
    ):
        sub = attach_to_bus()
    assert sub is None
    assert active_subscription() is None


def test_attach_to_bus_subscribes_to_all_events(tmp_path: Path) -> None:
    """ToolCallEvent + HookSignalEvent both end up in agent.log when enabled."""
    log_path = tmp_path / "agent.log"
    cfg = Config(
        system_control=FullSystemControlConfig(
            enabled=True,
            log_path=log_path,
        )
    )
    # Need to patch BOTH the config_store load (for default_logger AND
    # for attach_to_bus) — both lazy-import so the patch path is the
    # same.
    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=cfg,
    ):
        # Use the get_default_bus helper so we resolve the current bus
        # singleton, not a stale import from before reset_default_bus().
        from opencomputer.ingestion.bus import get_default_bus

        bus = get_default_bus()

        sub = attach_to_bus()
        assert sub is not None
        # Re-confirm: the new subscription is on the bus that
        # bus_listener uses.
        assert sub in [s for s in bus.subscribers(None)]

        bus.publish(
            ToolCallEvent(
                tool_name="Read",
                outcome="success",
                duration_seconds=0.1,
            )
        )
        bus.publish(
            HookSignalEvent(
                hook_name="PreToolUse",
                decision="pass",
                reason="ok",
            )
        )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    kinds = [r["kind"] for r in parsed]
    assert "tool_call" in kinds
    assert "hook" in kinds
    # Verify body fields land in the record
    tc = next(r for r in parsed if r["kind"] == "tool_call")
    assert tc["tool_name"] == "Read"
    assert tc["outcome"] == "success"


def test_attach_to_bus_idempotent(tmp_path: Path) -> None:
    """Two attach calls return the same subscription."""
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
        first = attach_to_bus()
        second = attach_to_bus()
    assert first is not None
    assert first is second


def test_listener_detaches_cleanly(tmp_path: Path) -> None:
    """After detach, new events do NOT appear in the log."""
    log_path = tmp_path / "agent.log"
    cfg = Config(
        system_control=FullSystemControlConfig(
            enabled=True,
            log_path=log_path,
        )
    )
    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=cfg,
    ):
        from opencomputer.ingestion.bus import get_default_bus

        bus = get_default_bus()
        attach_to_bus()
        bus.publish(ToolCallEvent(tool_name="Before"))

        detach_from_bus()
        assert active_subscription() is None

        bus.publish(ToolCallEvent(tool_name="After"))

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["tool_name"] == "Before"


def test_handler_skips_when_logger_returns_none(tmp_path: Path) -> None:
    """If config flips to disabled mid-flight, the handler skips quietly."""
    log_path = tmp_path / "agent.log"
    cfg_on = Config(
        system_control=FullSystemControlConfig(
            enabled=True,
            log_path=log_path,
        )
    )
    cfg_off = default_config()  # disabled

    # Attach with system-control on.
    from opencomputer.ingestion.bus import get_default_bus

    bus = get_default_bus()
    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=cfg_on,
    ):
        attach_to_bus()

    # Now flip to "disabled" config and verify the next publish doesn't
    # blow up and doesn't write.
    reset_default_logger()
    with patch(
        "opencomputer.agent.config_store.load_config",
        return_value=cfg_off,
    ):
        # The subscription is still attached, but the handler resolves
        # default_logger() lazily — and now returns None.
        bus.publish(ToolCallEvent(tool_name="Skipped"))

    # File should not have been created (or be empty).
    if log_path.exists():
        assert log_path.read_text(encoding="utf-8").strip() == ""
