"""Local PCM16 audio I/O for realtime voice — sounddevice mic + speaker.

Not a port of any OpenClaw file — telephony platforms use Twilio Media
Streams as the audio sink/source. For local-mic use we use the
``sounddevice`` library (already a dep via the ``[voice]`` extra) which
wraps PortAudio. Format is 16 kHz mono signed-16 PCM (matches OpenAI
Realtime's ``pcm16`` audio_format).

Lifecycle:
* ``start()`` — open both input + output streams.
* ``stop()`` — close streams, idempotent.
* ``send_audio(chunk)`` — write PCM16 bytes to the speaker.
* ``clear_audio()`` — flush any pending speaker buffer (used on
  barge-in when the user starts talking mid-reply).
* ``is_open()`` — True between start() and stop().
* ``on_mic_chunk(chunk)`` — caller-supplied handler invoked from the
  audio thread for each captured PCM16 chunk.
"""
from __future__ import annotations

from collections.abc import Callable

try:
    import sounddevice as sd
except (ImportError, OSError):  # OSError: PortAudio missing
    sd = None  # type: ignore[assignment]


_INPUT_SAMPLE_RATE = 16_000  # mic — every realtime provider so far accepts 16 kHz
_DEFAULT_OUTPUT_SAMPLE_RATE = 16_000  # speaker — Gemini ships 24 kHz, others vary
_CHANNELS = 1
_DTYPE = "int16"
# Default block size: 800 frames @ 16 kHz = 50ms. Latency-tuned callers
# (the realtime CLI's --block-size-ms) override to ~320 = 20ms which
# trades ~2.5× more WS frames for ~30ms shaved off mic-buffer latency
# AND finer-grained VAD silence detection on the server side.
_DEFAULT_BLOCK_SIZE = 800


class LocalAudioIO:
    """Mic capture + speaker playback for realtime voice.

    ``output_sample_rate`` defaults to 16 kHz (matches OpenAI Realtime).
    Override to 24 kHz for Gemini Live, or whatever rate the provider
    streams. Mic input is locked at 16 kHz — every realtime provider so
    far accepts that, and resampling on the way IN belongs in the bridge
    if a future provider demands something different.

    ``block_size`` is the per-frame sample count for both streams. 800
    (50ms) is the latency-tolerant default. Drop to 320 (20ms) for
    snappier turn-taking — costs more WS frames but reduces both the
    mic-buffer wait AND the granularity at which the server VAD can
    detect silence boundaries.
    """

    def __init__(
        self,
        *,
        on_mic_chunk: Callable[[bytes], None],
        output_sample_rate: int = _DEFAULT_OUTPUT_SAMPLE_RATE,
        block_size: int = _DEFAULT_BLOCK_SIZE,
    ) -> None:
        # Validate integer args first — they don't need PortAudio so
        # bad values fail fast even on dev machines without sounddevice.
        if output_sample_rate <= 0:
            raise ValueError(f"output_sample_rate must be positive, got {output_sample_rate}")
        if block_size <= 0:
            raise ValueError(f"block_size must be positive, got {block_size}")
        if sd is None:
            raise RuntimeError(
                "sounddevice not available. Install with "
                "`pip install opencomputer[voice]` and ensure PortAudio is on the system."
            )
        self._on_mic_chunk = on_mic_chunk
        self._output_sample_rate = output_sample_rate
        self._block_size = block_size
        self._input_stream = None
        self._output_stream = None
        self._started = False

    def _mic_callback(self, indata: bytes, frames: int, time_info, status) -> None:
        try:
            self._on_mic_chunk(bytes(indata))
        except Exception:  # noqa: BLE001 — never crash audio thread
            pass

    def start(self) -> None:
        if self._started:
            return
        self._input_stream = sd.RawInputStream(
            samplerate=_INPUT_SAMPLE_RATE,
            channels=_CHANNELS,
            dtype=_DTYPE,
            blocksize=self._block_size,
            callback=self._mic_callback,
        )
        self._output_stream = sd.RawOutputStream(
            samplerate=self._output_sample_rate,
            channels=_CHANNELS,
            dtype=_DTYPE,
            blocksize=self._block_size,
        )
        self._input_stream.start()
        self._output_stream.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        try:
            if self._input_stream is not None:
                self._input_stream.stop()
                self._input_stream.close()
        finally:
            self._input_stream = None
        try:
            if self._output_stream is not None:
                self._output_stream.stop()
                self._output_stream.close()
        finally:
            self._output_stream = None
        self._started = False

    def is_open(self) -> bool:
        return self._started

    def send_audio(self, audio: bytes) -> None:
        if self._output_stream is not None:
            self._output_stream.write(audio)

    def clear_audio(self) -> None:
        """Drop any pending speaker buffer (barge-in).

        sounddevice doesn't expose a direct ``flush`` — the cleanest
        portable approach is stop+restart of the output stream.
        """
        out = self._output_stream
        if out is None:
            return
        try:
            out.stop()
            out.close()
        except Exception:  # noqa: BLE001 — best effort
            pass
        self._output_stream = sd.RawOutputStream(
            samplerate=self._output_sample_rate,
            channels=_CHANNELS,
            dtype=_DTYPE,
            blocksize=self._block_size,
        )
        self._output_stream.start()


__all__ = ["LocalAudioIO"]
