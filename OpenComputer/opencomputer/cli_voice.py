"""``opencomputer voice`` CLI — manual TTS / STT smoke and one-off use.

Subcommands:

    opencomputer voice synthesize "text" [-o OUT.ogg]   — TTS to file
    opencomputer voice transcribe FILE                   — STT to stdout
    opencomputer voice cost-estimate "text" [...]        — projected USD without calling

Real cost-guard checks apply on synthesize / transcribe (see ``opencomputer cost``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from opencomputer.cost_guard import BudgetExceeded
from opencomputer.voice import (
    VoiceConfig,
    stt_cost_usd,
    synthesize_speech,
    transcribe_audio,
    tts_cost_usd,
)

voice_app = typer.Typer(
    name="voice",
    help="Text-to-speech and speech-to-text via OpenAI (cost-guarded).",
    no_args_is_help=True,
)


@voice_app.command("synthesize")
def voice_synthesize(
    text: Annotated[str, typer.Argument(help="Text to speak.")],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Path to write the audio file (default: temp file).")
    ] = None,
    model: Annotated[str, typer.Option("--model", help="tts-1 (default) or tts-1-hd.")] = "tts-1",
    voice: Annotated[str, typer.Option("--voice", help="alloy / echo / fable / onyx / nova / shimmer.")] = "alloy",
    fmt: Annotated[str, typer.Option("--format", help="opus (default — Telegram) / mp3 / wav / flac / aac / pcm.")] = "opus",
) -> None:
    """Synthesize speech from text and save it to a file."""
    cfg = VoiceConfig(model=model, voice=voice, format=fmt)
    try:
        out_path = synthesize_speech(
            text, cfg=cfg, dest_dir=output.parent if output else None
        )
    except BudgetExceeded as exc:
        typer.secho(f"Blocked by cost-guard: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc
    except (ValueError, RuntimeError) as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc

    if output and output.resolve() != out_path.resolve():
        # User specified a particular output filename; rename the synthesized file.
        out_path.rename(output)
        out_path = output

    cost = tts_cost_usd(text, model=model)
    typer.secho(f"Synthesized → {out_path}", fg="green")
    typer.echo(f"  chars:  {len(text)}")
    typer.echo(f"  cost:   ${cost:.4f}")


@voice_app.command("transcribe")
def voice_transcribe(
    audio: Annotated[Path, typer.Argument(help="Audio file path.")],
    model: Annotated[str, typer.Option("--model", help="Default whisper-1.")] = "whisper-1",
    language: Annotated[
        str | None,
        typer.Option("--language", "-l", help="ISO-639-1 hint (e.g. en, hi). Optional."),
    ] = None,
) -> None:
    """Transcribe an audio file to text."""
    try:
        text = transcribe_audio(audio, model=model, language=language)
    except BudgetExceeded as exc:
        typer.secho(f"Blocked by cost-guard: {exc}", fg="red", err=True)
        raise typer.Exit(2) from exc
    except (ValueError, RuntimeError) as exc:
        typer.secho(f"Error: {exc}", fg="red", err=True)
        raise typer.Exit(1) from exc

    typer.echo(text)


@voice_app.command("cost-estimate")
def voice_cost_estimate(
    text: Annotated[str | None, typer.Option("--text", "-t", help="Text to project TTS cost for.")] = None,
    duration: Annotated[
        float | None,
        typer.Option("--duration", "-d", help="Audio duration in seconds for STT projection."),
    ] = None,
    model: Annotated[str, typer.Option("--model", help="tts-1 / tts-1-hd / whisper-1.")] = "tts-1",
) -> None:
    """Estimate USD cost without making an API call.

    Pass --text for TTS or --duration for STT (or both).
    """
    if text is None and duration is None:
        typer.secho("Error: pass --text and/or --duration", fg="red", err=True)
        raise typer.Exit(2)

    if text is not None:
        cost = tts_cost_usd(text, model=model)
        typer.echo(f"TTS  ({model:8s})  {len(text):5d} chars   = ${cost:.4f}")
    if duration is not None:
        cost = stt_cost_usd(duration, model=model if model.startswith("whisper") else "whisper-1")
        typer.echo(f"STT  (whisper-1)   {duration:5.1f}s        = ${cost:.4f}")


@voice_app.command("talk")
def voice_talk(
    prefer_local: Annotated[
        bool,
        typer.Option(
            "--local",
            help="Prefer local STT backends (mlx-whisper / whisper-cpp) over the OpenAI API.",
        ),
    ] = False,
) -> None:
    """Enter continuous push-to-talk voice mode.

    Press Enter to start a recording, Enter again to stop and send. Ctrl+C exits.
    Real spacebar push-to-talk + barge-in keyboard handling lands in a polish PR.
    """
    import asyncio

    from extensions.voice_mode.voice_mode import run_voice_loop

    from opencomputer.agent.config import _home
    from opencomputer.cost_guard.guard import get_default_guard

    typer.echo("🎤 voice-mode: starting (Enter to record / Enter to stop, Ctrl+C to exit)")

    # Stub agent for T5 — T6+ wires the real AgentLoop.
    async def stub_agent(user_text: str) -> str:
        return f"You said: {user_text}. (real agent integration lands in a follow-up)"

    def record_trigger() -> bool:
        # Block until the user presses Enter twice (start + stop).
        # The orchestrator wraps the AudioCapture lifecycle around this call,
        # so the second prompt acts as the "stop" edge.
        input("Press Enter to record (then Enter to stop): ")
        return True

    def stop_trigger() -> bool:
        return False  # never stop autonomously; user exits via Ctrl+C

    try:
        asyncio.run(
            run_voice_loop(
                agent_runner=stub_agent,
                cost_guard=get_default_guard(),
                profile_home=_home(),
                record_trigger=record_trigger,
                stop_trigger=stop_trigger,
                prefer_local_stt=prefer_local,
            )
        )
    except KeyboardInterrupt:
        typer.echo("\nvoice-mode: stopped")


@voice_app.command("realtime")
def voice_realtime(
    provider: str = typer.Option(
        "openai",
        "--provider",
        help="Realtime provider name (must be registered via api.register_realtime_bridge).",
    ),
    voice: str = typer.Option(
        "alloy",
        "--voice",
        help="OpenAI realtime voice (alloy/ash/ballad/cedar/coral/echo/marin/sage/shimmer/verse). Ignored by providers that don't expose a voice picker.",
    ),
    model: str = typer.Option(
        "",
        "--model",
        help="Realtime model id. Empty → bridge factory's default.",
    ),
    instructions: str = typer.Option(
        "",
        "--instructions",
        help="Extra system-style instructions appended to the composed prompt.",
    ),
    silence_duration_ms: int = typer.Option(
        250,
        "--silence-duration-ms",
        help="VAD silence threshold (ms) — how long of a pause before the model treats you as 'done speaking'. Default 250 (snappy). Bump to 500 for slow/deliberate speech.",
    ),
    prefix_padding_ms: int = typer.Option(
        40,
        "--prefix-padding-ms",
        help="VAD lookbehind (ms) — how much pre-speech audio to include when start-of-speech triggers. 40ms is plenty for clean speech; lower can clip word starts.",
    ),
    block_size_ms: int = typer.Option(
        20,
        "--block-size-ms",
        help="Mic + speaker block size in ms. 20ms (default, snappy) trades ~2.5× more WS frames for ~30ms shaved off mic-buffer latency AND finer-grained server VAD. Bump to 50 if you see audio glitches.",
    ),
    no_transcripts: bool = typer.Option(
        False,
        "--no-transcripts",
        help="Skip live transcription on the wire. Saves ~50-100ms server-side per turn. Use when you only want voice-out and don't need the text shown back.",
    ),
    no_tools: bool = typer.Option(
        False,
        "--no-tools",
        help="Don't register OC's tool registry with the realtime model. Use when you want a pure-chat voice loop with no actions.",
    ),
    no_persona: bool = typer.Option(
        False,
        "--no-persona",
        help="Skip the OpenComputer identity preamble + profile SOUL.md. Use --instructions only as the system prompt.",
    ),
    resume_session: str = typer.Option(
        "",
        "--resume-session",
        help="Session id (or prefix) to resume — last messages get summarised into the system prompt so the model picks up the thread.",
    ),
) -> None:
    """Two-way streaming voice — provider chosen by ``--provider``.

    Bridge factories are registered by plugins via
    ``api.register_realtime_bridge(name, factory, env_var=...,
    audio_sink_kwargs=...)``. The CLI pulls the env var (for API-key
    validation) and audio-sink kwargs (for output sample rate) from
    that registration — no hardcoded provider table here. Ctrl+C exits.
    """
    import asyncio
    import os

    provider_id = provider.strip().lower()
    registration = _resolve_realtime_bridge_registration(provider_id)

    # Validate API key only when the plugin declared an env_var. Plugins
    # that source credentials another way (file, keychain, etc.) leave
    # env_var=None and handle missing creds inside their factory.
    api_key = ""
    if registration.env_var:
        api_key = os.environ.get(registration.env_var, "").strip()
        if not api_key:
            typer.secho(
                f"{registration.env_var} not set — required for provider {provider_id!r}.",
                fg="red", err=True,
            )
            raise typer.Exit(code=2)

    typer.echo(f"🎤 voice realtime ({provider_id}): connecting (Ctrl+C to exit)…")
    asyncio.run(_run_realtime_loop(
        provider=provider_id,
        api_key=api_key,
        model=model,
        voice=voice,
        instructions=instructions,
        audio_sink_kwargs=dict(registration.audio_sink_kwargs),
        silence_duration_ms=silence_duration_ms,
        prefix_padding_ms=prefix_padding_ms,
        block_size_ms=block_size_ms,
        enable_transcription=not no_transcripts,
        register_tools=not no_tools,
        include_persona=not no_persona,
        resume_session_id=resume_session or None,
    ))


def _resolve_realtime_bridge_registration(provider: str):
    """Return the bridge registration for ``provider`` from the plugin registry.

    Loads plugins lazily on first access so this works for one-shot CLI
    runs that didn't go through ``cli._discover_plugins``. Raises
    ``typer.Exit(2)`` with the available names listed if the provider
    isn't registered.
    """
    from opencomputer.plugins.registry import registry as plugin_registry

    # Lazy-load plugins if no api has been built yet. Importing cli at
    # module level would be circular (cli.py imports voice_app from us).
    if plugin_registry.shared_api is None:
        from opencomputer.cli import _discover_plugins
        _discover_plugins()

    api = plugin_registry.shared_api
    if api is None:
        # Discovery ran but built no api — pre-Phase-N codepaths. Build
        # one ad-hoc so registration calls land on the same map any
        # post-discovery code would see.
        api = plugin_registry.api()
        plugin_registry.shared_api = api

    try:
        return api.get_realtime_bridge_registration(provider)
    except KeyError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(code=2) from exc


async def _run_realtime_loop(
    *,
    provider: str,
    api_key: str,
    model: str,
    voice: str,
    instructions: str,
    audio_sink_kwargs: dict[str, object] | None = None,
    silence_duration_ms: int = 250,
    prefix_padding_ms: int = 40,
    block_size_ms: int = 20,
    enable_transcription: bool = True,
    register_tools: bool = True,
    include_persona: bool = True,
    resume_session_id: str | None = None,
) -> None:
    """Build the bridge + audio I/O + tool router and run until Ctrl+C.

    Pulled out as a module-level coroutine so tests can call it with
    monkey-patched bridge/audio without spinning the CLI runner. The
    bridge is resolved via the plugin-driven registry; ``provider`` is
    just a name the plugin claimed when it called
    ``api.register_realtime_bridge``.
    """
    import asyncio

    from opencomputer.tools.registry import registry  # singleton, audit B1
    from opencomputer.voice.audio_io import LocalAudioIO
    from opencomputer.voice.realtime_context import (
        compose_system_prompt,
        load_profile_persona,
        load_recent_messages,
        registered_tools_for_realtime,
    )
    from opencomputer.voice.realtime_session import create_realtime_voice_session
    from opencomputer.voice.tool_router import dispatch_realtime_tool_call
    from plugin_sdk.runtime_context import RuntimeContext

    # Subsystem B follow-up (2026-05-02) — flag voice context so the
    # effort policy picks ``low`` automatically. Realtime voice can't
    # afford reasoning budget on the round-trip critical path.
    runtime = RuntimeContext(custom={"voice_mode": True})
    # The CLI command above already resolved the registration to read
    # env_var + audio_sink_kwargs; here we only need the factory.
    factory = _resolve_realtime_bridge_registration(provider).factory

    # ── Compose the system prompt + tool list the bridge gets ───────────
    # The realtime bridge defaults are NO tools and NO system prompt, so
    # without this block the model has no idea what OC is or what it can
    # do. Toggle either off via --no-tools / --no-persona for a bare
    # session.
    realtime_tools = (
        registered_tools_for_realtime(registry) if register_tools else ()
    )
    profile_persona = load_profile_persona() if include_persona else ""
    resumed_summary = (
        load_recent_messages(resume_session_id) if resume_session_id else ""
    )
    composed_instructions = compose_system_prompt(
        tool_count=len(realtime_tools),
        user_instructions=instructions or None,
        resumed_session_summary=resumed_summary or None,
        profile_persona=profile_persona or None,
    ) if include_persona else (instructions or None)

    audio: LocalAudioIO | None = None
    session = None  # type: ignore[var-annotated]

    # Surface connect/runtime failures so the user doesn't see a silent
    # ``connecting…`` hang. Both events fire on the loop thread (bridge
    # callbacks are invoked from ``_do_connect`` / ``_read_loop``), so
    # ``asyncio.Event`` is safe to set without ``call_soon_threadsafe``.
    exit_event = asyncio.Event()
    exit_reason: list[str] = []

    def _on_error(exc: Exception) -> None:
        if not exit_reason:
            msg = str(exc) or type(exc).__name__
            exit_reason.append(f"error: {msg}")
        exit_event.set()

    def _on_close(reason: str) -> None:
        # ``"completed"`` means our own ``close()`` ran (intentional);
        # don't surface that as an error. ``"error"`` = reconnect gave up.
        if reason == "completed":
            exit_event.set()
            return
        if not exit_reason:
            exit_reason.append(f"closed: {reason}")
        exit_event.set()

    def _on_mic_chunk(chunk: bytes) -> None:
        if session is None:
            return
        session.send_audio(chunk)

    # Convert block_size_ms → frames at 16 kHz (mic input rate).
    # Speaker uses the same block size — slightly under-sized at 24 kHz
    # (output rate for Gemini) but that just means more frequent feeds,
    # not a quality issue.
    block_size_frames = max(1, int(round(16_000 * block_size_ms / 1000)))
    audio = LocalAudioIO(
        on_mic_chunk=_on_mic_chunk,
        block_size=block_size_frames,
        **(audio_sink_kwargs or {}),
    )

    def _on_tool_call(event, sess) -> None:
        asyncio.create_task(dispatch_realtime_tool_call(
            event=event, registry=registry, bridge=sess.bridge, runtime=runtime,
        ))

    def _create_bridge(callbacks):
        return factory(
            callbacks=callbacks,
            api_key=api_key,
            model=model or None,
            voice=voice,
            instructions=composed_instructions,
            tools=realtime_tools,
            silence_duration_ms=silence_duration_ms,
            prefix_padding_ms=prefix_padding_ms,
            enable_transcription=enable_transcription,
        )

    session = create_realtime_voice_session(
        create_bridge=_create_bridge,
        audio_sink=audio,
        on_tool_call=_on_tool_call,
        on_error=_on_error,
        on_close=_on_close,
    )

    audio.start()
    try:
        await session.connect()
        # Wait for either Ctrl+C (KeyboardInterrupt) or an
        # error/close signal from the bridge. Polling ``asyncio.Event``
        # via wait() lets the loop run the bridge's read coroutine
        # while we idle.
        await exit_event.wait()
        if exit_reason:
            typer.secho(f"voice realtime: {exit_reason[0]}", fg="red", err=True)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        session.close()
        audio.stop()


__all__ = ["voice_app"]
