"""Gemini provider plugin — entry point.

Currently registers only the realtime voice surface. Chat-completion
``BaseProvider`` for ``models/gemini-*`` is not yet ported (separate scope —
google-genai SDK shape, multimodal, streaming, etc.).
"""
from __future__ import annotations

try:
    from realtime import OUTPUT_RATE_HZ, GeminiRealtimeBridge  # plugin-loader mode
except ImportError:  # pragma: no cover
    from extensions.gemini_provider.realtime import (
        OUTPUT_RATE_HZ,
        GeminiRealtimeBridge,
    )


def _gemini_realtime_factory(
    *,
    callbacks,
    api_key,
    model,
    instructions,
    tools=(),
    silence_duration_ms=500,
    prefix_padding_ms=40,
    enable_transcription=True,
    **kwargs,
):
    """Build a GeminiRealtimeBridge from a session callbacks dict.

    Ignores ``voice`` (passed by the CLI for OpenAI parity) — Gemini Live
    doesn't expose a voice picker on this model. ``tools`` is a tuple of
    ``RealtimeVoiceTool`` declarations the model will see in the setup
    message; without it the model has no idea what actions are available.
    """
    return GeminiRealtimeBridge(
        api_key=api_key,
        model=model or None,  # bridge uses _DEFAULT_MODEL when None
        instructions=instructions,
        tools=tools,
        silence_duration_ms=silence_duration_ms,
        prefix_padding_ms=prefix_padding_ms,
        enable_transcription=enable_transcription,
        on_audio=callbacks["on_audio"],
        on_clear_audio=callbacks["on_clear_audio"],
        on_transcript=callbacks.get("on_transcript"),
        on_tool_call=callbacks.get("on_tool_call"),
        on_ready=callbacks.get("on_ready"),
        on_error=callbacks.get("on_error"),
        on_close=callbacks.get("on_close"),
    )


def register(api) -> None:  # PluginAPI duck-typed
    # Realtime voice bridge — registered with metadata the CLI uses to
    # validate the env var AND size LocalAudioIO at the provider's
    # native output rate (24 kHz for Gemini). No more hardcoded provider
    # tables in ``cli_voice.py`` — everything the CLI needs is here.
    if hasattr(api, "register_realtime_bridge"):
        api.register_realtime_bridge(
            "gemini",
            _gemini_realtime_factory,
            env_var="GEMINI_API_KEY",
            audio_sink_kwargs={"output_sample_rate": OUTPUT_RATE_HZ},
        )
