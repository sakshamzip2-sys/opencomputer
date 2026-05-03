"""Cross-platform service backend Protocol + result dataclasses.

This is the contract every per-platform backend module must satisfy.
The factory in ``service/factory.py`` returns a module conforming to
this Protocol (modules-as-objects polymorphism; no inheritance).
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Protocol


class ServiceUnsupportedError(RuntimeError):
    """Raised when the current platform has no service backend."""


@dataclass(frozen=True)
class InstallResult:
    backend: str
    config_path: Path
    enabled: bool
    started: bool
    notes: list[str]


@dataclass(frozen=True)
class StatusResult:
    backend: str
    file_present: bool
    enabled: bool
    running: bool
    pid: int | None
    uptime_seconds: float | None
    last_log_lines: list[str]


@dataclass(frozen=True)
class UninstallResult:
    backend: str
    file_removed: bool
    config_path: Path | None
    notes: list[str]


class ServiceBackend(Protocol):
    """Module-level Protocol every backend conforms to."""

    NAME: ClassVar[str]

    def supported(self) -> bool: ...

    def install(
        self,
        *,
        profile: str,
        extra_args: str,
        restart: bool = True,
    ) -> InstallResult: ...

    def uninstall(self) -> UninstallResult: ...

    def status(self) -> StatusResult: ...

    def start(self) -> bool: ...

    def stop(self) -> bool: ...

    def follow_logs(
        self, *, lines: int = 100, follow: bool = False,
    ) -> Iterator[str]: ...


__all__ = [
    "InstallResult",
    "ServiceBackend",
    "ServiceUnsupportedError",
    "StatusResult",
    "UninstallResult",
]
