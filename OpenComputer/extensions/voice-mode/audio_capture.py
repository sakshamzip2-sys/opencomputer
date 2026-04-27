"""Audio capture via sounddevice — cross-platform push-to-talk recording.

Lazy imports of sounddevice/numpy/wave so import-time install issues
don't crash OC for users without audio hardware (SSH, Docker, etc.).
"""
from __future__ import annotations

import logging
import threading
import wave
from dataclasses import dataclass
from io import BytesIO
from typing import Any

_log = logging.getLogger("opencomputer.voice_mode.audio_capture")

DEFAULT_SAMPLE_RATE = 16000  # Whisper expects 16kHz
DEFAULT_CHANNELS = 1
DEFAULT_DTYPE = "int16"


@dataclass(frozen=True, slots=True)
class AudioBuffer:
    """In-memory audio buffer. NEVER persisted to disk."""

    pcm_bytes: bytes
    sample_rate: int
    channels: int
    dtype: str

    @property
    def duration_seconds(self) -> float:
        if self.sample_rate <= 0 or self.channels <= 0:
            return 0.0
        bytes_per_sample = {"int16": 2, "int32": 4, "float32": 4}.get(self.dtype, 2)
        total_samples = len(self.pcm_bytes) // (bytes_per_sample * self.channels)
        return total_samples / self.sample_rate

    def to_wav_bytes(self) -> bytes:
        """Encode as WAV (in-memory). Whisper API accepts WAV."""
        bio = BytesIO()
        sample_width = {"int16": 2, "int32": 4, "float32": 4}[self.dtype]
        with wave.open(bio, "wb") as wf:
            wf.setnchannels(self.channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(self.sample_rate)
            wf.writeframes(self.pcm_bytes)
        return bio.getvalue()


class AudioCaptureError(RuntimeError):
    """Raised when audio capture cannot start (no device, permission denied, etc.)."""


class AudioCapture:
    """Push-to-talk recorder. Lazy-imports sounddevice/numpy on first use."""

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        dtype: str = DEFAULT_DTYPE,
        device: int | str | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._dtype = dtype
        self._device = device
        self._chunks: list[bytes] = []
        self._stream: Any = None
        self._lock = threading.Lock()

    def _import_sd(self):
        try:
            import sounddevice as sd
            return sd
        except (ImportError, OSError) as exc:
            # OSError on Linux when PortAudio is missing
            raise AudioCaptureError(
                f"sounddevice not available: {exc}. Install: pip install sounddevice "
                f"(Linux: also `apt install libportaudio2`)"
            ) from exc

    def list_devices(self) -> list[dict]:
        """List available input audio devices."""
        sd = self._import_sd()
        try:
            return [d for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
        except Exception as exc:  # noqa: BLE001
            raise AudioCaptureError(f"failed to query audio devices: {exc}") from exc

    def start(self) -> None:
        """Begin recording into in-memory buffer."""
        sd = self._import_sd()
        with self._lock:
            if self._stream is not None:
                raise AudioCaptureError("recording already in progress")
            self._chunks = []

            def _callback(indata, frames, time_info, status):  # noqa: ARG001
                if status:
                    _log.debug("audio status: %s", status)
                # indata is numpy array; convert to bytes
                self._chunks.append(bytes(indata))

            try:
                self._stream = sd.InputStream(
                    samplerate=self._sample_rate,
                    channels=self._channels,
                    dtype=self._dtype,
                    device=self._device,
                    callback=_callback,
                )
                self._stream.start()
            except Exception as exc:  # noqa: BLE001
                self._stream = None
                raise AudioCaptureError(f"failed to start recording: {exc}") from exc

    def stop(self) -> AudioBuffer:
        """Stop recording and return the captured buffer."""
        with self._lock:
            if self._stream is None:
                raise AudioCaptureError("no recording in progress")
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:  # noqa: BLE001
                _log.warning("error closing audio stream: %s", exc)
            self._stream = None

            pcm_bytes = b"".join(self._chunks)
            self._chunks = []

        return AudioBuffer(
            pcm_bytes=pcm_bytes,
            sample_rate=self._sample_rate,
            channels=self._channels,
            dtype=self._dtype,
        )

    def is_recording(self) -> bool:
        with self._lock:
            return self._stream is not None
