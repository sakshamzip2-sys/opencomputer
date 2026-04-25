"""StructuredAgentLogger — append-only JSON-line log for 3.F.

Writes one JSON object per line to ``~/.opencomputer/<profile>/agent.log``
when the system-control master switch is on. Includes ``timestamp`` (epoch
float) and ``pid`` automatically. The ``kind`` field is the discriminator
(``"tool_call"``, ``"consent_decision"``, ``"session_start"``,
``"sandbox_event"``, ...).

Design stance
-------------

- **Best-effort writes**: ``OSError`` is caught + logged at WARNING and
  the call falls back to a stderr print. The logger MUST NOT crash the
  agent under any failure mode (full disk, permission flip, etc.).
- **Rotation**: when the file grows past ``max_size_bytes``, the current
  file is renamed to ``<log_path>.old`` and a fresh file starts. One
  ``.old`` rolloff only — admins use ``logrotate`` for longer history.
- **Thread-safety**: a module-level lock serialises append + rotate so
  concurrent log() calls from different threads don't interleave.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger("opencomputer.system_control.logger")


class StructuredAgentLogger:
    """One JSON-line per call, append-only, rotating, OSError-tolerant.

    Parameters
    ----------
    path:
        Absolute path to ``agent.log``. Parent directory is created if
        missing (also OSError-tolerant — a failure here means subsequent
        writes will hit the fallback).
    max_size_bytes:
        Rotation threshold. Default = 50 MB.
    """

    def __init__(self, path: Path, max_size_bytes: int = 50 * 1024 * 1024) -> None:
        self._path = Path(path)
        self._max_size_bytes = max_size_bytes
        self._lock = threading.Lock()
        # Best-effort parent dir creation. If this raises (OSError) we
        # swallow it — a subsequent log() will fall back to stderr.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _log.warning("agent.log parent dir create failed: %s", e)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def max_size_bytes(self) -> int:
        return self._max_size_bytes

    def log(self, *, kind: str, **fields: Any) -> None:
        """Write one JSON line. Auto-attaches ``timestamp`` + ``pid``.

        Never raises. If the write fails (full disk, permission flip,
        etc.), the entry is printed to stderr and a WARNING is logged.
        Caller is expected to dispatch even if the system is unhealthy.
        """
        record: dict[str, Any] = {
            "kind": kind,
            "timestamp": time.time(),
            "pid": os.getpid(),
        }
        # Caller fields override autos only if they really insist —
        # otherwise the autos win to keep records consistent.
        for key, value in fields.items():
            if key in record:
                # Don't silently drop user fields: rename to ``<key>_user``.
                record[f"{key}_user"] = value
            else:
                record[key] = value

        try:
            line = json.dumps(record, default=_json_fallback, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            # Defensive: if a payload can't be serialised, log a marker
            # entry instead so the file stays parseable.
            _log.warning("agent.log serialisation failed (%s); writing marker", e)
            line = json.dumps(
                {
                    "kind": "log_serialisation_error",
                    "timestamp": time.time(),
                    "pid": os.getpid(),
                    "original_kind": kind,
                    "error": str(e),
                }
            )

        with self._lock:
            self._maybe_rotate_locked()
            self._append_locked(line)

    # ─── private ───────────────────────────────────────────────────

    def _append_locked(self, line: str) -> None:
        """Append one line. OSError-tolerant — falls back to stderr."""
        try:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.write("\n")
        except OSError as e:
            _log.warning("agent.log write failed: %s", e)
            # stderr fallback so the entry isn't lost if someone is
            # tailing the process. Best-effort; if even stderr is
            # closed we silently drop.
            try:
                print(line, file=sys.stderr)
            except Exception:  # noqa: BLE001 — last-resort path
                pass

    def _maybe_rotate_locked(self) -> None:
        """Rotate when file is too big.

        Atomic rename to ``<log_path>.old``. Any pre-existing ``.old``
        is overwritten (one rolloff only). OSError-tolerant: if rotation
        fails we log a WARNING and continue appending — better to keep
        going than to lose new events.
        """
        try:
            size = self._path.stat().st_size
        except OSError:
            return  # file doesn't exist yet; nothing to rotate
        if size <= self._max_size_bytes:
            return
        old_path = self._path.with_suffix(self._path.suffix + ".old")
        try:
            # Path.replace is atomic across same-filesystem renames and
            # overwrites the destination if it exists.
            self._path.replace(old_path)
        except OSError as e:
            _log.warning("agent.log rotation failed: %s", e)


def _json_fallback(obj: Any) -> Any:
    """Default-encoder fallback for non-JSON-native values.

    Paths -> str; dataclasses -> dict (via ``asdict`` if importable);
    everything else -> ``repr``. Keeps the log honest without crashing
    the caller.
    """
    if isinstance(obj, Path):
        return str(obj)
    try:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(obj) and not isinstance(obj, type):
            return asdict(obj)
    except Exception:  # noqa: BLE001 — defensive
        pass
    return repr(obj)


# ---------------------------------------------------------------------------
# Module-level singleton helper
# ---------------------------------------------------------------------------


_default_logger_lock = threading.Lock()
_cached_logger: StructuredAgentLogger | None = None
_cached_log_path: Path | None = None


def default_logger() -> StructuredAgentLogger | None:
    """Return the singleton :class:`StructuredAgentLogger` if 3.F is on.

    Returns ``None`` when ``Config.system_control.enabled`` is ``False`` —
    callers use the walrus pattern to short-circuit cleanly::

        if (lg := default_logger()):
            lg.log(kind="tool_call", tool_name="Read")

    The cached instance is keyed by ``log_path``; a config reload that
    changes the path forces a new instance.
    """
    # Local import to avoid a circular: config_store -> system_control
    # would form a loop. config is leaf; we are downstream of it.
    from opencomputer.agent.config_store import load_config

    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001 — defensive
        _log.warning("default_logger: failed to load config: %s", e)
        return None

    if not cfg.system_control.enabled:
        return None

    global _cached_logger, _cached_log_path
    desired_path = Path(cfg.system_control.log_path)
    desired_max = cfg.system_control.json_log_max_size_bytes
    with _default_logger_lock:
        if (
            _cached_logger is None
            or _cached_log_path != desired_path
            or _cached_logger.max_size_bytes != desired_max
        ):
            _cached_logger = StructuredAgentLogger(desired_path, max_size_bytes=desired_max)
            _cached_log_path = desired_path
        return _cached_logger


def reset_default_logger() -> None:
    """Drop the cached singleton. Test-only."""
    global _cached_logger, _cached_log_path
    with _default_logger_lock:
        _cached_logger = None
        _cached_log_path = None


__all__ = [
    "StructuredAgentLogger",
    "default_logger",
    "reset_default_logger",
]
