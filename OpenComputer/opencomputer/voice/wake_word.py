"""Wake-word detection for hands-free OC activation (PR-A Feature 2).

Uses openWakeWord (Apache 2.0, ONNX, CPU). Default wake-word:
``hey_open_computer`` — but this is NOT bundled with openwakeword.
The user must train a custom ONNX model for this phrase using the
openWakeWord training pipeline (~30 min on CPU; Piper TTS synthesis
+ training notebook at https://github.com/dscripka/openWakeWord).

If the custom ``hey_open_computer.onnx`` model is not found at
startup, the detector AUTOMATICALLY falls back to ``hey_jarvis``
(bundled with openwakeword) and logs a helpful hint pointing at the
training URL. This keeps OC working out of the box while preserving
the user's intent of "hey open computer" once they train their model.

Bundled wake-words available without training:
    hey_jarvis, alexa, hey_mycroft, hey_rhasspy, ok_google

Always-on capture loop runs in a dedicated asyncio task; on detection
(score >= threshold), an async callback fires that hands off to the
existing voice-mode pipeline.

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
import time as _time_module
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_log = logging.getLogger("opencomputer.voice.wake_word")

WakeState = Literal["IDLE", "DETECTED", "SPEAKING"]

#: Wake-words bundled with openwakeword — usable without custom training.
BUNDLED_WAKE_WORDS: frozenset[str] = frozenset({
    "hey_jarvis",
    "alexa",
    "hey_mycroft",
    "hey_rhasspy",
    "ok_google",
})

#: Default fallback when the user's chosen word isn't a custom-trained
#: model and isn't in the bundled set.
FALLBACK_BUNDLED_WORD: str = "hey_jarvis"

#: User-facing hint surfaced when fallback fires.
TRAINING_URL: str = "https://github.com/dscripka/openWakeWord"

#: Audio frame size openWakeWord expects: 1280 samples at 16 kHz = 80 ms.
WAKE_FRAME_SAMPLES: int = 1280

#: Sample rate openWakeWord expects.
WAKE_SAMPLE_RATE: int = 16000

#: Suppress further detections for this many seconds after a fire so
#: a single utterance doesn't trigger 5 callbacks. Also gives downstream
#: voice-mode hand-off time to claim the mic.
_DETECTION_COOLDOWN_S: float = 1.5


def _resolve_profile_home() -> Path:
    """Return the active profile's home directory.

    Mirrors the logic the CLI uses (active profile via
    ``read_active_profile``, falling back to ``~/.opencomputer/default``).
    Lives in the detector module so :func:`_auto_discover_model` can
    use it without pulling Typer into the import path.
    """
    try:
        from opencomputer.profiles import (  # noqa: PLC0415
            profile_home_dir,
            read_active_profile,
        )
        active = read_active_profile() or "default"
        return profile_home_dir(active)
    except Exception:  # noqa: BLE001
        return Path.home() / ".opencomputer" / "default"


def wake_models_dir() -> Path:
    """Return ``<profile_home>/wake_models/`` (caller creates on demand)."""
    return _resolve_profile_home() / "wake_models"


def _auto_discover_model(word: str) -> Path | None:
    """Look for ``<profile_home>/wake_models/<word>.onnx``.

    Returns the path when present and non-empty; ``None`` otherwise.
    Used by :meth:`WakeWordDetector._resolve_word` to pick up a model
    that the user trained via ``oc voice train-wake`` without requiring
    them to pass ``--model`` on every wake invocation.
    """
    candidate = wake_models_dir() / f"{word}.onnx"
    if candidate.is_file() and candidate.stat().st_size > 0:
        return candidate
    return None


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
        word: str = "hey_open_computer",
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
        # PR-A Feature 2: track whether the active word is the requested
        # one or a fallback (so the CLI can show an honest indicator).
        self._effective_word: str = word
        self._fell_back: bool = False
        # PR-A Feature 2 (production): pause/resume support so the
        # detection callback can hand the mic over to voice-mode for
        # one turn without contending for the audio device.
        self._pause_event: asyncio.Event | None = None
        self._resume_event: asyncio.Event | None = None

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

    @property
    def effective_word(self) -> str:
        """The wake-word actually being listened for (post-fallback)."""
        return self._effective_word

    @property
    def fell_back(self) -> bool:
        """True if the requested word was unavailable and a fallback was used."""
        return self._fell_back

    def _resolve_word(self) -> str:
        """Pick the actual wake-word to listen for.

        Order:
          1. ``model_path`` set → use ``self.word`` as the label, model
             loaded from path (caller is responsible for the file).
          2. ``self.word`` is in ``BUNDLED_WAKE_WORDS`` → use as-is.
          3. Auto-discovered model at ``<profile_home>/wake_models/<word>.onnx``
             → use it (sets ``self.model_path`` for downstream loaders).
          4. Otherwise → fall back to ``FALLBACK_BUNDLED_WORD`` and log
             a hint pointing at the training URL / CLI.
        """
        if self.model_path is not None:
            self._effective_word = self.word
            self._fell_back = False
            return self.word
        if self.word in BUNDLED_WAKE_WORDS:
            self._effective_word = self.word
            self._fell_back = False
            return self.word
        # Auto-discover a trained ONNX before falling back. This closes
        # the loop opened by PR-A's hey_open_computer default — a user
        # who trained via `oc voice train-wake` doesn't need to pass
        # --model on every wake invocation.
        auto_path = _auto_discover_model(self.word)
        if auto_path is not None:
            _log.info(
                "wake: auto-discovered trained model at %s", auto_path,
            )
            self.model_path = auto_path
            self._effective_word = self.word
            self._fell_back = False
            return self.word
        # Custom word requested but no model_path → fall back.
        _log.warning(
            "wake: custom wake-word '%s' is not bundled and no model_path "
            "provided; falling back to '%s'. Train a custom model with "
            "`oc voice train-wake` (~30 min on CPU). Reference: %s",
            self.word, FALLBACK_BUNDLED_WORD, TRAINING_URL,
        )
        self._effective_word = FALLBACK_BUNDLED_WORD
        self._fell_back = True
        return FALLBACK_BUNDLED_WORD

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
        self._pause_event = asyncio.Event()
        self._resume_event = asyncio.Event()
        self._task = asyncio.create_task(self._run_loop())

    async def pause(self) -> None:
        """Close the audio input stream and wait until ``resume()``.

        The detection loop releases the mic so another consumer
        (voice-mode capture, push-to-talk) can take it. Returns once the
        stream has been confirmed closed.
        """
        if self._pause_event is None or self._resume_event is None:
            return
        self._resume_event.clear()
        self._pause_event.set()
        # Best-effort: small wait so the loop has time to close the
        # stream before pause() returns. Tests that don't need the
        # actual stream pass through quickly.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if not self._pause_event.is_set():
                break

    def resume(self) -> None:
        """Resume the detection loop after ``pause()``.

        Synchronous because the asyncio.Event.set is thread-safe and
        callers (voice-mode loop) are mid-cleanup.
        """
        if self._resume_event is not None:
            self._resume_event.set()

    async def stop(self) -> None:
        """Stop the capture loop gracefully and release the singleton."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        if self._pid_release is not None:
            self._pid_release()
            self._pid_release = None

    async def _run_loop(self) -> None:
        """Production capture-and-score loop.

        Spawns a sounddevice InputStream at 16 kHz mono int16, accumulates
        1280-sample (80 ms) frames into an asyncio.Queue, and on each
        full frame calls openwakeword.Model.predict(). When the active
        word's score crosses :attr:`threshold`, fires the user callback
        via :meth:`_fire_callback`.

        Cooldown: after a fire, suppresses further detections for
        :data:`_DETECTION_COOLDOWN_S` seconds so a single utterance
        doesn't trigger multiple callbacks. The cooldown also gives
        the downstream voice-mode hand-off time to claim the mic.

        Audio thread → asyncio bridge: sounddevice's callback runs on
        its own thread; we marshal frames via ``call_soon_threadsafe``
        + ``Queue.put_nowait``. Backpressure (model fell behind) drops
        the oldest frames rather than blocking the audio thread —
        better to skip a frame than glitch the input stream.

        Lifecycle:
            ``start()`` schedules this coroutine; ``stop()`` sets the
            stop event and waits up to 2 s; the InputStream is always
            closed in the finally block.
        """
        try:
            import numpy as np  # type: ignore[import-untyped]
            ow_model_module = importlib.import_module("openwakeword.model")
            Model = ow_model_module.Model  # type: ignore[attr-defined]
        except ImportError as exc:
            raise WakeWordError(
                f"openwakeword.model / numpy not importable: {exc}"
            ) from exc

        try:
            import sounddevice as sd  # type: ignore[import-untyped]
        except (ImportError, OSError) as exc:
            raise WakeWordError(
                f"sounddevice not available for wake capture: {exc}. "
                "Install: pip install sounddevice "
                "(Linux: also `apt install libportaudio2`)"
            ) from exc

        # Resolve word (may set _effective_word to fallback when a
        # custom word is requested without a model_path).
        active_word = self._resolve_word()
        try:
            if self.model_path is not None:
                model = Model(wakeword_models=[str(self.model_path)])
            else:
                # Bundled-only path — pretrained ONNX models bundled
                # with openwakeword. ``predict()`` returns a dict keyed
                # by model name; we filter on ``active_word`` below.
                model = Model()
        except Exception as exc:  # noqa: BLE001
            raise WakeWordError(
                f"failed to load openwakeword model: {exc}"
            ) from exc

        assert self._stop_event is not None
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)

        def _audio_callback(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                _log.debug("wake audio status: %s", status)
            try:
                pcm_bytes = bytes(indata)
            except Exception:  # noqa: BLE001
                return
            try:
                loop.call_soon_threadsafe(queue.put_nowait, pcm_bytes)
            except asyncio.QueueFull:
                _log.debug("wake: audio queue full — dropping frame")
            except RuntimeError:
                # Loop closed (shutdown race) — ignore.
                pass

        last_fire_at: float = 0.0

        def _open_stream():
            return sd.InputStream(
                samplerate=WAKE_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=WAKE_FRAME_SAMPLES,
                callback=_audio_callback,
            )

        try:
            stream = _open_stream()
            stream.start()
        except Exception as exc:  # noqa: BLE001
            raise WakeWordError(
                f"failed to start audio input stream: {exc}"
            ) from exc

        try:
            while not self._stop_event.is_set():
                # PR-A: honor pause/resume so voice-mode hand-off can
                # claim the mic. We close the stream during pause and
                # reopen on resume; this is the only reliable cross-
                # platform way to share a single mic device.
                if (
                    self._pause_event is not None
                    and self._pause_event.is_set()
                ):
                    try:
                        stream.stop()
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                    # Drain any frames queued before the pause so the
                    # post-resume detector starts from fresh audio.
                    while not queue.empty():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    # Signal pause-acknowledged + wait for resume.
                    self._pause_event.clear()
                    assert self._resume_event is not None
                    while (
                        not self._stop_event.is_set()
                        and not self._resume_event.is_set()
                    ):
                        try:
                            await asyncio.wait_for(
                                self._resume_event.wait(), timeout=0.5,
                            )
                        except TimeoutError:
                            continue
                    if self._stop_event.is_set():
                        break
                    self._resume_event.clear()
                    # Reopen the stream so the next iteration has audio.
                    try:
                        stream = _open_stream()
                        stream.start()
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "wake: failed to reopen stream after resume: %s",
                            exc,
                        )
                        # Bail; can't continue without audio.
                        break
                    continue
                # Pull next frame with a short timeout so stop_event
                # remains responsive even when the mic is silent.
                try:
                    pcm_bytes = await asyncio.wait_for(
                        queue.get(), timeout=0.5,
                    )
                except TimeoutError:
                    continue

                # Cooldown: keep draining the queue (so it doesn't fill
                # while the user speaks post-wake) but skip prediction.
                now = _time_module.monotonic()
                if now - last_fire_at < _DETECTION_COOLDOWN_S:
                    continue

                samples = np.frombuffer(pcm_bytes, dtype=np.int16)
                if samples.size != WAKE_FRAME_SAMPLES:
                    # Partial frame from a flushing stream — skip.
                    continue
                try:
                    scores = model.predict(samples)
                except Exception:  # noqa: BLE001
                    _log.warning("wake: model.predict raised", exc_info=True)
                    continue

                # ``scores`` is a dict keyed by model name. Accept an
                # exact match on active_word, OR — when the user asked
                # for a custom word but is on fallback — look up the
                # fallback name in the scores dict.
                lookup_word = active_word
                if lookup_word not in scores and self._fell_back:
                    lookup_word = FALLBACK_BUNDLED_WORD
                score = float(scores.get(lookup_word, 0.0))
                if score >= self.threshold:
                    last_fire_at = now
                    detection = WakeDetection(
                        word=self.word,
                        score=score,
                        timestamp=now,
                    )
                    # Schedule the callback so the audio queue keeps
                    # draining. The callback may take seconds (voice-
                    # mode hand-off) and we don't want to block here.
                    asyncio.create_task(self._fire_callback(detection))
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    async def __aenter__(self) -> WakeWordDetector:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()


__all__ = [
    "BUNDLED_WAKE_WORDS",
    "FALLBACK_BUNDLED_WORD",
    "TRAINING_URL",
    "WAKE_FRAME_SAMPLES",
    "WAKE_SAMPLE_RATE",
    "WakeDetection",
    "WakeState",
    "WakeWordDetector",
    "WakeWordError",
    "_acquire_pid_lock",
    "_auto_discover_model",
    "_resolve_profile_home",
    "wake_models_dir",
]
