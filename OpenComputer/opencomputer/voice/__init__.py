"""OpenComputer voice — TTS + STT via OpenAI APIs, cost-guarded.

Tier 2.10 / Sub-project G.9. Built on top of:

- ``openai`` SDK (already a project dep) for ``audio.speech.create`` (TTS)
  and ``audio.transcriptions.create`` (Whisper STT).
- ``opencomputer.cost_guard`` for pre-flight budget checks + usage recording.
- ``ChannelCapabilities.VOICE_OUT/VOICE_IN`` (G.2) which Telegram already
  declares — ``send_voice`` and ``download_attachment`` are in place.

Public surface:

- :func:`synthesize_speech` — text → audio file path. Default format ``opus``
  (Telegram voice messages); also supports mp3/aac/flac/wav/pcm.
- :func:`transcribe_audio` — audio file path → text. Whisper.
- :class:`VoiceConfig` — model + voice + cost-guard knobs.
- Cost helpers: :func:`tts_cost_usd`, :func:`stt_cost_usd` for budget projection.

The Telegram adapter's ``_handle_update`` is wired to call
:func:`transcribe_audio` on inbound voice messages so the agent receives
a regular text MessageEvent. Falls back to attachment-only when
budget is exhausted (still costs zero).
"""

from __future__ import annotations

from opencomputer.voice.costs import (
    OPENAI_STT_USD_PER_MINUTE,
    OPENAI_TTS_USD_PER_1K_CHARS,
    stt_cost_usd,
    tts_cost_usd,
)
from opencomputer.voice.stt import transcribe_audio
from opencomputer.voice.tts import VoiceConfig, synthesize_speech
from opencomputer.voice.tts_edge import (
    DEFAULT_EDGE_VOICE,
    EdgeTTSError,
    synthesize_edge_speech,
)

__all__ = [
    "DEFAULT_EDGE_VOICE",
    "EdgeTTSError",
    "OPENAI_STT_USD_PER_MINUTE",
    "OPENAI_TTS_USD_PER_1K_CHARS",
    "VoiceConfig",
    "stt_cost_usd",
    "synthesize_edge_speech",
    "synthesize_speech",
    "transcribe_audio",
    "tts_cost_usd",
]
