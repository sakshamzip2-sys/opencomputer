"""Per-session flight recorder — structured timeline of agent events.

Port of OpenClaw's trajectory-bundle concept (see
``docs/OC-FROM-OPENCLAW.md`` item 9). For every session the agent
loop opens a bundle on first turn and emits structured events as
JSON-lines until session shutdown. The bundle is then exportable via
``oc trajectory export <session-id>`` into a redacted support
artifact for debugging or replay.

Two on-disk files per session::

    <profile>/trajectories/<session-id>/events.jsonl
    <profile>/trajectories/<session-id>/session-branch.json

* ``events.jsonl`` is append-only. Each line is a JSON object with at
  minimum ``{"ts": <epoch>, "type": <event-type>, ...}``.
* ``session-branch.json`` records branching metadata when a session
  forks (parent / child ids) so replay can stitch the tree back
  together. Single-shot file, rewritten on every branch event.

Bounded: 10 MB cap per events.jsonl, 200,000 events soft cap. When
either limit is hit, further appends are dropped with a single
``WARNING`` log line — never raise.

Public surface:

* :class:`TrajectoryBundle` — the recorder.
* :func:`open_bundle` — convenience to acquire / create one per
  session id.
* :class:`TrajectoryBundleError` — raised by ``export`` paths only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: Per-file size cap. Hard ceiling — beyond this we stop appending.
DEFAULT_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB

#: Soft event-count cap. Stop appending past this so a buggy run
#: can't fill the disk with 10 GB of trace.
DEFAULT_MAX_EVENTS: int = 200_000


class TrajectoryBundleError(RuntimeError):
    """Raised by export paths only. The recorder itself never raises
    on append — the agent loop must not crash because a trace write
    failed."""


@dataclass(slots=True)
class _BundleState:
    """Per-bundle mutable state — kept off the dataclass so the
    public :class:`TrajectoryBundle` reads as immutable from the
    outside."""

    bytes_written: int = 0
    events_written: int = 0
    closed: bool = False


@dataclass(slots=True)
class TrajectoryBundle:
    """A session-scoped flight recorder.

    Instances are obtained via :func:`open_bundle` so the loader can
    cache one per session. Two threads writing through the same
    bundle is safe — appends are serialised by an internal lock.

    Attributes
    ----------
    session_id : str
        The session whose trajectory is being recorded.
    root : Path
        Directory holding ``events.jsonl`` + ``session-branch.json``.
        Created lazily on first write.
    max_bytes : int
        Hard cap (default 10 MB). Beyond this, ``record`` is a no-op
        with a debug log.
    max_events : int
        Soft cap (default 200 000).
    """

    session_id: str
    root: Path
    max_bytes: int = DEFAULT_MAX_BYTES
    max_events: int = DEFAULT_MAX_EVENTS

    _state: _BundleState = field(default_factory=_BundleState)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def events_path(self) -> Path:
        return self.root / "events.jsonl"

    @property
    def branch_path(self) -> Path:
        return self.root / "session-branch.json"

    def record(self, event_type: str, **payload: Any) -> None:
        """Append one event. ``event_type`` is required; all other
        fields land in the JSON line verbatim.

        Never raises. On write failure logs at ``WARNING`` and drops
        the event. Honour ``max_bytes`` / ``max_events`` caps with
        a debug log on the first drop.
        """
        if not event_type or not isinstance(event_type, str):
            _log.debug("trajectory: dropped event with empty/non-str type")
            return
        with self._lock:
            if self._state.closed:
                return
            if self._state.events_written >= self.max_events:
                if self._state.events_written == self.max_events:
                    _log.warning(
                        "trajectory %s: event cap %d hit — dropping further events",
                        self.session_id,
                        self.max_events,
                    )
                self._state.events_written += 1  # tick once past cap to silence the log
                return
            if self._state.bytes_written >= self.max_bytes:
                return
            rec: dict[str, Any] = {
                "ts": time.time(),
                "type": event_type,
            }
            # Filter out None and non-serialisable values defensively.
            for k, v in payload.items():
                if v is None:
                    continue
                rec[k] = v
            try:
                line = json.dumps(rec, default=str, ensure_ascii=False) + "\n"
            except Exception as exc:  # noqa: BLE001 — payload may raise during str()
                _log.warning(
                    "trajectory: skipping unserialisable event %r: %s",
                    event_type,
                    exc,
                )
                return
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with self.events_path.open("a", encoding="utf-8") as fp:
                    fp.write(line)
            except OSError as exc:
                _log.warning(
                    "trajectory %s: write failed (%s); dropping event %r",
                    self.session_id,
                    exc,
                    event_type,
                )
                return
            self._state.bytes_written += len(line.encode("utf-8"))
            self._state.events_written += 1

    def record_branch(self, parent_id: str | None, child_id: str | None) -> None:
        """Update ``session-branch.json`` with the parent/child link.

        Idempotent — overwrites the whole file each time (the file
        is tiny). Records both ids so replay can walk either
        direction.
        """
        data: dict[str, Any] = {
            "session_id": self.session_id,
            "ts": time.time(),
        }
        if parent_id:
            data["parent"] = parent_id
        if child_id:
            data["child"] = child_id
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.branch_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.branch_path)
        except OSError as exc:
            _log.warning(
                "trajectory %s: branch metadata write failed (%s); skipping",
                self.session_id,
                exc,
            )

    def close(self) -> None:
        """Stop accepting further events. Idempotent."""
        with self._lock:
            self._state.closed = True


def open_bundle(
    session_id: str,
    *,
    root_dir: Path | None = None,
    max_bytes: int | None = None,
    max_events: int | None = None,
) -> TrajectoryBundle:
    """Acquire a :class:`TrajectoryBundle` for ``session_id``.

    Args:
        session_id: required, non-empty.
        root_dir: explicit container dir. When omitted, resolves to
            ``<profile_root>/trajectories/``. Setting
            ``OPENCOMPUTER_TRAJECTORY_DIR`` env var also overrides.
        max_bytes / max_events: cap overrides. ``None`` → use defaults.
    """
    if not session_id or not isinstance(session_id, str):
        raise TrajectoryBundleError("session_id must be a non-empty string")
    base = root_dir
    if base is None:
        env = os.environ.get("OPENCOMPUTER_TRAJECTORY_DIR")
        if env:
            base = Path(env)
        else:
            # Best-effort profile root resolution — fall back to ./trajectories
            # if the profile module isn't importable (testing edge case).
            try:
                from opencomputer.profiles import get_default_root

                base = get_default_root() / "trajectories"
            except Exception:  # noqa: BLE001
                base = Path.cwd() / "trajectories"
    bundle_root = base / session_id
    return TrajectoryBundle(
        session_id=session_id,
        root=bundle_root,
        max_bytes=max_bytes or DEFAULT_MAX_BYTES,
        max_events=max_events or DEFAULT_MAX_EVENTS,
    )


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_EVENTS",
    "TrajectoryBundle",
    "TrajectoryBundleError",
    "open_bundle",
]
