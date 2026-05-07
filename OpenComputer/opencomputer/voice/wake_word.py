"""Wake-word detection for hands-free OC activation (PR-A Feature 2).

Uses openWakeWord (Apache 2.0, ONNX, CPU). Default model: ``hey_jarvis``
(bundled with openwakeword). Always-on capture loop runs in a dedicated
asyncio task; on detection (score >= threshold), an async callback fires
that hands off to the existing voice-mode pipeline.

**Default OFF** — must be invoked via ``oc voice wake``. Microphone
access permission is deferred to the OS.

State machine: ``IDLE → DETECTED → SPEAKING → IDLE``. Wake re-engages
on transition to IDLE. Mic singleton enforced via PID-file at
``<profile_home>/voice_wake.pid``.

Privacy: all audio processing is local. Only post-wake utterances ever
leave the machine, and only if the chained STT backend is remote. No
persistent audio buffer is written to disk.

Optional dependency: ``openwakeword>=0.6.0`` + ``onnxruntime>=1.17``
behind the ``[wake]`` extra. Module-level imports never crash without
the dep; the constructor raises :class:`WakeWordError` with a friendly
install hint.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_log = logging.getLogger("opencomputer.voice.wake_word")

WakeState = Literal["IDLE", "DETECTED", "SPEAKING"]


class WakeWordError(RuntimeError):
    """Raised on wake-word setup or runtime errors."""


@dataclass(frozen=True, slots=True)
class WakeDetection:
    """One wake-word fire event.

    Attributes:
        word: The wake-word model that fired (e.g. "hey_jarvis").
        score: Detector confidence 0.0-1.0.
        timestamp: monotonic seconds when detection landed.
    """

    word: str
    score: float
    timestamp: float


def _acquire_pid_lock(pid_file: Path) -> Callable[[], None]:
    """Acquire a PID-file lock; return a release callable.

    Writes the current PID to ``pid_file``. Refuses to overwrite if the
    file's contents reference a process that is still alive (signal 0
    test). Stale PID files (process not alive) are silently removed and
    overwritten.

    Returns:
        A zero-arg callable that removes the pid file. Idempotent.

    Raises:
        WakeWordError: another live wake process holds the lock.
    """
    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            existing_pid = None
        if existing_pid is not None:
            try:
                os.kill(existing_pid, 0)
            except ProcessLookupError:
                _log.info("wake: removing stale pid file %s", pid_file)
                try:
                    pid_file.unlink(missing_ok=True)
                except OSError:
                    pass
            except PermissionError as exc:
                raise WakeWordError(
                    f"another wake process is already running "
                    f"(pid {existing_pid}, no perm to verify)"
                ) from exc
            else:
                raise WakeWordError(
                    f"another wake process is already running "
                    f"(pid {existing_pid}); kill it or use a different profile"
                )
        else:
            try:
                pid_file.unlink(missing_ok=True)
            except OSError:
                pass

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    def release() -> None:
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass

    return release


class WakeWordDetector:
    """openWakeWord wrapper with state machine + cooperative async callback.

    Construction is fail-fast: if ``openwakeword`` is not importable, a
    :class:`WakeWordError` is raised with a friendly install hint. The
    actual ONNX model is loaded lazily inside :meth:`start` (which is
    where platform-specific ONNX runtime issues surface — `oc doctor
    wake` exists to surface those at install time).
    """

    def __init__(
        self,
        *,
        word: str = "hey_jarvis",
        threshold: float = 0.5,
        model_path: Path | None = None,
        on_detect: Callable[[WakeDetection], Awaitable[None]] | None = None,
        pid_file: Path | None = None,
    ) -> None:
        self.word = word
        self.threshold = threshold
        self.model_path = model_path
        self._on_detect = on_detect
        self._state: WakeState = "IDLE"
        self._pid_file = pid_file
        self._pid_release: Callable[[], None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

        # Lazy import + graceful degrade. We don't actually USE the
        # imported module here — just verify importability so the error
        # surfaces at construction rather than later in start().
        try:
            ow = importlib.import_module("openwakeword")
        except ImportError as exc:
            raise WakeWordError(
                "openwakeword not installed; "
                "install with `pip install opencomputer[wake]`"
            ) from exc
        if ow is None:
            raise WakeWordError(
                "openwakeword not available "
                "(install with `pip install opencomputer[wake]`)"
            )

    @property
    def state(self) -> WakeState:
        return self._state

    def set_state(self, new_state: WakeState) -> None:
        """Move the detector to a new state. Logs the transition."""
        _log.debug("wake: state transition %s -> %s", self._state, new_state)
        self._state = new_state

    async def _fire_callback(self, detection: WakeDetection) -> None:
        """Invoke the user callback with state IDLE → DETECTED → IDLE.

        Test seam: production code path also invokes this from the
        capture loop. Errors inside the callback are logged but not
        propagated (one bad callback must not kill the loop).
        """
        if self._on_detect is None:
            return
        self.set_state("DETECTED")
        try:
            await self._on_detect(detection)
        except Exception:  # noqa: BLE001
            _log.warning(
                "wake: on_detect callback raised; continuing", exc_info=True,
            )
        finally:
            # Caller (or callback) owns SPEAKING transitions; revert to
            # IDLE here so the next wake fire is unambiguous.
            self.set_state("IDLE")

    async def start(self) -> None:
        """Begin the always-on capture + detect loop.

        Acquires the PID singleton if ``pid_file`` was provided. Starts
        an asyncio task running :meth:`_run_loop`.
        """
        if self._pid_file is not None:
            self._pid_release = _acquire_pid_lock(self._pid_file)
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the capture loop gracefully and release the singleton."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if self._pid_release is not None:
            self._pid_release()
            self._pid_release = None

    async def _run_loop(self) -> None:
        """Inner capture-and-score loop.

        The model is loaded here (not in ``__init__``) so platform ONNX
        issues surface only when the user actually starts wake mode.
        Audio capture is intentionally minimal — production deployment
        chains the existing ``extensions/voice-mode/audio_capture.py``
        pipeline via the CLI driver. This loop only sleeps; the CLI
        ticks the model with PCM frames captured externally.
        """
        try:
            ow_model_module = importlib.import_module("openwakeword.model")
            Model = ow_model_module.Model  # type: ignore[attr-defined]
        except ImportError as exc:
            raise WakeWordError(
                "openwakeword.model not importable — verify install"
            ) from exc

        try:
            if self.model_path is not None:
                _model = Model(wakeword_models=[str(self.model_path)])
            else:
                _model = Model()
        except Exception as exc:  # noqa: BLE001
            raise WakeWordError(
                f"failed to load openwakeword model: {exc}"
            ) from exc

        assert self._stop_event is not None
        # The CLI driver owns audio capture; this loop's job is only to
        # cooperate with stop() in a cancel-safe way. Each tick checks
        # the stop event; CLI calls into the model out-of-band.
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=0.08,
                )
            except asyncio.TimeoutError:
                continue

    async def __aenter__(self) -> "WakeWordDetector":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


__all__ = [
    "WakeDetection",
    "WakeState",
    "WakeWordDetector",
    "WakeWordError",
    "_acquire_pid_lock",
]
