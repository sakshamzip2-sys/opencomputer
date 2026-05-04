"""Piper TTS provider — local neural text-to-speech.

Wave 5 T8 — Hermes-port (8d302e37a). Lazy import (``piper-tts`` is only
imported when actually used so users without local TTS hardware don't
pay the import cost). Voice cache keyed on ``(model_path, use_cuda)``.
Auto-downloads the voice file on first use when ``voice`` is a name (e.g.
``en_US-lessac-medium``); accepts a path verbatim when ``voice`` ends
in ``.onnx``. WAV output is the native Piper format.

Default voice ``en_US-lessac-medium`` was chosen by the upstream Hermes
port for being a good neutral default — replace with any
`piper.download_voices`-known name in config.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

#: Hermes default — neutral en_US voice, medium quality.
DEFAULT_VOICE: str = "en_US-lessac-medium"


@dataclass(slots=True, frozen=True)
class PiperConfig:
    """Knobs for a Piper synthesis call.

    All knobs except ``voice`` and ``use_cuda`` are optional and forwarded
    to ``PiperVoice.synthesize_wav`` only when set, so users get sensible
    upstream defaults unless they override.
    """

    voice: str = DEFAULT_VOICE
    use_cuda: bool = False
    length_scale: float | None = None
    noise_scale: float | None = None
    noise_w_scale: float | None = None
    volume: float | None = None
    normalize_audio: bool | None = None


def _voice_cache_dir() -> Path:
    """Where downloaded voices are persisted between runs.

    Default: ``~/.opencomputer/cache/piper-voices/``. Override with the
    ``OC_HOME`` env var to point at a different profile root.
    """
    base = (
        Path(os.environ.get("OC_HOME", str(Path.home() / ".opencomputer")))
        / "cache"
        / "piper-voices"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _import_piper():
    """Lazy import — raises with an actionable error when piper-tts is missing."""
    try:
        import piper  # noqa: PLC0415 — lazy by design
    except ImportError as exc:
        raise RuntimeError(
            "Piper TTS requires the piper-tts package. "
            "Install with: pip install piper-tts",
        ) from exc
    return piper


def _resolve_voice_path(voice: str) -> Path:
    """Resolve ``voice`` to a concrete .onnx path on disk.

    If ``voice`` is already a path to an existing .onnx file, return it
    unchanged. Otherwise treat it as a Piper voice id and download it
    into the cache via ``python -m piper.download_voices`` on first use.
    """
    p = Path(voice)
    if p.suffix == ".onnx" and p.exists():
        return p
    cache = _voice_cache_dir()
    target = cache / f"{voice}.onnx"
    if target.exists():
        return target
    logger.info("Downloading Piper voice %s into %s", voice, cache)
    subprocess.run(
        [
            "python",
            "-m",
            "piper.download_voices",
            "--download-dir",
            str(cache),
            voice,
        ],
        check=True,
    )
    return target


@lru_cache(maxsize=8)
def _load_voice(path: str, use_cuda: bool):
    """Cache voice instances keyed on ``(path, use_cuda)``.

    Loading a Piper voice is non-trivial (PyTorch / ONNX runtime init);
    caching keeps repeat synthesis fast. Cap at 8 to bound memory.
    """
    piper = _import_piper()
    return piper.PiperVoice.load(path, use_cuda=use_cuda)


class PiperTTS:
    """Synthesize text to a WAV file via local Piper neural TTS.

    Construct once per voice config; ``synthesize`` is reentrant — call
    it as many times as you like. Each call runs the model in a worker
    thread so the agent loop's event loop doesn't block.
    """

    def __init__(self, config: PiperConfig | None = None) -> None:
        self.config = config or PiperConfig()

    def _get_voice(self):
        path = _resolve_voice_path(self.config.voice)
        return _load_voice(str(path), self.config.use_cuda)

    async def synthesize(self, text: str, *, out_path: str) -> str:
        """Render ``text`` into a WAV file at ``out_path``; return the path."""
        voice = self._get_voice()  # may raise if piper-tts not installed
        synth_kwargs: dict[str, object] = {}
        if self.config.length_scale is not None:
            synth_kwargs["length_scale"] = self.config.length_scale
        if self.config.noise_scale is not None:
            synth_kwargs["noise_scale"] = self.config.noise_scale
        if self.config.noise_w_scale is not None:
            synth_kwargs["noise_w_scale"] = self.config.noise_w_scale
        if self.config.volume is not None:
            synth_kwargs["volume"] = self.config.volume
        if self.config.normalize_audio is not None:
            synth_kwargs["normalize_audio"] = self.config.normalize_audio
        # PiperVoice.synthesize_wav() is sync — push to a worker thread
        # so we don't block the event loop.
        await asyncio.to_thread(
            voice.synthesize_wav, text, out_path, **synth_kwargs,
        )
        return out_path


__all__ = [
    "DEFAULT_VOICE",
    "PiperConfig",
    "PiperTTS",
]
