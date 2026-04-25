"""Text-to-speech via OpenAI's ``audio.speech.create``.

Synthesizes audio in the requested format (default ``opus``, which is
Telegram's native voice-message codec) and writes it to disk. Cost-guarded:
checks ``CostGuard.check_budget`` before the API call; records actual usage
after.

Usage::

    from opencomputer.voice import synthesize_speech, VoiceConfig

    out_path = synthesize_speech(
        "good morning, here's your stock briefing for april 25",
        cfg=VoiceConfig(model="tts-1", voice="alloy", format="opus"),
        dest_dir=Path("/tmp"),
    )
    # out_path is /tmp/<uuid>.ogg ready for adapter.send_voice(...)

Errors:

- :class:`opencomputer.cost_guard.BudgetExceeded` — daily/monthly cap hit
  before the call; nothing is sent.
- :class:`RuntimeError` — empty text, OpenAI API failure, or write failure.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from opencomputer.cost_guard import BudgetExceeded, get_default_guard
from opencomputer.voice.costs import tts_cost_usd

logger = logging.getLogger("opencomputer.voice.tts")


# Format → file extension. Telegram voice expects ``.ogg`` (Opus codec);
# discord/slack accept multiple formats. PCM is raw and rarely useful.
_FORMAT_EXTENSIONS: dict[str, str] = {
    "opus": ".ogg",
    "mp3": ".mp3",
    "aac": ".aac",
    "flac": ".flac",
    "wav": ".wav",
    "pcm": ".pcm",
}

_VALID_VOICES = frozenset(
    {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
)


@dataclass(frozen=True, slots=True)
class VoiceConfig:
    """Knobs for TTS synthesis. Defaults are sensible for Telegram delivery.

    Attributes:
        model: OpenAI TTS model. ``tts-1`` (faster, cheaper) or ``tts-1-hd``
            (higher quality, 2× cost).
        voice: One of ``alloy / echo / fable / onyx / nova / shimmer``.
        format: ``opus`` (Telegram), ``mp3`` (Discord/SMS), ``wav`` (loss-less).
        speed: 0.25–4.0 (default 1.0). Faster = cheaper-to-deliver but harder to follow.
    """

    model: str = "tts-1"
    voice: str = "alloy"
    format: str = "opus"
    speed: float = 1.0


def synthesize_speech(
    text: str,
    *,
    cfg: VoiceConfig | None = None,
    dest_dir: Path | str | None = None,
    cost_guard: object | None = None,
    openai_client: object | None = None,
) -> Path:
    """Synthesize ``text`` into an audio file. Returns the path written.

    Args:
        text: The text to speak. OpenAI's hard limit is 4096 characters per call.
        cfg: Voice config (model / voice / format / speed). Defaults applied
            if ``None``.
        dest_dir: Directory to write the file into. Created if missing. If
            ``None``, uses the system temp dir.
        cost_guard: Override the default cost-guard (used in tests).
        openai_client: Override the OpenAI client (used in tests).

    Raises:
        ValueError: empty text, invalid voice/format, or text > 4096 chars.
        BudgetExceeded: daily or monthly cap blocked the call.
        RuntimeError: API or filesystem failure.
    """
    cfg = cfg or VoiceConfig()
    if not text or not text.strip():
        raise ValueError("text must be non-empty")
    if len(text) > 4096:
        raise ValueError(
            f"text length {len(text)} exceeds OpenAI TTS limit of 4096 characters"
        )
    if cfg.voice not in _VALID_VOICES:
        raise ValueError(
            f"voice must be one of {sorted(_VALID_VOICES)}, got {cfg.voice!r}"
        )
    if cfg.format not in _FORMAT_EXTENSIONS:
        raise ValueError(
            f"format must be one of {sorted(_FORMAT_EXTENSIONS)}, got {cfg.format!r}"
        )

    # Pre-flight budget check.
    guard = cost_guard or get_default_guard()
    projected = tts_cost_usd(text, model=cfg.model)
    decision = guard.check_budget("openai", projected_cost_usd=projected)
    if not decision.allowed:
        logger.warning("TTS blocked by cost-guard: %s", decision.reason)
        raise BudgetExceeded(decision.reason)

    # Resolve output path.
    out_dir = Path(dest_dir) if dest_dir else Path(os.environ.get("TMPDIR", "/tmp"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _FORMAT_EXTENSIONS[cfg.format]
    out_path = out_dir / f"tts_{uuid.uuid4().hex[:12]}{ext}"

    # Resolve client. Constructed lazily so tests can patch.
    if openai_client is None:
        from openai import OpenAI

        openai_client = OpenAI()

    # OpenAI's audio.speech.create returns a streamed response; .stream_to_file
    # writes it directly to disk.
    try:
        with openai_client.audio.speech.with_streaming_response.create(  # type: ignore[attr-defined]
            model=cfg.model,
            voice=cfg.voice,
            input=text,
            response_format=cfg.format,
            speed=cfg.speed,
        ) as response:
            response.stream_to_file(out_path)
    except Exception as exc:  # noqa: BLE001 — wrap in RuntimeError for caller
        raise RuntimeError(f"OpenAI TTS failed: {type(exc).__name__}: {exc}") from exc

    # Record actual usage. The OpenAI TTS API doesn't return per-call cost in
    # the response, so we use the projected amount as the recorded cost.
    guard.record_usage("openai", cost_usd=projected, operation=f"tts:{cfg.model}")
    logger.info(
        "TTS synthesized %d chars (model=%s voice=%s format=%s) → %s ($%.4f)",
        len(text), cfg.model, cfg.voice, cfg.format, out_path, projected,
    )
    return out_path


__all__ = ["VoiceConfig", "synthesize_speech"]
