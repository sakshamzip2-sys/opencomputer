"""Edge TTS (Microsoft Azure) — free TTS provider, no API key required.

Tier 3.D headline win from docs/refs/hermes-agent/2026-04-28-major-gaps.md.
**Edge TTS is free** — no API key, no signup. Uses the public neural-TTS
endpoints exposed via Microsoft Edge's read-aloud feature. Quality is
comparable to OpenAI ``tts-1``; latency is similar; voices are different
but plentiful (300+ across 100+ languages).

The library:
    https://github.com/rany2/edge-tts
    pip install edge-tts  (or: pip install opencomputer[voice-edge])

Why this matters: removes the VOICE_TOOLS_OPENAI_KEY friction point on
TTS. A user who hasn't set up any voice API key still gets working
voice output via this provider.

Module is **lazy-imported** — production calls ``import edge_tts`` only
when synthesize_speech_edge() is invoked, so installs without the extra
don't pay the import cost.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("opencomputer.voice.edge_tts")


_DEFAULT_VOICE = "en-US-AriaNeural"
_MAX_TEXT_CHARS = 8192

_KNOWN_VOICES: frozenset[str] = frozenset({
    "en-US-AriaNeural", "en-US-GuyNeural", "en-US-JennyNeural",
    "en-US-DavisNeural", "en-US-EmmaNeural", "en-US-AndrewNeural",
    "en-GB-SoniaNeural", "en-GB-RyanNeural",
    "en-AU-NatashaNeural", "en-AU-WilliamNeural",
    "es-ES-ElviraNeural", "es-MX-DaliaNeural",
    "fr-FR-DeniseNeural", "de-DE-KatjaNeural",
    "ja-JP-NanamiNeural", "zh-CN-XiaoxiaoNeural",
})


class EdgeTTSNotInstalledError(RuntimeError):
    """Raised when edge-tts isn't installed but synthesize_speech_edge is called."""


# Backwards-compat alias — original name was used in tests / external callers
# before the N818 lint convention required the *Error suffix.
EdgeTTSNotInstalled = EdgeTTSNotInstalledError


@dataclass(frozen=True, slots=True)
class EdgeVoiceConfig:
    """Knobs for Edge TTS synthesis."""
    voice: str = _DEFAULT_VOICE
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"


def _import_edge_tts() -> Any:
    """Lazy-import edge_tts; raise a helpful error if missing."""
    try:
        import edge_tts  # type: ignore[import-not-found]
    except ImportError as e:
        raise EdgeTTSNotInstalled(
            "edge-tts not installed. Install with `pip install edge-tts` "
            "or `pip install opencomputer[voice-edge]`. It's free and "
            "requires no API key."
        ) from e
    return edge_tts


async def _async_synthesize(
    text: str,
    cfg: EdgeVoiceConfig,
    out_path: Path,
    *,
    edge_tts_module: Any | None = None,
) -> None:
    """Async core — most of the lib is async-native."""
    mod = edge_tts_module or _import_edge_tts()
    communicate = mod.Communicate(
        text,
        cfg.voice,
        rate=cfg.rate,
        volume=cfg.volume,
        pitch=cfg.pitch,
    )
    await communicate.save(str(out_path))


def synthesize_speech_edge(
    text: str,
    *,
    cfg: EdgeVoiceConfig | None = None,
    dest_dir: Path | str | None = None,
    edge_tts_module: Any | None = None,
) -> Path:
    """Synthesize ``text`` to MP3 via Edge TTS. Returns the path written.

    Edge TTS doesn't support format selection — output is always MP3.
    The caller can transcode if a different format is needed.

    Raises:
        ValueError: empty text or text > 8192 chars.
        EdgeTTSNotInstalled: the edge-tts package isn't installed AND
            no test module was injected.
        RuntimeError: synthesis failed (network, voice not found, etc.).
    """
    cfg = cfg or EdgeVoiceConfig()
    if not text or not text.strip():
        raise ValueError("text must be non-empty")
    if len(text) > _MAX_TEXT_CHARS:
        raise ValueError(
            f"text length {len(text)} exceeds Edge TTS limit of {_MAX_TEXT_CHARS} chars"
        )

    import tempfile
    import uuid
    if dest_dir is None:
        out_dir = Path(tempfile.gettempdir())
    else:
        out_dir = Path(dest_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"edge_tts_{uuid.uuid4().hex[:8]}.mp3"

    try:
        asyncio.run(
            _async_synthesize(text, cfg, out_path, edge_tts_module=edge_tts_module)
        )
    except EdgeTTSNotInstalled:
        raise
    except Exception as e:
        raise RuntimeError(
            f"Edge TTS synthesis failed: {type(e).__name__}: {e}"
        ) from e

    if not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(
            "Edge TTS synthesis produced no output (empty/missing file)"
        )
    logger.info(
        "Edge TTS synthesized %d chars (voice=%s) → %s",
        len(text), cfg.voice, out_path,
    )
    return out_path


__all__ = [
    "EdgeVoiceConfig",
    "EdgeTTSNotInstalled",
    "synthesize_speech_edge",
]
