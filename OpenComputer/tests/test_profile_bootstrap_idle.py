"""Idle detection tests — psutil-based CPU + power source check."""
from unittest.mock import MagicMock, patch

from opencomputer.profile_bootstrap.idle import (
    IdleStatus,
    check_idle,
    is_idle_detection_available,
)


def test_is_idle_detection_available_false_without_psutil():
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        side_effect=ImportError(),
    ):
        assert is_idle_detection_available() is False


def test_check_idle_treats_unavailable_as_not_idle():
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        side_effect=ImportError(),
    ):
        status = check_idle()
    assert status.idle is False
    assert "psutil" in status.reason


def test_check_idle_returns_idle_when_cpu_low_and_plugged_in():
    fake = MagicMock()
    fake.cpu_percent.return_value = 5.0
    fake_battery = MagicMock()
    fake_battery.power_plugged = True
    fake.sensors_battery.return_value = fake_battery
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        return_value=fake,
    ):
        status = check_idle(cpu_threshold=20.0, sample_seconds=0.0)
    assert status.idle is True


def test_check_idle_not_idle_when_cpu_high():
    fake = MagicMock()
    fake.cpu_percent.return_value = 75.0
    fake_battery = MagicMock()
    fake_battery.power_plugged = True
    fake.sensors_battery.return_value = fake_battery
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        return_value=fake,
    ):
        status = check_idle(cpu_threshold=20.0, sample_seconds=0.0)
    assert status.idle is False
    assert "CPU" in status.reason


def test_check_idle_not_idle_when_on_battery():
    fake = MagicMock()
    fake.cpu_percent.return_value = 5.0
    fake_battery = MagicMock()
    fake_battery.power_plugged = False
    fake.sensors_battery.return_value = fake_battery
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        return_value=fake,
    ):
        status = check_idle(cpu_threshold=20.0, sample_seconds=0.0)
    assert status.idle is False
    assert "battery" in status.reason.lower()


def test_check_idle_treats_no_battery_as_plugged_in():
    """Desktops have no battery — always plugged in."""
    fake = MagicMock()
    fake.cpu_percent.return_value = 5.0
    fake.sensors_battery.return_value = None  # no battery
    with patch(
        "opencomputer.profile_bootstrap.idle._import_psutil",
        return_value=fake,
    ):
        status = check_idle(cpu_threshold=20.0, sample_seconds=0.0)
    assert status.idle is True
