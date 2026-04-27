"""Voice-mode main orchestrator (T5).

Wires together the T1-T4 pieces — :mod:`audio_capture`, :mod:`vad`,
:mod:`stt`, :mod:`tts_playback` — into the continuous push-to-talk loop
that ``opencomputer voice talk`` invokes.

Single-turn flow (see :func:`run_single_turn`):

    capture (already started) → stop → AudioBuffer
        → VAD gate → drop if no speech
        → STT  → drop if empty transcript
        → agent_runner(user_text)
        → synthesize_and_play(agent_text) (with barge-in)
        → TurnResult

Multi-turn loop (see :func:`run_voice_loop`):

    while not stop_trigger():
        wait for record_trigger
        start a fresh AudioCapture
        run a single turn
        log result; failures are swallowed so one bad turn doesn't kill the loop

The loop intentionally runs ``record_trigger`` / ``stop_trigger`` (which may
block on stdin / a keyboard hook) inside ``asyncio.to_thread`` so the event
loop stays responsive.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audio_capture import AudioCapture, AudioCaptureError
from .stt import transcribe
from .tts_playback import synthesize_and_play
from .vad import detect_speech

_log = logging.getLogger("opencomputer.voice_mode.orchestrator")

# Anything shorter than this is treated as a fat-finger / accidental press
# rather than a real utterance, and skipped before VAD/STT.
_MIN_UTTERANCE_SECONDS = 0.3


class VoiceModeError(RuntimeError):
    """Raised on unrecoverable orchestrator errors."""


@dataclass(frozen=True, slots=True)
class TurnResult:
    """Outcome of one capture → transcribe → agent → speak cycle."""

    user_text: str
    agent_text: str
    interrupted: bool
    user_audio_seconds: float
    agent_speak_seconds: float


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_single_turn(
    *,
    agent_runner: Callable[[str], Awaitable[str]],
    cost_guard: Any,
    capture: AudioCapture,
    prefer_local_stt: bool = False,
) -> TurnResult | None:
    """Run a single capture → transcribe → agent → speak cycle.

    ``capture`` is expected to have been started already (e.g. via
    :meth:`AudioCapture.start`); this helper calls ``capture.stop()`` to
    pull the buffer back. Callers therefore control the start/stop edges
    (push-to-talk gating) without us reaching into keyboard handling here.

    Returns ``None`` (no error, no agent invocation) if the audio is too
    short, contains no speech, or transcribes to an empty string. Real
    failures (STT crash, agent crash, playback crash) propagate to the
    caller — :func:`run_voice_loop` is the failure-isolation boundary.
    """
    buffer = capture.stop()

    if buffer.duration_seconds < _MIN_UTTERANCE_SECONDS:
        _log.debug(
            "skipping turn: capture %.3fs < min %.3fs",
            buffer.duration_seconds,
            _MIN_UTTERANCE_SECONDS,
        )
        return None

    vad_result = detect_speech(buffer)
    if not vad_result.is_speech:
        _log.debug(
            "skipping turn: VAD speech_ratio=%.2f below threshold",
            vad_result.speech_ratio,
        )
        return None

    print("📝 transcribing...", flush=True)
    stt_result = await transcribe(
        buffer, prefer_local=prefer_local_stt, cost_guard=cost_guard
    )
    user_text = stt_result.text.strip()
    if not user_text:
        _log.debug("skipping turn: empty transcript from %s", stt_result.backend)
        return None

    print(f"   you: {user_text}", flush=True)
    print("💭 thinking...", flush=True)
    agent_result = agent_runner(user_text)
    if inspect.isawaitable(agent_result):
        agent_text = await agent_result
    else:
        agent_text = agent_result  # type: ignore[assignment]
    agent_text = (agent_text or "").strip()

    print(f"   agent: {agent_text}", flush=True)
    print("🔊 speaking...", flush=True)
    playback_result = await synthesize_and_play(agent_text, cost_guard=cost_guard)

    return TurnResult(
        user_text=user_text,
        agent_text=agent_text,
        interrupted=bool(getattr(playback_result, "interrupted", False)),
        user_audio_seconds=buffer.duration_seconds,
        agent_speak_seconds=float(
            getattr(playback_result, "duration_seconds", 0.0) or 0.0
        ),
    )


async def run_voice_loop(
    *,
    agent_runner: Callable[[str], Awaitable[str]],
    cost_guard: Any,
    profile_home: Path,
    record_trigger: Callable[[], Any] | None = None,
    stop_trigger: Callable[[], bool] | None = None,
    prefer_local_stt: bool = False,
    audio_config: Any = None,
) -> None:
    """Continuous push-to-talk voice loop.

    Each iteration:

    1. Check ``stop_trigger`` — exit if it returns truthy.
    2. Wait for ``record_trigger`` (e.g. spacebar press / Enter).
    3. Start a fresh :class:`AudioCapture`, hand off to
       :func:`run_single_turn`, log the result.
    4. Swallow per-turn errors so a bad STT call / dropped network
       doesn't kill the whole session — print a one-line warning instead.

    ``profile_home`` is plumbed through for future per-profile config
    (e.g. preferred voice, transcript persistence opt-in). The current
    implementation logs it for traceability but does not write to disk.
    """
    if stop_trigger is None:
        def stop_trigger() -> bool:  # noqa: E306 — local default
            return False

    if record_trigger is None:
        def record_trigger() -> Any:  # noqa: E306 — local default
            # Default: block on Enter. Real keyboard hooks land in a polish PR.
            input("Press Enter to record (then Enter to stop): ")
            return True

    _log.info("voice-mode loop starting (profile=%s)", profile_home)

    capture_kwargs: dict[str, Any] = {}
    if audio_config is not None:
        for attr in ("sample_rate", "channels", "dtype", "device"):
            value = getattr(audio_config, attr, None)
            if value is not None:
                capture_kwargs[attr] = value

    while True:
        # Allow stop_trigger to be sync or async; run sync flavour off-loop
        # so it can block on stdin without freezing the event loop.
        should_stop = await _maybe_to_thread(stop_trigger)
        if should_stop:
            _log.info("voice-mode loop stopping (stop_trigger=True)")
            return

        try:
            await _maybe_to_thread(record_trigger)
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — defensive
            _log.warning("record_trigger raised; ending loop: %s", exc)
            return

        print("🎤 listening...", flush=True)
        try:
            capture = AudioCapture(**capture_kwargs)
            capture.start()
        except AudioCaptureError as exc:
            print(f"  voice-mode: capture unavailable: {exc}", flush=True)
            _log.error("AudioCapture.start failed: %s", exc)
            return
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001
            _log.warning("unexpected capture-start error: %s", exc)
            continue

        try:
            result = await run_single_turn(
                agent_runner=agent_runner,
                cost_guard=cost_guard,
                capture=capture,
                prefer_local_stt=prefer_local_stt,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Make sure we don't leak an open audio stream on cancel.
            with _suppress_capture_errors(capture):
                capture.stop()
            raise
        except Exception as exc:  # noqa: BLE001 — failure-isolated turn
            _log.warning("voice-mode turn failed: %s", exc)
            print(f"  (turn failed: {exc} — ready for next)", flush=True)
            with _suppress_capture_errors(capture):
                if capture.is_recording():
                    capture.stop()
            continue

        if result is None:
            _log.debug("turn produced no transcript (silent/short)")
            continue

        _log.info(
            "turn done: %.2fs in / %.2fs out  interrupted=%s",
            result.user_audio_seconds,
            result.agent_speak_seconds,
            result.interrupted,
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _maybe_to_thread(fn: Callable[[], Any]) -> Any:
    """Run a possibly-blocking sync callable off-loop.

    If ``fn`` returns an awaitable (the user passed an async trigger),
    await it directly instead of going through ``to_thread``.
    """
    result = await asyncio.to_thread(fn)
    if inspect.isawaitable(result):
        return await result
    return result


class _suppress_capture_errors:  # noqa: N801 — context-manager idiom
    """Best-effort cleanup helper around AudioCapture.stop()."""

    def __init__(self, capture: AudioCapture) -> None:
        self._capture = capture

    def __enter__(self) -> _suppress_capture_errors:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Swallow only AudioCaptureError; any other exception propagates.
        return not (exc is not None and not isinstance(exc, AudioCaptureError))


__all__ = [
    "TurnResult",
    "VoiceModeError",
    "run_single_turn",
    "run_voice_loop",
]
