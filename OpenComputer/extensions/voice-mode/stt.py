"""Speech-to-text pipeline with backend auto-detection.

Three backends, in selection order:

1. ``openai-api`` — wraps :func:`opencomputer.voice.stt.transcribe_audio`
   (existing cost-guarded Whisper API client).
2. ``mlx-whisper`` — Apple Silicon-optimized local Whisper. Fast on M-series.
3. ``whisper-cpp`` — cross-platform whisper.cpp binding via ``pywhispercpp``.

Selection rules (when ``backend=None``):

* ``prefer_local=True`` OR ``OPENAI_API_KEY`` unset → try local chain first
  (mlx → whisper-cpp), then fall back to API if a key is present.
* Otherwise → API first, local chain as fallback.

All backend imports are lazy: this module imports cleanly even on hosts
that have only one (or none) of the optional wheels installed. Failures
surface as :class:`SttError` with install hints.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .audio_capture import AudioBuffer

_log = logging.getLogger("opencomputer.voice_mode.stt")

# Smallest mlx-whisper community model — ~75 MB, fast on M-series.
DEFAULT_MLX_MODEL = "mlx-community/whisper-tiny"
# Smallest whisper.cpp model — ~75 MB ggml; pywhispercpp downloads on demand.
DEFAULT_CPP_MODEL = "tiny"

_VALID_BACKENDS = ("openai-api", "mlx-whisper", "whisper-cpp")
_INSTALL_HINT = (
    "No STT backend available. Install one of:\n"
    "  • OPENAI_API_KEY=sk-... (uses Whisper API)\n"
    "  • pip install opencomputer[voice-mlx]   (macOS Apple Silicon)\n"
    "  • pip install opencomputer[voice-local] (cross-platform whisper.cpp)"
)


class SttError(RuntimeError):
    """Raised when no STT backend is available or transcription fails."""


@dataclass(frozen=True, slots=True)
class TranscribeResult:
    text: str
    backend: str  # "openai-api" | "mlx-whisper" | "whisper-cpp" | "stub"
    duration_seconds: float


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def transcribe(
    audio: AudioBuffer,
    *,
    prefer_local: bool = False,
    backend: str | None = None,
    cost_guard=None,
    model: str = "whisper-1",
) -> TranscribeResult:
    """Transcribe ``audio`` to text via the best available backend.

    Args:
        audio: in-memory PCM buffer from :mod:`audio_capture`.
        prefer_local: try local backends before the OpenAI API. Useful for
            offline use or when minimising API spend.
        backend: explicit backend name; bypasses auto-selection. One of
            ``openai-api``, ``mlx-whisper``, ``whisper-cpp``.
        cost_guard: optional :class:`opencomputer.cost_guard.CostGuard`
            override (applies to the API backend only).
        model: model id. ``whisper-1`` for the API; ignored by local
            backends (they use their respective DEFAULT_* constants).

    Raises:
        SttError: empty audio, unknown backend name, or no working backend.
    """
    if not audio.pcm_bytes:
        raise SttError("audio buffer is empty — nothing to transcribe")

    if backend is not None:
        if backend not in _VALID_BACKENDS:
            raise SttError(
                f"unknown backend {backend!r}; expected one of {_VALID_BACKENDS}"
            )
        return await _dispatch_single(
            backend, audio, cost_guard=cost_guard, model=model
        )

    has_api_key = bool(os.environ.get("OPENAI_API_KEY"))
    use_local_first = prefer_local or not has_api_key

    if use_local_first:
        local_result = await _try_local_chain(audio)
        if local_result is not None:
            return local_result
        # Local failed. Fall back to API only if a key is configured.
        if has_api_key:
            try:
                return await _transcribe_openai_api(
                    audio, cost_guard=cost_guard, model=model
                )
            except Exception as exc:  # noqa: BLE001
                raise SttError(f"OpenAI API fallback failed: {exc}") from exc
        raise SttError(_INSTALL_HINT)

    # API-first path (key is set and prefer_local=False).
    try:
        return await _transcribe_openai_api(
            audio, cost_guard=cost_guard, model=model
        )
    except Exception as api_exc:  # noqa: BLE001
        _log.warning("OpenAI STT failed, falling back to local: %s", api_exc)
        local_result = await _try_local_chain(audio)
        if local_result is not None:
            return local_result
        raise SttError(
            f"OpenAI STT failed and no local fallback available: {api_exc}"
        ) from api_exc


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------


async def _dispatch_single(
    backend: str,
    audio: AudioBuffer,
    *,
    cost_guard,
    model: str,
) -> TranscribeResult:
    """Run a single explicitly-selected backend, surfacing failures clearly."""
    if backend == "openai-api":
        return await _transcribe_openai_api(
            audio, cost_guard=cost_guard, model=model
        )
    if backend == "mlx-whisper":
        try:
            return await asyncio.to_thread(_transcribe_mlx_whisper, audio)
        except ImportError as exc:
            raise SttError(
                "mlx-whisper not installed. "
                "Install: pip install opencomputer[voice-mlx]"
            ) from exc
    if backend == "whisper-cpp":
        try:
            return await asyncio.to_thread(_transcribe_whisper_cpp, audio)
        except ImportError as exc:
            raise SttError(
                "pywhispercpp not installed. "
                "Install: pip install opencomputer[voice-local]"
            ) from exc
    # Defensive — _VALID_BACKENDS already checked.
    raise SttError(f"unknown backend {backend!r}")


async def _try_local_chain(audio: AudioBuffer) -> TranscribeResult | None:
    """Try mlx-whisper, then whisper-cpp. Return None if both unavailable."""
    # 1) mlx-whisper
    try:
        return await asyncio.to_thread(_transcribe_mlx_whisper, audio)
    except ImportError as exc:
        _log.debug("mlx-whisper unavailable: %s", exc)
    except Exception as exc:  # noqa: BLE001
        _log.warning("mlx-whisper transcription failed: %s", exc)

    # 2) whisper-cpp
    try:
        return await asyncio.to_thread(_transcribe_whisper_cpp, audio)
    except ImportError as exc:
        _log.debug("whisper-cpp unavailable: %s", exc)
    except Exception as exc:  # noqa: BLE001
        _log.warning("whisper-cpp transcription failed: %s", exc)

    return None


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------


async def _transcribe_openai_api(
    audio: AudioBuffer,
    *,
    cost_guard,
    model: str,
) -> TranscribeResult:
    """Reuse :func:`opencomputer.voice.stt.transcribe_audio` (cost-guarded).

    Writes the in-memory PCM as a temp WAV (Whisper accepts WAV directly),
    delegates to the existing implementation, and deletes the temp file —
    in line with voice-mode's "audio never persists" rule.
    """
    from opencomputer.voice.stt import transcribe_audio  # lazy import

    start = time.monotonic()
    wav_bytes = audio.to_wav_bytes()

    # NamedTemporaryFile so the path is real on disk for the OpenAI client
    # to attach. ``delete=False`` because Windows can't reopen an open temp;
    # we unlink in the finally below to honour voice-mode's "audio never
    # persists" rule.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_name = tmp.name
    try:
        path = Path(tmp_name)

        def _call():
            return transcribe_audio(path, model=model, cost_guard=cost_guard)

        text = await asyncio.to_thread(_call)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass

    return TranscribeResult(
        text=text,
        backend="openai-api",
        duration_seconds=time.monotonic() - start,
    )


def _transcribe_mlx_whisper(audio: AudioBuffer) -> TranscribeResult:
    """Apple Silicon-optimized local Whisper via ``mlx_whisper``.

    Lazy import so hosts without the wheel still load this module. The
    library accepts a float32 numpy array of mono 16 kHz PCM.
    """
    import mlx_whisper  # noqa: F401  # raises ImportError if missing
    import numpy as np

    if audio.dtype != "int16":
        raise SttError(
            f"mlx-whisper backend expects int16 PCM (got dtype={audio.dtype!r})"
        )
    if audio.channels != 1:
        raise SttError(
            f"mlx-whisper backend expects mono audio (got channels={audio.channels})"
        )

    start = time.monotonic()
    # int16 → float32 in [-1, 1].
    pcm = np.frombuffer(audio.pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    # Resample to 16 kHz if needed — Whisper expects 16 kHz internally.
    # mlx-whisper resamples for us when given a path, but with a numpy array
    # we hand off raw; insist on 16 kHz to keep the contract simple.
    if audio.sample_rate != 16000:
        raise SttError(
            f"mlx-whisper backend expects 16 kHz audio (got {audio.sample_rate} Hz)"
        )

    result = mlx_whisper.transcribe(pcm, path_or_hf_repo=DEFAULT_MLX_MODEL)
    text = (result.get("text") if isinstance(result, dict) else None) or ""
    return TranscribeResult(
        text=text.strip(),
        backend="mlx-whisper",
        duration_seconds=time.monotonic() - start,
    )


def _transcribe_whisper_cpp(audio: AudioBuffer) -> TranscribeResult:
    """Cross-platform local Whisper via ``pywhispercpp``.

    pywhispercpp's ``Model.transcribe`` accepts a float32 numpy array of
    mono 16 kHz PCM and returns a list of ``Segment`` objects.
    """
    import numpy as np
    import pywhispercpp.model  # noqa: F401  # raises ImportError if missing

    if audio.dtype != "int16":
        raise SttError(
            f"whisper-cpp backend expects int16 PCM (got dtype={audio.dtype!r})"
        )
    if audio.channels != 1:
        raise SttError(
            f"whisper-cpp backend expects mono audio (got channels={audio.channels})"
        )
    if audio.sample_rate != 16000:
        raise SttError(
            f"whisper-cpp backend expects 16 kHz audio (got {audio.sample_rate} Hz)"
        )

    start = time.monotonic()
    pcm = np.frombuffer(audio.pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

    # Cache the model on first use — instantiating reloads ggml weights.
    model = _get_whisper_cpp_model()
    segments = model.transcribe(pcm)
    text = " ".join(getattr(s, "text", "") for s in segments).strip()

    return TranscribeResult(
        text=text,
        backend="whisper-cpp",
        duration_seconds=time.monotonic() - start,
    )


_whisper_cpp_model_cache = None


def _get_whisper_cpp_model():
    """Memoize the pywhispercpp Model so we don't reload weights per call."""
    global _whisper_cpp_model_cache
    if _whisper_cpp_model_cache is None:
        from pywhispercpp.model import Model

        _whisper_cpp_model_cache = Model(DEFAULT_CPP_MODEL)
    return _whisper_cpp_model_cache


__all__ = [
    "DEFAULT_CPP_MODEL",
    "DEFAULT_MLX_MODEL",
    "SttError",
    "TranscribeResult",
    "transcribe",
]
