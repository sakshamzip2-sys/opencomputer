"""Tests for ScreenAwarenessSensor.capture_now() — orchestrator.

Mocks mss + OCR + foreground-app + lock-detect so tests run on any host
without a real display server.
"""
from __future__ import annotations

from unittest import mock

from extensions.screen_awareness.ring_buffer import ScreenRingBuffer
from extensions.screen_awareness.sensor import ScreenAwarenessSensor


def _mk_sensor(buf=None):
    return ScreenAwarenessSensor(
        ring_buffer=buf or ScreenRingBuffer(max_size=10),
        cooldown_seconds=0.0,  # disable cooldown for most tests
    )


def test_happy_path_captures_and_appends():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_ocr_screen", return_value="hello world"), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="iTerm2"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        result = sensor.capture_now(session_id="s1", trigger="user_message")
    assert result is not None
    assert result.text == "hello world"
    assert result.trigger == "user_message"
    assert len(sensor._ring) == 1


def test_lock_skip_returns_none_no_append():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_is_locked", return_value=True):
        result = sensor.capture_now(session_id="s1", trigger="user_message")
    assert result is None
    assert len(sensor._ring) == 0


def test_sensitive_app_skip_returns_none():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="1Password 7"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=True):
        result = sensor.capture_now(session_id="s1", trigger="user_message")
    assert result is None
    assert len(sensor._ring) == 0


def test_dedup_same_text_appends_once():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_ocr_screen", return_value="same"), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="x"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        sensor.capture_now(session_id="s1", trigger="user_message")
        result2 = sensor.capture_now(session_id="s1", trigger="user_message")
    assert result2 is not None
    assert len(sensor._ring) == 1


def test_cooldown_blocks_rapid_second_capture():
    sensor = ScreenAwarenessSensor(
        ring_buffer=ScreenRingBuffer(max_size=10),
        cooldown_seconds=10.0,
    )
    with mock.patch.object(sensor, "_ocr_screen", return_value="t"), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="x"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        first = sensor.capture_now(session_id="s1", trigger="user_message")
        second = sensor.capture_now(session_id="s1", trigger="pre_tool_use")
    assert first is not None
    assert second is not None  # cooldown reuses the latest
    assert len(sensor._ring) == 1


def test_ocr_failure_returns_none_no_crash():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_ocr_screen", side_effect=RuntimeError("ocr boom")), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="x"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        result = sensor.capture_now(session_id="s1", trigger="user_message")
    assert result is None
    assert len(sensor._ring) == 0


def test_capture_records_tool_call_id():
    sensor = _mk_sensor()
    with mock.patch.object(sensor, "_ocr_screen", return_value="x"), \
         mock.patch.object(sensor, "_is_locked", return_value=False), \
         mock.patch.object(sensor, "_foreground_app_name", return_value="x"), \
         mock.patch.object(sensor, "_is_sensitive", return_value=False):
        result = sensor.capture_now(
            session_id="s1",
            trigger="pre_tool_use",
            tool_call_id="toolu_abc123",
        )
    assert result is not None
    assert result.tool_call_id == "toolu_abc123"
