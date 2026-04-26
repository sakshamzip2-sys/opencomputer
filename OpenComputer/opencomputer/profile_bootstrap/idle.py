"""Idle detection for Layer 3 deepening — psutil-based.

The deepening loop only runs when the user is idle so the laptop stays
responsive. Two checks:

1. CPU usage averaged over a short window < threshold (default 20%).
2. Power source is AC (not running on battery), unless there's no
   battery sensor (desktops without batteries always count as plugged).

If psutil is missing, idle detection returns ``IdleStatus(idle=False)``
so the loop never runs without explicit consent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class IdleStatus:
    """Output of :func:`check_idle`."""

    idle: bool
    cpu_percent: float = 0.0
    on_battery: bool = False
    reason: str = ""


def _import_psutil() -> Any:
    import psutil  # type: ignore[import-not-found]
    return psutil


def is_idle_detection_available() -> bool:
    try:
        _import_psutil()
        return True
    except ImportError:
        return False


def check_idle(
    *,
    cpu_threshold: float = 20.0,
    sample_seconds: float = 1.0,
) -> IdleStatus:
    """Return whether the system is idle right now."""
    try:
        psutil = _import_psutil()
    except ImportError:
        return IdleStatus(
            idle=False, reason="psutil not installed (install opencomputer[deepening])",
        )

    cpu = float(psutil.cpu_percent(interval=sample_seconds))
    battery = psutil.sensors_battery()
    on_battery = battery is not None and not battery.power_plugged

    if cpu >= cpu_threshold:
        return IdleStatus(
            idle=False, cpu_percent=cpu, on_battery=on_battery,
            reason=f"CPU at {cpu:.1f}% (threshold {cpu_threshold:.1f}%)",
        )
    if on_battery:
        return IdleStatus(
            idle=False, cpu_percent=cpu, on_battery=True,
            reason="On battery power",
        )
    return IdleStatus(
        idle=True, cpu_percent=cpu, on_battery=on_battery,
        reason="idle",
    )
