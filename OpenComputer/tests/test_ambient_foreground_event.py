"""tests/test_ambient_foreground_event.py — SDK type contract for ambient sensor."""
from __future__ import annotations

import dataclasses

import pytest

from plugin_sdk.ingestion import (
    AmbientSensorPauseEvent,
    ForegroundAppEvent,
    SignalEvent,
)


def test_foreground_event_inherits_signal_event():
    e = ForegroundAppEvent(app_name="Code", window_title_hash="abc", platform="darwin")
    assert isinstance(e, SignalEvent)
    assert e.event_type == "foreground_app"


def test_foreground_event_default_fields_are_safe():
    """Privacy: defaults must NOT leak any data."""
    e = ForegroundAppEvent()
    assert e.app_name == ""
    assert e.window_title_hash == ""
    assert e.bundle_id == ""
    assert e.is_sensitive is False
    assert e.platform == ""


def test_foreground_event_is_frozen():
    e = ForegroundAppEvent(app_name="Code")
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.app_name = "Other"


def test_pause_event_inherits_signal_event():
    e = AmbientSensorPauseEvent(sensor_name="foreground", paused=True, reason="user")
    assert isinstance(e, SignalEvent)
    assert e.event_type == "ambient_sensor_pause"
    assert e.sensor_name == "foreground"
    assert e.paused is True


def test_pause_event_default_is_paused_true():
    """The "default" pause event represents an entry-into-paused state, so paused=True."""
    e = AmbientSensorPauseEvent()
    assert e.paused is True
    assert e.sensor_name == "foreground"


def test_foreground_event_filtered_shape():
    """When sensor filters a sensitive app: app_name=<filtered>, hash="", is_sensitive=True."""
    e = ForegroundAppEvent(app_name="<filtered>", window_title_hash="", is_sensitive=True, platform="darwin")
    assert e.is_sensitive is True
    assert e.window_title_hash == ""
