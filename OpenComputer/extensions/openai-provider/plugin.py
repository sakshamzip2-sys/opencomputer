"""
OpenAI provider plugin — entry point.

Flat layout: plugin.py is the entry, sibling modules are importable via
plain names because the plugin loader puts the plugin root on sys.path.
"""

from __future__ import annotations

try:
    from provider import OpenAIProvider  # plugin-loader mode
    from realtime import OpenAIRealtimeBridge
except ImportError:  # pragma: no cover
    from extensions.openai_provider.provider import OpenAIProvider  # package mode
    from extensions.openai_provider.realtime import OpenAIRealtimeBridge


def _openai_realtime_factory(
    *,
    callbacks,
    api_key,
    model,
    voice,
    instructions,
    tools=(),
    silence_duration_ms=500,
    **kwargs,
):
    """Build an OpenAIRealtimeBridge from a session callbacks dict.

    Standard signature for realtime-voice factories registered via
    ``api.register_realtime_bridge``: callbacks dict first, provider
    args next, ``**kwargs`` to swallow forward-compat fields. ``tools``
    surfaces OC's tool registry to the model in the session.update
    message; without it the model has no actions available.
    """
    return OpenAIRealtimeBridge(
        api_key=api_key,
        model=model or "gpt-realtime-1.5",
        voice=voice or "alloy",
        instructions=instructions,
        tools=tools,
        silence_duration_ms=silence_duration_ms,
        on_audio=callbacks["on_audio"],
        on_clear_audio=callbacks["on_clear_audio"],
        on_transcript=callbacks.get("on_transcript"),
        on_tool_call=callbacks.get("on_tool_call"),
        on_ready=callbacks.get("on_ready"),
        on_error=callbacks.get("on_error"),
        on_close=callbacks.get("on_close"),
    )


def register(api) -> None:  # PluginAPI duck-typed
    api.register_provider("openai", OpenAIProvider)
    # Realtime voice bridge — looked up by name from the CLI. The CLI
    # uses ``env_var`` to validate the API key before connecting; no
    # ``audio_sink_kwargs`` because OpenAI Realtime is symmetric 16 kHz
    # in/out and ``LocalAudioIO`` already defaults to 16 kHz output.
    if hasattr(api, "register_realtime_bridge"):
        api.register_realtime_bridge(
            "openai",
            _openai_realtime_factory,
            env_var="OPENAI_API_KEY",
        )
