"""Edge-TTS adapter that plugs into ``VoiceConfig`` (Hermes parity, 2026-05-01).

The lower-level ``opencomputer.voice.edge_tts`` module already gives us
free Edge TTS synthesis (Tier 3.D). What it lacks is integration with
:class:`opencomputer.voice.tts.VoiceConfig` and format conversion — Edge
TTS only emits MP3, but Telegram voice bubbles want OGG/Opus.

This module provides :func:`synthesize_edge_speech`, the dispatch target
called by :func:`opencomputer.voice.tts.synthesize_speech` when
``cfg.provider == "edge"``. It does three jobs:

* Translates ``VoiceConfig`` (model/voice/format/speed) into Edge-TTS's
  ``EdgeVoiceConfig`` (voice/rate/volume/pitch).
* Calls the existing ``synthesize_speech_edge`` for the actual MP3.
* Re-muxes via ``ffmpeg`` when ``cfg.format != "mp3"``.

No cost guard — Edge TTS is free.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from opencomputer.voice.edge_tts import (
    EdgeTTSNotInstalledError,
    EdgeVoiceConfig,
    synthesize_speech_edge,
)

logger = logging.getLogger("opencomputer.voice.tts_edge")

DEFAULT_EDGE_VOICE = "en-US-AriaNeural"


# Format → file extension. Mirrors the dict in tts.py — kept private to
# avoid a circular import on tts.py.
_FORMAT_EXTENSIONS: dict[str, str] = {
    "opus": ".ogg",
    "mp3": ".mp3",
    "aac": ".aac",
    "flac": ".flac",
    "wav": ".wav",
    "pcm": ".pcm",
}


class EdgeTTSError(RuntimeError):
    """Raised when Edge TTS synth or ffmpeg post-processing fails."""


def _speed_to_rate(speed: float) -> str:
    """Convert OC's 0.25-4.0 speed multiplier to Edge's '+N%' / '-N%' string.

    Edge expects a percentage delta, e.g. ``+20%`` for 1.2× speed.
    """
    if abs(speed - 1.0) < 0.001:
        return "+0%"
    pct = round((speed - 1.0) * 100)
    return f"{pct:+d}%"


def synthesize_edge_speech(
    text: str,
    *,
    cfg,  # opencomputer.voice.tts.VoiceConfig — typed loose to avoid circular import
    dest_dir: Path | str | None = None,
) -> Path:
    """Synthesize ``text`` via Edge TTS, applying ``cfg`` knobs.

    Args:
        text: Text to speak.
        cfg: ``VoiceConfig`` with ``provider="edge"``. ``voice`` is any
            Microsoft neural voice ID; ``format`` is one of the entries in
            ``_FORMAT_EXTENSIONS``; ``speed`` is mapped to ``rate``.
        dest_dir: Output directory. Defaults to ``$TMPDIR``.

    Raises:
        ValueError: empty text or unsupported format.
        EdgeTTSError: edge-tts not installed, network failure, or ffmpeg
            re-mux failure (when format != mp3).
    """
    if not text or not text.strip():
        raise ValueError("text must be non-empty")
    if cfg.format not in _FORMAT_EXTENSIONS:
        raise ValueError(
            f"format must be one of {sorted(_FORMAT_EXTENSIONS)}, got {cfg.format!r}"
        )

    voice = cfg.voice or DEFAULT_EDGE_VOICE
    out_dir = Path(dest_dir) if dest_dir else Path(os.environ.get("TMPDIR", "/tmp"))
    out_dir.mkdir(parents=True, exist_ok=True)

    edge_cfg = EdgeVoiceConfig(
        voice=voice,
        rate=_speed_to_rate(cfg.speed),
    )

    try:
        mp3_path = synthesize_speech_edge(text, cfg=edge_cfg, dest_dir=out_dir)
    except EdgeTTSNotInstalledError as exc:
        raise EdgeTTSError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — wrap for caller
        raise EdgeTTSError(f"Edge TTS synth failed: {exc}") from exc

    if cfg.format == "mp3":
        return mp3_path
    return _convert_with_ffmpeg(mp3_path, target_format=cfg.format, out_dir=out_dir)


# ─── helpers ─────────────────────────────────────────────────────


def _convert_with_ffmpeg(src: Path, *, target_format: str, out_dir: Path) -> Path:
    """Re-mux ``src`` (MP3) to ``target_format`` via ffmpeg.

    Telegram voice bubbles need OGG/Opus. Other targets are best-effort.
    Source file is unlinked on success so we don't leave intermediates.
    """
    if shutil.which("ffmpeg") is None:
        raise EdgeTTSError(
            "edge-tts emits MP3; converting to "
            f"{target_format!r} requires ffmpeg (not found on PATH). "
            "Install ffmpeg or set format='mp3' on VoiceConfig."
        )

    dst = out_dir / f"edge_tts_{uuid.uuid4().hex[:8]}{_FORMAT_EXTENSIONS[target_format]}"

    if target_format == "opus":
        codec_args = ["-c:a", "libopus", "-b:a", "48k"]
    elif target_format == "wav":
        codec_args = ["-c:a", "pcm_s16le"]
    elif target_format == "flac":
        codec_args = ["-c:a", "flac"]
    elif target_format == "aac":
        codec_args = ["-c:a", "aac", "-b:a", "96k"]
    elif target_format == "pcm":
        codec_args = ["-f", "s16le", "-acodec", "pcm_s16le"]
    else:
        codec_args = ["-c:a", "copy"]

    cmd = ["ffmpeg", "-y", "-i", str(src), *codec_args, str(dst)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)  # noqa: S603,S607
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
        raise EdgeTTSError(f"ffmpeg conversion failed: {stderr.strip() or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise EdgeTTSError("ffmpeg conversion timed out after 30s") from exc

    try:
        src.unlink(missing_ok=True)
    except OSError:
        pass

    return dst


__all__ = [
    "DEFAULT_EDGE_VOICE",
    "EdgeTTSError",
    "synthesize_edge_speech",
]
